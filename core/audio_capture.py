"""WASAPI shared-mode audio capture, VAD segmentation and anti-feedback gate.

Pipeline per channel
--------------------
    WASAPI shared stream (int16)
        -> down-mix to mono + gain + DC removal
        -> streaming anti-alias FIR + linear resampler to 16 kHz
        -> Silero-VAD (ONNX Runtime, CPU) frame classifier
        -> UtteranceSegmenter (pre-roll, silence hangover, max length)
        -> AudioSegment  ->  inference worker

Nothing in this module opens an *exclusive* WASAPI stream: meetings keep
working, the microphone is never locked, and loopback capture listens to the
same mix that the user hears.  While ``TTSGate`` is closed (TTS speaking +
350 ms), the PortAudio callback queue is flushed continuously so no backlog can
leak into the recogniser after the voice stops.

Threading note: PortAudio is COM-apartment bound on Windows.  The PyAudio
instance is created on the Tk thread, and each capture thread calls
``CoInitializeEx`` before opening its stream; without it WASAPI returns
``Unanticipated host error (-9999)``.
"""

from __future__ import annotations

import ctypes
import math
import queue
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

import numpy as np

TARGET_SR = 16_000
#: Silero VAD v4/v5 expects exactly 512 samples per hop at 16 kHz.
VAD_FRAME = 512
VAD_FRAME_MS = VAD_FRAME * 1000.0 / TARGET_SR
#: SHA-256 of the pinned Silero v5.1.2 ONNX model (512-sample window).
#: The upstream master branch switched to a 256-sample graph; a mismatch here
#: is the first thing to check when "speech is never detected".
VAD_SHA256 = "2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f"


# ---------------------------------------------------------------------------
# Device model
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    channels: int
    sample_rate: int
    kind: str  # "input" | "loopback"
    is_default: bool = False

    @property
    def label(self) -> str:
        suffix = ""
        if self.kind == "loopback":
            suffix = "  [LOOPBACK]"
        elif self.is_default:
            suffix = "  [DEFAULT]"
        return f"{self.name}{suffix}"


@dataclass
class AudioSegment:
    channel: str  # "mic" | "loopback"
    samples: np.ndarray
    sample_rate: int
    started_at: float
    ended_at: float
    rms: float
    reason: str = "silence"
    device_name: str = ""

    @property
    def duration(self) -> float:
        return len(self.samples) / float(self.sample_rate)


def _co_initialize() -> bool:
    """Initialise COM on a capture thread (required by WASAPI/PortAudio).

    PyAudio created on the Tk thread cannot open a WASAPI stream on another
    thread unless that thread owns a COM apartment; without this the call
    fails with ``Unanticipated host error (-9999)``.
    """
    if sys.platform != "win32":
        return False
    try:
        result = ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # STA
        return result in (0, 1)  # S_OK / S_FALSE
    except Exception:
        return False


def _co_uninitialize(active: bool) -> None:
    if not active:
        return
    try:
        ctypes.windll.ole32.CoUninitialize()
    except Exception:
        pass


def _dedup(values: Iterable[int]) -> list[int]:
    out: list[int] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


def enumerate_devices(pa=None) -> dict[str, list[AudioDevice]]:
    """Return ``{"input": [...], "loopback": [...]}``.

    Safe to call before any stream is open.  Exceptions from exotic drivers
    are swallowed per device so one broken endpoint cannot hide the others.
    """
    import pyaudiowpatch as pyaudio

    owns_pa = pa is None
    if owns_pa:
        pa = pyaudio.PyAudio()

    inputs: list[AudioDevice] = []
    loops: list[AudioDevice] = []
    try:
        try:
            wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            wasapi_index = int(wasapi["index"])
            default_in = int(wasapi["defaultInputDevice"])
        except Exception:
            wasapi_index = -1
            default_in = -1

        for index in range(pa.get_device_count()):
            try:
                info = pa.get_device_info_by_index(index)
            except Exception:
                continue
            if info.get("isLoopbackDevice"):
                continue
            if int(info.get("hostApi", -2)) != wasapi_index:
                continue
            channels = int(info.get("maxInputChannels", 0) or 0)
            if channels <= 0:
                continue
            inputs.append(
                AudioDevice(
                    index=index,
                    name=str(info.get("name", f"Input {index}")),
                    channels=channels,
                    sample_rate=int(info.get("defaultSampleRate", 48000) or 48000),
                    kind="input",
                    is_default=(index == default_in),
                )
            )

        try:
            default_loop_index = int(pa.get_default_wasapi_loopback()["index"])
        except Exception:
            default_loop_index = -1

        generator = getattr(pa, "get_loopback_device_info_generator", None)
        if callable(generator):
            try:
                for info in generator():
                    channels = int(info.get("maxInputChannels", 0) or 0)
                    if channels <= 0:
                        continue
                    index = int(info["index"])
                    loops.append(
                        AudioDevice(
                            index=index,
                            name=str(info.get("name", f"Loopback {index}")),
                            channels=channels,
                            sample_rate=int(
                                info.get("defaultSampleRate", 48000) or 48000
                            ),
                            kind="loopback",
                            is_default=(index == default_loop_index),
                        )
                    )
            except Exception:
                pass
    finally:
        if owns_pa:
            try:
                pa.terminate()
            except Exception:
                pass

    return {"input": inputs, "loopback": loops}


