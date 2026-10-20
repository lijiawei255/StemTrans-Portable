"""Offline text-to-speech worker (Windows SAPI5) with a hard anti-feedback gate.

Design notes
------------
* ``pyttsx3`` is *not* thread safe and its SAPI event loop must stay on its owner thread. One dedicated
  worker pumps it and handles cancellation for the entire engine lifetime.
* ``TTSGate`` is the single source of truth shared with every capture channel.
  While TTS is audible, and for ``cooldown_ms`` after the last phoneme, all
  microphone / loopback frames are dropped and drained.  This is what makes
  "the app hearing itself" architecturally impossible instead of merely
  unlikely.
* The module imports cleanly without pyttsx3 installed: the feature degrades
  to a no-op with one warning, so the rest of the application keeps working.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Optional


class TTSGate:
    """Global mutex protecting the audio path from the speaker output.

    The gate is intentionally tiny and lock-free on the hot path
    (``is_blocked`` is called for every 20 ms audio chunk).
    """

    def __init__(self, cooldown_ms: int = 350) -> None:
        self.cooldown_ms = max(0, int(cooldown_ms))
        self._lock = threading.Lock()
        self._speaking = threading.Event()
        self._cooldown_until = 0.0

    # -- state ---------------------------------------------------------------
    @property
    def is_tts_speaking(self) -> bool:
        """True only while the SAPI5 engine is actually producing sound."""
        return self._speaking.is_set()

    @property
    def cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - time.monotonic())

    def is_blocked(self) -> bool:
        """True when capture must discard frames (speaking + cooldown)."""
        if self._speaking.is_set():
            return True
        return time.monotonic() < self._cooldown_until

    # -- transitions ---------------------------------------------------------
    def begin(self) -> None:
        with self._lock:
            self._cooldown_until = 0.0
            self._speaking.set()

    def end(self) -> None:
        with self._lock:
            self._speaking.clear()
            self._cooldown_until = time.monotonic() + self.cooldown_ms / 1000.0

    def cancel(self) -> None:
        """Force-open the gate (used on shutdown / fatal TTS errors)."""
        with self._lock:
            self._speaking.clear()
            self._cooldown_until = 0.0

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"TTSGate(speaking={self.is_tts_speaking}, "
            f"cooldown={self.cooldown_remaining * 1000:.0f}ms)"
        )


class TTSWorker:
    """Queue-driven SAPI5 speaker.

    Parameters
    ----------
    gate:
        Shared :class:`TTSGate` instance.
    rate, volume:
        SAPI5 prosody settings.
    max_pending:
        New translations arriving while the previous one is still being spoken
        are *coalesced*: the oldest pending item is dropped so the prompter
        never lags behind the meeting by more than one sentence.
    """

    _LANG_HINTS = {
        "en": ("english", "zira", "david", "aria", "guy", "mark", "samantha"),
        "zh": ("chinese", "huihui", "yaoyao", "kangkang", "xiaoxiao", "zh-cn"),
    }

    def __init__(
        self,
        gate: TTSGate,
        rate: int = 185,
        volume: float = 0.95,
        max_pending: int = 2,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.gate = gate
        self.rate = int(rate)
        self.volume = float(volume)
        self.max_pending = max(1, int(max_pending))
        self._log = logger or (lambda msg: None)

        self._queue: "queue.Queue[Optional[tuple[str, str]]]" = queue.Queue(
            maxsize=self.max_pending
        )
        self._thread: Optional[threading.Thread] = None
        self._thread_lock = threading.Lock()
        self._stop = threading.Event()
        self._cancel_speech = threading.Event()
        self._enabled = False
        self._engine = None
        self._engine_lock = threading.Lock()
        self._warned_unavailable = False
        self._voice_ids: dict[str, str] = {}

    # -- public API ----------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def is_speaking(self) -> bool:
        return self.gate.is_tts_speaking

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        if not self._enabled:
            self._cancel_speech.set()
            self.flush()

    def say(self, text: str, lang: str = "en") -> bool:
        """Enqueue an utterance. Returns False when TTS is disabled/empty."""
        if not self._enabled:
            return False
        text = (text or "").strip()
        if not text:
            return False
        # Never queue a wall of text: the prompter should speak the newest line.
        if len(text) > 900:
            text = text[:900].rsplit(" ", 1)[0] + " ..."

        self._ensure_thread()
        item = (text, lang if lang in ("en", "zh") else "en")
        while True:
            try:
                self._queue.put_nowait(item)
                return True
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass

    def flush(self) -> None:
        """Drop everything that has not started yet."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def interrupt(self) -> None:
        """Cancel speech on its owner thread and clear queued utterances."""
        self._cancel_speech.set()
        self.flush()

    def shutdown(self, timeout: float = 1.2) -> None:
        self._stop.set()
        self.flush()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        # Do NOT call engine.stop() here: SAPI5 can block while a sentence is
        # being synthesised.  The worker thread is a daemon and main.shutdown()
        # follows with os._exit(0), so the process still leaves in well under 1 s.
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self.gate.cancel()

    # -- internals -----------------------------------------------------------
    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="stemtrans-tts", daemon=True
            )
            self._thread.start()

    def _run(self) -> None:
        try:
            import pyttsx3
        except Exception as exc:  # pragma: no cover - depends on host
            if not self._warned_unavailable:
                self._warned_unavailable = True
                self._log(
                    "TTS unavailable (pyttsx3 import failed): "
                    f"{exc}. The app continues without voice output."
                )
            return

        engine = None
        try:
            engine = pyttsx3.init("sapi5")
            engine.setProperty("rate", self.rate)
            engine.setProperty("volume", max(0.0, min(1.0, self.volume)))
            with self._engine_lock:
                self._engine = engine
            self._index_voices(engine)
            self._apply_voice(engine, "en")
            self._log("TTS engine ready (SAPI5, offline).")
        except Exception as exc:  # pragma: no cover - depends on host
            self._log(f"TTS init failed: {exc}. Voice output disabled.")
            with self._engine_lock:
                self._engine = None
            return

        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None or self._stop.is_set():
                break
            text, lang = item
            if not self._enabled:
                continue
            try:
                self._apply_voice(engine, lang)
                self._cancel_speech.clear()
                # Gate ON *before* the first sample leaves the speaker.
                self.gate.begin()
                engine.say(text)
                # Pump on the SAPI owner thread so disabling voice output can
                # actually interrupt it without unsafe cross-thread stop().
                engine.startLoop(False)
                try:
                    engine.iterate()
                    while engine.isBusy():
                        if (
                            self._stop.is_set()
                            or self._cancel_speech.is_set()
                            or not self._enabled
                        ):
                            engine.stop()
                            break
                        engine.iterate()
                        time.sleep(0.01)
                finally:
                    engine.endLoop()
            except Exception as exc:
                self._log(f"TTS utterance failed: {exc}")
                break
            finally:
                # Gate OFF + 350 ms cooldown (never leave capture muted).
                self.gate.end()

        with self._engine_lock:
            self._engine = None
        try:
            engine.stop()
        except Exception:
            pass
        self.gate.cancel()

    def _index_voices(self, engine) -> None:
        try:
            voices = engine.getProperty("voices") or []
        except Exception:
            voices = []
        for voice in voices:
            haystack = " ".join(
                [
                    str(getattr(voice, "id", "")),
                    str(getattr(voice, "name", "")),
                ]
                + [str(lang) for lang in (getattr(voice, "languages", []) or [])]
            ).lower()
            for lang, hints in self._LANG_HINTS.items():
                if lang in self._voice_ids:
                    continue
                if any(hint in haystack for hint in hints):
                    self._voice_ids[lang] = voice.id
        if self._voice_ids:
            self._log(f"TTS voices: {sorted(self._voice_ids)}")

    def _apply_voice(self, engine, lang: str) -> None:
        voice_id = self._voice_ids.get(lang)
        if not voice_id:
            return
        try:
            engine.setProperty("voice", voice_id)
        except Exception:
            pass


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    gate = TTSGate()
    worker = TTSWorker(gate, logger=print)
    worker.set_enabled(True)
    worker.say("Signal isolation is required for the STM32 ADC front end.", "en")
    for _ in range(120):
        if gate.is_blocked():
            print("gate: BLOCKED", end="\r")
        else:
            print("gate: open   ", end="\r")
        time.sleep(0.05)
    worker.shutdown()
    print("\nDone.")