def resolve_device(
    wanted: str, devices: list[AudioDevice], prefer_default: bool = True
) -> Optional[AudioDevice]:
    """Resolve a saved device *name* (indices are not stable across reboots)."""
    wanted = (wanted or "").strip()
    if wanted:
        for dev in devices:
            if dev.name == wanted:
                return dev
        low = wanted.lower()
        for dev in devices:
            if low in dev.name.lower():
                return dev
    if prefer_default:
        for dev in devices:
            if dev.is_default:
                return dev
    return devices[0] if devices else None


# ---------------------------------------------------------------------------
# Streaming resampler (numpy only, no scipy)
# ---------------------------------------------------------------------------
class _StreamingFir:
    """Windowed-sinc low-pass with overlap state (anti-aliasing before decimation)."""

    def __init__(self, taps: int, cutoff_cycles_per_sample: float) -> None:
        self.taps = int(taps)
        n = np.arange(self.taps, dtype=np.float64) - (self.taps - 1) / 2.0
        h = 2.0 * cutoff_cycles_per_sample * np.sinc(2.0 * cutoff_cycles_per_sample * n)
        h *= np.hamming(self.taps)
        total = float(np.sum(h))
        if total != 0.0:
            h /= total
        self.h = h.astype(np.float64)
        self.state = np.zeros(self.taps - 1, dtype=np.float64)

    def process(self, block: np.ndarray) -> np.ndarray:
        buffer = np.concatenate((self.state, block.astype(np.float64, copy=False)))
        out = np.convolve(buffer, self.h, mode="valid")
        self.state = buffer[-(self.taps - 1) :].copy()
        return out.astype(np.float32)

    def reset(self) -> None:
        self.state.fill(0.0)


class StreamingResampler:
    """Rational resampling with anti-alias protection.

    A windowed-sinc low-pass runs at the *source* rate, then a fractional
    linear interpolator converts to 16 kHz.  This keeps the module free of
    scipy while staying clean enough for Whisper/Silero (SNR > 60 dB in the
    speech band for 44.1/48 kHz sources).
    """

    def __init__(self, src_rate: int, dst_rate: int = TARGET_SR) -> None:
        self.src_rate = int(src_rate or 48000)
        self.dst_rate = int(dst_rate)
        self.ratio = self.dst_rate / float(self.src_rate)
        self._buffer = np.zeros(0, dtype=np.float32)
        self._pos = 0.0
        self._fir: Optional[_StreamingFir] = None
        if self.dst_rate < self.src_rate:
            cutoff = 0.45 * self.dst_rate / self.src_rate
            self._fir = _StreamingFir(taps=127, cutoff_cycles_per_sample=cutoff)

    def process(self, block: np.ndarray) -> np.ndarray:
        block = np.asarray(block, dtype=np.float32).reshape(-1)
        if block.size == 0:
            return block
        if self.src_rate == self.dst_rate:
            return block
        if self._fir is not None:
            block = self._fir.process(block)

        self._buffer = np.concatenate((self._buffer, block))
        out: list[float] = []
        buf = self._buffer
        step = 1.0 / self.ratio
        while True:
            index = int(self._pos)
            if index + 1 >= len(buf):
                break
            frac = self._pos - index
            out.append(float(buf[index] * (1.0 - frac) + buf[index + 1] * frac))
            self._pos += step

        # A downsampling step can jump beyond the current buffer. Keep the
        # residual position relative to the next block, not to discarded data.
        consumed = min(int(self._pos), len(buf))
        if consumed > 0:
            self._buffer = buf[consumed:].copy()
            self._pos -= consumed
        return np.asarray(out, dtype=np.float32)

    def reset(self) -> None:
        self._buffer = np.zeros(0, dtype=np.float32)
        self._pos = 0.0
        if self._fir is not None:
            self._fir.reset()


# ---------------------------------------------------------------------------
# VAD backends
# ---------------------------------------------------------------------------
class SileroVadOnnx:
    """Silero VAD v4/v5 executed directly on ONNX Runtime.

    Using the raw ONNX graph (instead of the ``silero-vad`` package) removes
    the PyTorch dependency: the whole VAD costs ~2 MB and a few milliseconds
    per 32 ms frame on one CPU core.
    """

    _sessions: dict[str, object] = {}
    _sessions_lock = threading.Lock()

    def __init__(self, model_path: str, threads: int = 1) -> None:
        import onnxruntime as ort

        self.model_path = str(model_path)
        key = self.model_path
        with SileroVadOnnx._sessions_lock:
            session = SileroVadOnnx._sessions.get(key)
            if session is None:
                options = ort.SessionOptions()
                options.intra_op_num_threads = max(1, int(threads))
                options.inter_op_num_threads = 1
                options.log_severity_level = 3
                options.graph_optimization_level = (
                    ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                )
                session = ort.InferenceSession(
                    key, sess_options=options, providers=["CPUExecutionProvider"]
                )
                SileroVadOnnx._sessions[key] = session
        self.session = session
        self._input_names = [item.name for item in session.get_inputs()]
        self._output_names = [item.name for item in session.get_outputs()]
        self._state_shape = [2, 1, 128]
        for item in session.get_inputs():
            if item.name == "state" and item.shape:
                shape = []
                for dim in item.shape:
                    shape.append(int(dim) if isinstance(dim, int) and dim > 0 else 1)
                if len(shape) == 3:
                    self._state_shape = shape
        self._state = np.zeros(self._state_shape, dtype=np.float32)
        self._context = np.zeros(64, dtype=np.float32)
        self.model_sha256 = ""
        self.model_matches_pinned = True
        try:
            import hashlib

            digest = hashlib.sha256()
            with open(self.model_path, "rb") as handle:
                for block in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(block)
            self.model_sha256 = digest.hexdigest()
            self.model_matches_pinned = self.model_sha256.lower() == VAD_SHA256.lower()
        except Exception:
            pass

    def reset(self) -> None:
        self._state = np.zeros(self._state_shape, dtype=np.float32)
        self._context = np.zeros(64, dtype=np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        frame = np.asarray(frame, dtype=np.float32).reshape(-1)
        if frame.size != VAD_FRAME:
            raise ValueError("Silero v5 requires a 512-sample hop at 16 kHz")
        # v5.1.2's official wrapper prepends 64 samples of acoustic context.
        # 512 is the hop size, NOT the complete ONNX input size (576).
        # https://github.com/snakers4/silero-vad/blob/v5.1.2/src/silero_vad/utils_vad.py
        model_input = np.concatenate((self._context, frame))
        feed: dict[str, np.ndarray] = {}
        for name in self._input_names:
            if name == "input":
                feed[name] = model_input.reshape(1, -1)
            elif name == "sr":
                feed[name] = np.asarray(TARGET_SR, dtype=np.int64)
            elif name == "state":
                feed[name] = self._state
            else:
                feed[name] = np.zeros((1,), dtype=np.float32)
        outputs = self.session.run(None, feed)
        self._context = frame[-64:].copy()
        result = dict(zip(self._output_names, outputs))
        prob = float(np.asarray(result.get("output", outputs[0])).reshape(-1)[0])
        new_state = result.get("stateN")
        if new_state is not None:
            self._state = np.asarray(new_state, dtype=np.float32)
        return max(0.0, min(1.0, prob))


class EnergyVad:
    """Adaptive RMS fallback used when Silero/ORT is unavailable.

    Not as accurate as Silero, but it is a real VAD (adaptive noise floor +
    absolute gate) so the application never becomes unusable.
    """

    def __init__(self, min_rms: float = 0.006, ratio: float = 3.0) -> None:
        self.min_rms = float(min_rms)
        self.ratio = float(ratio)
        self._noise = 0.003

    def reset(self) -> None:
        self._noise = 0.003

    def __call__(self, frame: np.ndarray) -> float:
        frame = np.asarray(frame, dtype=np.float32)
        rms = float(np.sqrt(np.mean(frame * frame) + 1e-12))
        threshold = max(self.min_rms, self._noise * self.ratio)
        speech = rms > threshold
        if not speech:
            self._noise = 0.95 * self._noise + 0.05 * min(rms, 0.05)
        return 1.0 if speech else 0.0


def build_vad(model_path: str, backend: str = "silero-onnx", threads: int = 1):
    """Create a VAD instance, degrading to the energy backend on any failure."""
    if backend != "energy":
        try:
            import os

            if model_path and os.path.isfile(model_path):
                return SileroVadOnnx(model_path, threads=threads), "silero-onnx"
        except Exception:
            pass
    return EnergyVad(), "energy"


# ---------------------------------------------------------------------------
# Utterance segmentation
# ---------------------------------------------------------------------------
class UtteranceSegmenter:
    """Turn a continuous 16 kHz stream into speech utterances.

    * Pre-roll protects word onsets; hysteresis preserves quieter syllables.
    * A natural pause ends a turn; long turns carry context across boundaries.
    """

    def __init__(
        self,
        vad,
        threshold: float = 0.5,
        min_silence_ms: int = 1100,
        min_speech_ms: int = 240,
        speech_pad_ms: int = 320,
        max_utterance_s: float = 24.0,
    ) -> None:
        self.vad = vad
        self.threshold = float(threshold)
        self.frame_ms = VAD_FRAME_MS
        self.min_silence_ms = float(min_silence_ms)
        self.min_speech_ms = float(min_speech_ms)
        self.pad_frames = max(1, int(round(speech_pad_ms / self.frame_ms)))
        self.max_frames = max(1, int(max_utterance_s * 1000.0 / self.frame_ms))

        self._pending = np.zeros(0, dtype=np.float32)
        self._pre: deque[np.ndarray] = deque(maxlen=self.pad_frames + 1)
        self._frames: list[np.ndarray] = []
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._active = False
        self._started_at = 0.0

    def reset(self) -> None:
        self._pending = np.zeros(0, dtype=np.float32)
        self._pre.clear()
        self._frames = []
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._active = False
        self._started_at = 0.0
        self.vad.reset()

    def push(
        self, samples: np.ndarray, now: float
    ) -> list[tuple[np.ndarray, str, float]]:
        """Feed arbitrary-length audio; return ``[(audio, reason, started_at)]``."""
        results: list[tuple[np.ndarray, str, float]] = []
        if samples.size:
            self._pending = np.concatenate(
                (self._pending, np.asarray(samples, dtype=np.float32))
            )

        while self._pending.size >= VAD_FRAME:
            frame = self._pending[:VAD_FRAME].copy()
            self._pending = self._pending[VAD_FRAME:]
            frame = frame - float(np.mean(frame))

            if not self._active:
                is_speech = float(self.vad(frame)) >= self.threshold
                self._pre.append(frame)
                if not is_speech:
                    continue
                self._active = True
                self._frames = list(self._pre)
                self._speech_ms = self.frame_ms
                self._silence_ms = 0.0
                self._started_at = now - len(self._frames) * self.frame_ms / 1000.0
            else:
                self._frames.append(frame)
                if float(self.vad(frame)) >= max(0.1, self.threshold - 0.15):
                    self._speech_ms += self.frame_ms
                    self._silence_ms = 0.0
                else:
                    self._silence_ms += self.frame_ms

            if self._active and self._silence_ms >= self.min_silence_ms:
                segment = self._finalize(reason="silence")
                if segment is not None:
                    results.append(segment)
            elif self._active and len(self._frames) >= self.max_frames:
                # Keep acoustic context across the rare safety-limit split.
                overlap = self._frames[-self.pad_frames :]
                segment = self._finalize(reason="max_length")
                if segment is not None:
                    results.append(segment)
                self._pre.extend(overlap)

        return results

    def flush(self) -> Optional[tuple[np.ndarray, str, float]]:
        """Emit whatever is buffered (used when stopping the channel)."""
        if not self._active or len(self._frames) < max(
            2, int(self.min_speech_ms / self.frame_ms)
        ):
            self.reset()
            return None
        if self._pending.size:
            self._frames.append(self._pending.copy())
            self._pending = np.zeros(0, dtype=np.float32)
        return self._finalize(reason="flush")

    def _finalize(self, reason: str) -> Optional[tuple[np.ndarray, str, float]]:
        frames = self._frames
        trailing = max(0, int(round(self._silence_ms / self.frame_ms)))
        if trailing > self.pad_frames:
            frames = frames[: len(frames) - (trailing - self.pad_frames)]
        started_at = self._started_at
        speech_ms = self._speech_ms
        self._active = False
        self._frames = []
        self._pre.clear()
        self._speech_ms = 0.0
        self._silence_ms = 0.0

        if not frames or speech_ms < self.min_speech_ms:
            return None
        audio = np.concatenate(frames).astype(np.float32, copy=False)
        return audio, reason, started_at


# ---------------------------------------------------------------------------
# Capture channel
# ---------------------------------------------------------------------------
class ChannelCapture(threading.Thread):
    """One WASAPI shared-mode input stream -> VAD utterances."""

    def __init__(
        self,
        pa,
        device: AudioDevice,
        channel: str,
        *,
        gate,
        on_segment: Callable[[AudioSegment], None],
        on_level: Callable[[str, float], None],
        on_status: Callable[[str, str], None],
        on_error: Callable[[str, str], None],
        gain: float = 1.0,
        vad_model: str = "models/silero_vad.onnx",
        vad_backend: str = "silero-onnx",
        vad_threshold: float = 0.5,
        min_silence_ms: int = 1100,
        min_speech_ms: int = 240,
        speech_pad_ms: int = 320,
        max_utterance_s: float = 24.0,
        chunk_ms: int = 20,
        vad_threads: int = 1,
    ) -> None:
        super().__init__(name=f"stemtrans-{channel}", daemon=True)
        self.pa = pa
        self.device = device
        self.channel = channel
        self.gate = gate
        self.on_segment = on_segment
        self.on_level = on_level
        self.on_status = on_status
        self.on_error = on_error
        self.gain = float(gain)
        self.chunk_ms = max(10, int(chunk_ms))

        self.vad, self.vad_backend = build_vad(
            vad_model, backend=vad_backend, threads=vad_threads
        )
        self.segmenter = UtteranceSegmenter(
            self.vad,
            threshold=vad_threshold,
            min_silence_ms=min_silence_ms,
            min_speech_ms=min_speech_ms,
            speech_pad_ms=speech_pad_ms,
            max_utterance_s=max_utterance_s,
        )

        self._stop_event = threading.Event()
        self._stream = None
        self._actual_rate = 0
        self._resampler: Optional[StreamingResampler] = None
        self._last_level_ts = 0.0
        self._dropped_frames = 0
        # PortAudio callback -> bounded queue.  A blocking read() on an idle
        # WASAPI loopback endpoint can wait forever (the output device only
        # produces frames while something is playing), which would freeze the
        # channel thread and make stop() impossible.  The callback keeps the
        # thread responsive: it simply receives nothing while the device idles.
        self._raw_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=32)
        self._idle_warned = False
        self.opened = threading.Event()
        self.open_error = ""
        self._last_audio_at = time.monotonic()
        self._overflow = threading.Event()
        try:
            import pyaudiowpatch as pyaudio

            self._pa_continue = pyaudio.paContinue
            self._pa_complete = pyaudio.paComplete
        except Exception:
            self._pa_continue = 0
            self._pa_complete = 1

    # -- lifecycle -----------------------------------------------------------
    def stop(self) -> None:
        """Request shutdown; the capture thread closes its own stream.

        Never call ``stream.close()`` from another thread: PortAudio/WASAPI can
        block for several seconds waiting for the callback/COM apartment, which
        is exactly what made window-close take 10 s in testing.  The run loop
        wakes from ``queue.get(timeout=0.12)`` and closes the stream in its own
        ``finally`` block.
        """
        self._stop_event.set()

    @property
    def dropped_frames(self) -> int:
        return self._dropped_frames

    # -- stream opening ------------------------------------------------------
    def _wasapi_shared_info(self):
        """Ask PortAudio for shared mode with automatic format conversion.

        ``paWinWasapiAutoConvert`` makes WASAPI honour our requested rate and
        channel count instead of the raw mix format.  If the installed
        PortAudio build does not expose it we simply open a plain shared
        stream at the device's default rate (the mix rate), which also works.
        """
        try:
            import pyaudiowpatch as pyaudio

            return pyaudio.PaWasapiStreamInfo(flags=pyaudio.paWinWasapiAutoConvert)
        except Exception:
            return None

    def _open_stream(self):
        import pyaudiowpatch as pyaudio

        base_rate = int(self.device.sample_rate or 48000)
        channel_options = _dedup([min(2, max(1, self.device.channels)), 1])
        rate_options = _dedup([base_rate, 48000, 44100])
        shared_info = self._wasapi_shared_info()

        last_error: Optional[Exception] = None
        for rate in rate_options:
            for channels in channel_options:
                kwargs = dict(
                    format=pyaudio.paInt16,
                    channels=channels,
                    rate=rate,
                    input=True,
                    input_device_index=self.device.index,
                    frames_per_buffer=max(256, int(rate * self.chunk_ms / 1000.0)),
                    stream_callback=self._callback,
                    start=True,
                )
                if shared_info is not None:
                    kwargs["input_host_api_specific_stream_info"] = shared_info
                try:
                    stream = self.pa.open(**kwargs)
                    self._actual_rate = rate
                    self._channels = channels
                    self._resampler = StreamingResampler(rate, TARGET_SR)
                    return stream
                except Exception as exc:  # try next (rate, channels) pair
                    last_error = exc
        raise RuntimeError(
            f"WASAPI shared-mode open failed for '{self.device.name}' "
            f"({self.device.kind}): {last_error}"
        )

    # -- PortAudio callback --------------------------------------------------
    def _callback(self, in_data, frame_count, time_info, status):
        """Runs on a PortAudio thread: copy bytes into the bounded queue."""
        if self._stop_event.is_set():
            return (None, self._pa_complete)
        if in_data:
            try:
                self._raw_queue.put_nowait(bytes(in_data))
            except queue.Full:
                self._dropped_frames += 1
                self._overflow.set()
                try:
                    self._raw_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._raw_queue.put_nowait(bytes(in_data))
                except queue.Full:
                    pass
        return (None, self._pa_continue)

    # -- main loop -----------------------------------------------------------
    def run(self) -> None:
        # WASAPI/PortAudio is COM-apartment bound.  The PyAudio instance lives
        # on the manager thread; this capture thread must initialise COM before
        # opening, otherwise PortAudio returns -9999 "Unanticipated host error".
        com_ready = _co_initialize()
        try:
            self._stream = self._open_stream()
        except Exception as exc:
            self.open_error = str(exc)
            self.opened.set()
            self.on_error(self.channel, str(exc))
            _co_uninitialize(com_ready)
            return
        self.opened.set()

        self.on_status(
            self.channel,
            f"CAPTURE {self.channel.upper()} @ {self._actual_rate} Hz / "
            f"{self._channels} ch / VAD={self.vad_backend}",
        )
        if not getattr(self.vad, "model_matches_pinned", True):
            self.on_status(
                self.channel,
                "VAD MODEL VERSION MISMATCH: expected the pinned Silero v5.1.2 "
                "(512-sample) ONNX. Run: python download_models.py --only vad",
            )

        try:
            while not self._stop_event.is_set() or not self._raw_queue.empty():
                if self._overflow.is_set():
                    self._overflow.clear()
                    self.on_error(
                        self.channel,
                        "音频缓冲溢出，部分声音丢失；请减少同时运行的任务。",
                    )
                # ---- anti-feedback mutex: drain, never accumulate ----------
                if self.gate.is_blocked():
                    self._flush_raw()
                    self.segmenter.reset()
                    if self._resampler is not None:
                        self._resampler.reset()
                    self._dropped_frames += 1
                    self.on_level(self.channel, 0.0)
                    time.sleep(0.02)
                    continue

                try:
                    raw = self._raw_queue.get(timeout=0.12)
                except queue.Empty:
                    # Normal for an idle loopback endpoint: nothing is playing.
                    if not self._idle_warned and self.device.kind == "loopback":
                        self._idle_warned = True
                        self.on_status(
                            self.channel,
                            "LOOPBACK idle: waiting for audio playback "
                            "(device opened, no frames yet)",
                        )
                    self.on_level(self.channel, 0.0)
                    # WASAPI loopback stops delivering even silence when the
                    # player stops. Feed silence so the last turn can finish.
                    if (
                        self.channel == "loopback"
                        and time.monotonic() - self._last_audio_at >= 0.12
                    ):
                        self._emit_segments(np.zeros(1920, dtype=np.float32))
                    continue

                pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
                pcm *= 1.0 / 32768.0
                if self._channels > 1:
                    pcm = pcm.reshape(-1, self._channels).mean(axis=1)
                if self.gain != 1.0:
                    pcm *= self.gain
                pcm -= float(np.mean(pcm))

                self._emit_level(pcm)
                self._last_audio_at = time.monotonic()
                audio16 = self._resampler.process(pcm) if self._resampler else pcm
                if audio16.size == 0:
                    continue

                self._emit_segments(audio16)
        except Exception as exc:
            self.on_error(self.channel, f"音频采集异常：{exc}")
        finally:
            tail = self.segmenter.flush()
            if tail is not None:
                audio, reason, started_at = tail
                try:
                    self.on_segment(
                        AudioSegment(
                            channel=self.channel,
                            samples=audio,
                            sample_rate=TARGET_SR,
                            started_at=started_at,
                            ended_at=time.time(),
                            rms=float(np.sqrt(np.mean(audio * audio) + 1e-12)),
                            reason=reason,
                            device_name=self.device.name,
                        )
                    )
                except Exception:
                    pass
            stream, self._stream = self._stream, None
            if stream is not None:
                for action in ("stop_stream", "close"):
                    try:
                        getattr(stream, action)()
                    except Exception:
                        pass
            _co_uninitialize(com_ready)
            self.on_status(self.channel, f"CAPTURE {self.channel.upper()} stopped")

    def _emit_segments(self, audio16: np.ndarray) -> None:
        now = time.time()
        for audio, reason, started_at in self.segmenter.push(audio16, now):
            self.on_segment(
                AudioSegment(
                    channel=self.channel,
                    samples=audio,
                    sample_rate=TARGET_SR,
                    started_at=started_at,
                    ended_at=now,
                    rms=float(np.sqrt(np.mean(audio * audio) + 1e-12)),
                    reason=reason,
                    device_name=self.device.name,
                )
            )

    def _flush_raw(self) -> None:
        """Discard everything queued while the TTS gate is closed."""
        while True:
            try:
                self._raw_queue.get_nowait()
            except queue.Empty:
                return

    def _emit_level(self, pcm: np.ndarray) -> None:
        now = time.monotonic()
        if now - self._last_level_ts < 0.08:
            return
        self._last_level_ts = now
        rms = float(np.sqrt(np.mean(pcm * pcm) + 1e-12))
        db = 20.0 * math.log10(rms + 1e-9)
        level = max(0.0, min(1.0, (db + 60.0) / 60.0))
        self.on_level(self.channel, level)


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------
class AudioCaptureManager:
    """Owns the active set of capture channels.

    PyAudio is created once on the manager (Tk) thread.  Each capture thread
    calls ``CoInitializeEx`` before opening its stream, because PortAudio's
    WASAPI backend is COM-apartment bound.  Device enumeration uses a
    short-lived separate instance so it can run on a background thread.
    """

    def __init__(
        self,
        settings: dict,
        gate,
        *,
        on_segment: Callable[[AudioSegment], None],
        on_level: Callable[[str, float], None],
        on_status: Callable[[str, str], None],
        on_error: Callable[[str, str], None],
    ) -> None:
        self.settings = settings or {}
        self.gate = gate
        self.on_segment = on_segment
        self.on_level = on_level
        self.on_status = on_status
        self.on_error = on_error

        self._channels: dict[str, ChannelCapture] = {}
        self._lock = threading.RLock()
        self.mode = "online"
        self._mic_name = ""
        self._loopback_name = ""
        self._pa = None
        self._shutting_down = threading.Event()
        # Create PyAudio on the thread that constructs the manager (the Tk
        # thread).  Capture threads only *use* it after CoInitializeEx.
        try:
            self._ensure_pa()
        except Exception as exc:
            self.on_error("system", f"PyAudio init failed: {exc}")

    # -- devices -------------------------------------------------------------
    def _ensure_pa(self):
        if self._pa is None:
            import pyaudiowpatch as pyaudio

            self._pa = pyaudio.PyAudio()
        return self._pa

    def list_devices(self) -> dict[str, list[AudioDevice]]:
        with self._lock:
            # Enumeration uses a short-lived instance so it can run on a
            # background thread without touching the stream instance's COM
            # apartment.
            return enumerate_devices()

    # -- lifecycle -----------------------------------------------------------
    def start(
        self,
        mode: str = "online",
        mic_name: str = "",
        loopback_name: str = "",
    ) -> bool:
        with self._lock:
            if self._shutting_down.is_set():
                return False
            self.stop()
            self.mode = "offline" if mode == "offline" else "online"
            self._mic_name = mic_name
            self._loopback_name = loopback_name

            audio_cfg = self.settings.get("audio", {})
            vad_cfg = audio_cfg.get("vad", {})
            try:
                devices = self.list_devices()
            except Exception as exc:
                self.on_error("system", f"cannot enumerate audio devices: {exc}")
                return False

            mic = resolve_device(mic_name, devices["input"])
            if mic is None:
                self.on_error("mic", "未找到可用麦克风设备 / no input device found")
                return False

            self._start_channel(
                mic,
                "mic",
                gain=float(audio_cfg.get("input_gain", 1.0)),
                vad_cfg=vad_cfg,
                chunk_ms=int(audio_cfg.get("chunk_ms", 20)),
            )

            if self.mode == "online":
                loop = resolve_device(loopback_name, devices["loopback"])
                if loop is None:
                    self.on_error(
                        "loopback",
                        "未找到系统声音设备。请选择可用设备，或切换到单麦对话。",
                    )
                    self.stop()
                    return False
                else:
                    self._start_channel(
                        loop,
                        "loopback",
                        gain=1.0,
                        vad_cfg=vad_cfg,
                        chunk_ms=int(audio_cfg.get("chunk_ms", 20)),
                    )
            for worker in self._channels.values():
                if self._shutting_down.is_set():
                    self.stop()
                    return False
                if not worker.opened.wait(4.0) or worker.open_error:
                    self.stop()
                    return False
            return True

    def _start_channel(
        self,
        device: AudioDevice,
        channel: str,
        *,
        gain: float,
        vad_cfg: dict,
        chunk_ms: int,
    ) -> None:
        worker = ChannelCapture(
            self._ensure_pa(),
            device,
            channel,
            gate=self.gate,
            on_segment=self.on_segment,
            on_level=self.on_level,
            on_status=self.on_status,
            on_error=self.on_error,
            gain=gain,
            vad_model=str(vad_cfg.get("model_path", "models/silero_vad.onnx")),
            vad_backend=str(vad_cfg.get("backend", "silero-onnx")),
            vad_threshold=float(vad_cfg.get("threshold", 0.5)),
            min_silence_ms=int(vad_cfg.get("min_silence_ms", 1100)),
            min_speech_ms=int(vad_cfg.get("min_speech_ms", 240)),
            speech_pad_ms=int(vad_cfg.get("speech_pad_ms", 320)),
            max_utterance_s=float(vad_cfg.get("max_utterance_s", 24.0)),
            chunk_ms=chunk_ms,
            vad_threads=int(self.settings.get("asr", {}).get("cpu_threads", 1)),
        )
        self._channels[channel] = worker
        worker.start()

    def stop(self) -> None:
        with self._lock:
            channels = list(self._channels.values())
            self._channels.clear()
        for worker in channels:
            worker.stop()
        # Hard 0.45 s budget for the whole manager: a window close must not
        # wait seconds for a stuck WASAPI driver.
        deadline = time.monotonic() + 0.45
        for worker in channels:
            remaining = max(0.02, deadline - time.monotonic())
            worker.join(timeout=remaining)
        self.on_level("mic", 0.0)
        self.on_level("loopback", 0.0)

    def shutdown(self) -> None:
        """Stop channels for application exit.

        Deliberately does **not** call ``Pa_Terminate()``: on some WASAPI
        drivers it blocks for tens of seconds waiting for idle endpoints, which
        would violate the "close the window, process is gone" guarantee.
        ``main.shutdown()`` calls ``os._exit(0)`` immediately afterwards, and
        the OS reclaims the audio handles.
        """
        self._shutting_down.set()
        if self._lock.acquire(blocking=False):
            try:
                self.stop()
            finally:
                self._lock.release()
        else:
            # Startup may be inside a slow driver call. Do not block the UI
            # exit budget waiting for it; the process owns all these handles.
            for worker in list(self._channels.values()):
                worker.stop()

    @property
    def running(self) -> bool:
        return bool(self._channels)

    @property
    def active_channels(self) -> list[str]:
        return list(self._channels.keys())


# ---------------------------------------------------------------------------
# Manual self-test:  python -m core.audio_capture --seconds 4
# ---------------------------------------------------------------------------
def _self_test(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="StemTrans audio self-test")
    parser.add_argument("--seconds", type=float, default=4.0)
    parser.add_argument("--loopback", action="store_true")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)

    class _Gate:
        def is_blocked(self) -> bool:
            return False

    print("Enumerating WASAPI endpoints ...")
    devices = enumerate_devices()
    for kind in ("input", "loopback"):
        print(f"\n[{kind}]")
        for dev in devices[kind]:
            print(
                f"  {dev.index:>3}  {dev.label}  ({dev.channels} ch @ {dev.sample_rate} Hz)"
            )
    if args.list:
        return 0

    kind = "loopback" if args.loopback else "input"
    if not devices[kind]:
        print(f"No {kind} device available.")
        return 2
    target = next((d for d in devices[kind] if d.is_default), devices[kind][0])

    segments: list[AudioSegment] = []
    import pyaudiowpatch as pyaudio

    pa = pyaudio.PyAudio()
    print(f"\nRecording {args.seconds:.1f}s from: {target.name} ... speak now.")
    worker = ChannelCapture(
        pa,
        target,
        kind,
        gate=_Gate(),
        on_segment=segments.append,
        on_level=lambda ch, lvl: None,
        on_status=lambda ch, msg: print(f"[status] {msg}"),
        on_error=lambda ch, msg: print(f"[error:{ch}] {msg}"),
        vad_backend="silero-onnx",
    )
    worker.start()
    time.sleep(max(0.5, args.seconds))
    worker.stop()
    worker.join(timeout=2.0)
    pa.terminate()

    print(f"\n{len(segments)} utterance(s):")
    for index, seg in enumerate(segments, 1):
        print(
            f"  {index:02d}  {seg.duration:5.2f}s  rms={seg.rms:.4f}  "
            f"{seg.reason:<10} {seg.device_name}"
        )
    return 0 if segments else 3


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_self_test())
