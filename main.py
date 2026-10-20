#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""StemTrans-Portable — entry point.

IMPORTANT: every cache variable is redirected to ``./models`` *before* any AI
library is imported.  Nothing is ever written to ``%USERPROFILE%``, the
registry, or a system temp folder, which is what makes the application
portable and removable by deleting its folder.

Closing the window triggers ``StemTransApp.shutdown()``: capture streams are
closed, worker threads are signalled, SAPI5 is stopped, and the process is
terminated with ``os._exit`` so no zombie thread or audio handle survives.
"""

from __future__ import annotations

import os
import sys

APP_DIR = (
    os.path.dirname(os.path.abspath(sys.executable))
    if getattr(sys, "frozen", False)
    else os.path.dirname(os.path.abspath(__file__))
)
os.chdir(APP_DIR)
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

# ---------------------------------------------------------------------------
# 1. PORTABLE SANDBOX — must run before importing numpy/ctranslate2/onnxruntime
# ---------------------------------------------------------------------------
os.environ["HF_HOME"] = os.path.abspath("./models/hf_cache")
os.environ["HF_HUB_CACHE"] = os.path.abspath("./models/hf_cache/hub")
os.environ["TORCH_HOME"] = os.path.abspath("./models/torch_cache")
os.environ["TRANSFORMERS_CACHE"] = os.path.abspath("./models/hf_cache/transformers")
os.environ["XDG_CACHE_HOME"] = os.path.abspath("./models/xdg_cache")
os.environ["NUMBA_CACHE_DIR"] = os.path.abspath("./models/numba_cache")
os.environ["OMP_NUM_THREADS"] = "4"
os.environ["MKL_NUM_THREADS"] = "4"
os.environ["OPENBLAS_NUM_THREADS"] = "4"
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["PYTHONUTF8"] = "1"
# Safe default for mainland China; overridden from settings below when needed.
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import argparse  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import platform  # noqa: E402
import queue  # noqa: E402
import signal  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from logging.handlers import RotatingFileHandler  # noqa: E402

try:  # make Chinese logs survive a GBK console
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

LOG_DIR = os.path.join(APP_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, "app.log")

logger = logging.getLogger("stemtrans")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _formatter = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S"
    )
    _file = RotatingFileHandler(
        LOG_PATH, maxBytes=1_500_000, backupCount=2, encoding="utf-8"
    )
    _file.setFormatter(_formatter)
    _console = logging.StreamHandler(stream=sys.stdout)
    _console.setFormatter(_formatter)
    logger.addHandler(_file)
    logger.addHandler(_console)

from core.audio_capture import (  # noqa: E402
    AudioCaptureManager,
    AudioSegment,
    enumerate_devices,
)
from core.inference_engine import (  # noqa: E402
    EngineNotReady,
    Glossary,
    InferenceEngine,
)
from core.tts_worker import TTSGate, TTSWorker  # noqa: E402
from ui.main_window import StemTransWindow  # noqa: E402

SETTINGS_PATH = os.path.join(APP_DIR, "config", "settings.json")
LOCAL_SETTINGS_PATH = os.path.join(APP_DIR, "config", "settings.local.json")
_settings_write_lock = threading.Lock()
GLOSSARY_PATH = os.path.join(APP_DIR, "config", "glossary.json")
DEFAULT_SETTINGS = {
    "network": {"source": "china"},
    "ui": {"mode": "offline", "opacity": 0.97, "always_on_top": True, "max_cards": 40},
    "audio": {
        "mic_device": "",
        "loopback_device": "",
        "input_gain": 1.0,
        "chunk_ms": 20,
        "vad": {
            "backend": "silero-onnx",
            "model_path": "models/silero_vad.onnx",
            "threshold": 0.5,
            "min_silence_ms": 1100,
            "min_speech_ms": 240,
            "speech_pad_ms": 320,
            "max_utterance_s": 24.0,
        },
    },
    "asr": {
        "model_size": "base",
        "model_dir": "models/whisper-base",
        "compute_type": "int8",
        "beam_size": 3,
        "cpu_threads": 4,
        "language_mic": "zh",
        "language_loopback": "en",
        "language_single": "zh",
        "use_glossary_prompt": False,
        "initial_prompt_max_chars": 780,
    },
    "llm": {
        "model_path": "models/qwen2.5-1.5b-instruct-q4_k_m.gguf",
        "n_ctx": 2048,
        "n_threads": 4,
        "max_tokens": 220,
        "temperature": 0.2,
    },
    "tts": {
        "enabled": False,
        "rate": 185,
        "volume": 0.95,
        "cooldown_ms": 350,
        "speak_languages": ["en"],
    },
    "pipeline": {"queue_size": 24, "min_chars": 1},
}


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------
def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_settings() -> dict:
    payload = {}
    for path in (SETTINGS_PATH, LOCAL_SETTINGS_PATH):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = deep_merge(payload, json.load(handle))
        except Exception as exc:
            logger.warning("%s 读取失败，保留已有配置: %s", os.path.basename(path), exc)
    settings = deep_merge(DEFAULT_SETTINGS, payload)
    if int(payload.get("version", 1)) < 2:
        settings["audio"]["vad"].update(
            min_silence_ms=1100, speech_pad_ms=320, max_utterance_s=24.0
        )
        settings["asr"].update(beam_size=3, use_glossary_prompt=False)
        settings["pipeline"].update(queue_size=24, min_chars=1)
        settings["pipeline"].pop("drop_stale", None)
        settings["version"] = 2
    # Speaking mutes the microphone for the ENTIRE utterance. Never silently
    # restore automatic speech from a previous run.
    settings["tts"]["enabled"] = False
    return settings


def save_settings(settings: dict) -> None:
    tmp = LOCAL_SETTINGS_PATH + ".tmp"
    try:
        with _settings_write_lock:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(settings, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(tmp, LOCAL_SETTINGS_PATH)
    except Exception as exc:
        logger.warning("settings.local.json 保存失败: %s", exc)


def clean_device_label(label: str) -> str:
    return (label or "").replace("  [DEFAULT]", "").replace("  [LOOPBACK]", "").strip()


def configure_network(source: str) -> str:
    source = "global" if source == "global" else "china"
    if source == "china":
        os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
    else:
        os.environ["HF_ENDPOINT"] = "https://huggingface.co"
    return source


# ---------------------------------------------------------------------------
# Processing worker
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProcessingJob:
    segment: AudioSegment
    session: int
    mode: str
    language: str


class ProcessingWorker(threading.Thread):
    """Single ASR+MT worker: keeps transcript order and bounds latency."""

    def __init__(self, app: "StemTransApp") -> None:
        super().__init__(name="stemtrans-inference", daemon=True)
        self.app = app
        self.queue: "queue.Queue[ProcessingJob]" = queue.Queue(
            maxsize=max(1, int(app.settings.get("pipeline", {}).get("queue_size", 24)))
        )
        self._stop_event = threading.Event()

    def submit(self, segment: AudioSegment) -> None:
        app = self.app
        language_key = (
            "language_single"
            if app.mode == "offline"
            else "language_mic"
            if segment.channel == "mic"
            else "language_loopback"
        )
        job = ProcessingJob(
            segment,
            app.session,
            app.mode,
            str(app.settings["asr"].get(language_key, "zh")),
        )
        try:
            self.queue.put_nowait(job)
        except queue.Full:
            logger.error("inference queue full; rejected %.2fs", segment.duration)
            app.window.post(
                {
                    "type": "status",
                    "kind": "error",
                    "message": "处理速度跟不上：本段未保存。请停止监听，等待已有语音处理完成。",
                }
            )
            app.window.post({"type": "overload"})
        self._post_pending()

    def _post_pending(self) -> None:
        self.app.window.post(
            {
                "type": "pending",
                "count": self.queue.qsize(),
                "total": self.queue.unfinished_tasks,
            }
        )

    def clear(self) -> None:
        while True:
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except queue.Empty:
                break
        self._post_pending()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                job = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if self._stop_event.is_set():
                self.queue.task_done()
                break
            try:
                if job.session == self.app.session:
                    self._process(job)
            except EngineNotReady as exc:
                logger.error("%s", exc)
                self.app.window.post(
                    {
                        "type": "status",
                        "kind": "error",
                        "message": "模型未就绪：请先运行 download_models.py（详情见 logs/app.log）",
                    }
                )
            except Exception:
                logger.error("segment failed\n%s", traceback.format_exc())
                self.app.window.post(
                    {
                        "type": "status",
                        "kind": "error",
                        "message": "本段处理失败，已跳过 / segment skipped (see logs)",
                    }
                )
            finally:
                self.queue.task_done()
                self._post_pending()

    def _process(self, job: ProcessingJob) -> None:
        app = self.app
        segment = job.segment
        if segment.duration < 0.25:
            return
        online = job.mode == "online"
        if online and segment.channel == "mic":
            src_lang, dst_lang = "zh", "en"
            forced, auto = job.language, False
            column, tag = "left", "麦克风 · 中文 → 英文"
        elif online:
            src_lang, dst_lang = "en", "zh"
            forced, auto = job.language, False
            column, tag = "right", "系统声音 · 英文 → 中文"
        else:
            auto = job.language == "auto"
            src_lang = "en" if job.language == "en" else "zh"
            dst_lang = "zh" if src_lang == "en" else "en"
            forced = None if auto else src_lang
            column = "left" if dst_lang == "en" else "right"
            tag = "麦克风 · 中文 → 英文" if dst_lang == "en" else "麦克风 · 英文 → 中文"

        def transcript(text: str, src: str, dst: str) -> None:
            if job.session != app.session:
                return
            app.window.post(
                {
                    "type": "draft",
                    "session": job.session,
                    "column": "left" if dst == "en" else "right",
                    "text": text,
                }
            )

        app.window.post(
            {
                "type": "processing",
                "session": job.session,
                "text": "正在识别刚才的发言…",
            }
        )

        result = app.engine.process(
            segment.samples,
            src_lang,
            dst_lang,
            forced_asr_language=forced,
            auto_detect=auto,
            on_transcript=transcript,
        )
        if job.session != app.session or self._stop_event.is_set():
            return
        if len(result.source_text.strip()) < int(
            app.settings.get("pipeline", {}).get("min_chars", 1)
        ):
            app.window.post(
                {
                    "type": "processing",
                    "session": job.session,
                    "text": "刚才的声音未能可靠识别，请靠近麦克风再说一遍。",
                }
            )
            return

        if auto:
            column = "left" if result.dst_lang == "en" else "right"
            tag = (
                "麦克风 · 中文 → 英文"
                if result.dst_lang == "en"
                else "麦克风 · 英文 → 中文"
            )

        latency = time.time() - segment.ended_at
        app.window.post(
            {
                "type": "segment",
                "session": job.session,
                "payload": {
                    "column": column,
                    "source": result.source_text,
                    "translation": result.translated_text,
                    "src_lang": result.src_lang,
                    "dst_lang": result.dst_lang,
                    "terms": result.terms,
                    "latency": latency,
                    "timestamp": time.time(),
                    "channel_tag": tag,
                    "engine": result.engine,
                },
            }
        )
        logger.info(
            "segment %s %.1fs  asr=%.2fs llm=%.2fs  %s",
            segment.channel,
            segment.duration,
            result.asr_seconds,
            result.llm_seconds,
            result.engine,
        )
        app.window.post(
            {
                "type": "engine",
                "state": f"READY · {result.total_seconds:.1f}s",
            }
        )
        app.window.post({"type": "processing", "session": job.session, "text": ""})
        if (
            result.dst_lang == "en"
            and app.tts.enabled
            and app._listening
            and result.engine != "term-align"
        ):
            speak_langs = app.settings.get("tts", {}).get("speak_languages", ["en"])
            if result.dst_lang in speak_langs:
                app.tts.say(result.translated_text, "en")


# ---------------------------------------------------------------------------
# Application controller
# ---------------------------------------------------------------------------
class StemTransApp:
    def __init__(self, settings: dict) -> None:
        self.settings = settings
        self.mode = "offline" if settings["ui"].get("mode") == "offline" else "online"
        self.session = 0
        self._listening = False
        self._accept_audio = False
        self._transitioning = False
        self._closing = False
        self._lock = threading.RLock()
        self._save_timer: threading.Timer | None = None
        self._capture_operation = threading.Lock()
        self._capture_error_message = ""
        self.glossary = Glossary.load(GLOSSARY_PATH)
        if len(self.glossary) == 0:
            logger.warning("glossary.json missing or empty: %s", GLOSSARY_PATH)

        tts_cfg = self.settings.get("tts", {})
        self.gate = TTSGate(cooldown_ms=int(tts_cfg.get("cooldown_ms", 350)))
        self.tts = TTSWorker(
            self.gate,
            rate=int(tts_cfg.get("rate", 185)),
            volume=float(tts_cfg.get("volume", 0.95)),
            max_pending=int(tts_cfg.get("max_pending", 2)),
            logger=lambda msg: logger.info("%s", msg),
        )
        self.tts.set_enabled(bool(tts_cfg.get("enabled", False)))

        self.engine = InferenceEngine(self.settings, self.glossary, APP_DIR)
        self.window = StemTransWindow(
            controller=self, settings=self.settings, glossary=self.glossary
        )
        self.capture = AudioCaptureManager(
            self.settings,
            self.gate,
            on_segment=self._on_segment,
            on_level=self._on_level,
            on_status=self._on_capture_status,
            on_error=self._on_capture_error,
        )
        self.worker = ProcessingWorker(self)
        self.worker.start()

        self.window.set_recording(False)
        self._refresh_devices()
        self._start_preload()

    # -- startup -------------------------------------------------------------
    def _start_preload(self) -> None:
        assets = self.engine.assets()
        missing = [name for name, (ok, _) in assets.items() if not ok]
        if missing:
            self.window.set_status(
                "模型缺失: "
                + ", ".join(missing)
                + " — 运行  python download_models.py  （详见 README）",
                "warn",
            )
            self.window.set_engine_state("MISSING")
            logger.warning("missing assets: %s", assets)
            if not assets["asr"][0]:
                # Without ASR there is nothing to preload; keep the clear
                # "missing model" status instead of overwriting it.
                return
        else:
            self.window.set_status("正在加载本地模型（CPU int8）…", "info")
            self.window.set_engine_state("LOADING")

        def on_status(message: str) -> None:
            logger.info("engine: %s", message)
            if message == "READY":
                self.window.post({"type": "engine", "state": "READY"})
                self.window.post(
                    {
                        "type": "status",
                        "kind": "ok",
                        "message": "模型已就绪。确认场景和翻译方向后，点击「开始监听」。",
                    }
                )
            elif message.startswith("ASR ERROR") or message.startswith("LLM ERROR"):
                self.window.post({"type": "engine", "state": "ERROR"})
                self.window.post(
                    {"type": "status", "kind": "warn", "message": message[:110]}
                )
            elif message.startswith("LLM MISSING"):
                self.window.post({"type": "engine", "state": "TERM-ALIGN"})
            else:
                self.window.post({"type": "engine", "state": message})

        self.engine.preload_async(on_status=on_status)

    def refresh_devices(self) -> None:
        """Public hook for the settings drawer's refresh button."""
        self._refresh_devices()

    def _refresh_devices(self) -> None:
        def work() -> None:
            try:
                devices = self.capture.list_devices()
            except Exception as exc:
                logger.warning("device enumeration failed: %s", exc)
                return
            saved_mic = str(self.settings["audio"].get("mic_device", ""))
            saved_loop = str(self.settings["audio"].get("loopback_device", ""))
            mic_device = next(
                (d for d in devices["input"] if d.name == saved_mic), None
            ) or next((d for d in devices["input"] if d.is_default), None)
            loop_device = next(
                (d for d in devices["loopback"] if d.name == saved_loop), None
            ) or next((d for d in devices["loopback"] if d.is_default), None)

            self.window.post(
                {
                    "type": "devices",
                    "mic": [d.label for d in devices["input"]],
                    "loopback": [d.label for d in devices["loopback"]],
                    "mic_value": mic_device.label if mic_device else "",
                    "loopback_value": loop_device.label if loop_device else "",
                }
            )
            logger.info(
                "devices: %d input, %d loopback",
                len(devices["input"]),
                len(devices["loopback"]),
            )

        threading.Thread(target=work, name="stemtrans-devices", daemon=True).start()

    # -- audio callbacks -----------------------------------------------------
    def _on_segment(self, segment: AudioSegment) -> None:
        if not self._accept_audio or self._closing:
            return
        if self.gate.is_blocked():
            # A final guard: frames captured before the gate closed must never
            # reach the recogniser.
            return
        self.worker.submit(segment)

    def _on_level(self, channel: str, level: float) -> None:
        self.window.post({"type": "level", "channel": channel, "level": level})
        self.window.post({"type": "gate", "blocked": self.gate.is_blocked()})

    def _on_capture_status(self, channel: str, message: str) -> None:
        logger.info("[%s] %s", channel, message)

    def _on_capture_error(self, channel: str, message: str) -> None:
        logger.error("[%s] %s", channel, message)
        self._capture_error_message = message
        self.window.post(
            {
                "type": "status",
                "kind": "error",
                "message": f"{channel.upper()}: {message}",
            }
        )
        if self._listening:
            self.window.post({"type": "capture_failure"})

    # -- controller API used by the window -----------------------------------
    def start_listening(self) -> None:
        with self._lock:
            if self._listening or self._closing or self._transitioning:
                return
            if self.worker.queue.unfinished_tasks:
                self.window.set_status(
                    "正在处理已收到的语音，完成后即可继续监听。", "info"
                )
                return
            if not self.engine.asr_ready or self.engine.status.startswith("LOADING"):
                self.window.post(
                    {
                        "type": "status",
                        "kind": "error",
                        "message": "识别模型尚未就绪，请等待加载；加载失败时查看下方提示。",
                    }
                )
                self.window.post({"type": "recording", "value": False})
                return
            mode = self.mode
            mic = clean_device_label(self.window.mic_var.get())
            loop = clean_device_label(self.window.loop_var.get())
            self.settings["audio"]["mic_device"] = mic
            self.settings["audio"]["loopback_device"] = loop
            self._schedule_save()
            self.session += 1
            self.worker.clear()
            self.window.begin_session(self.session)
            self._listening = True
            self._accept_audio = True
            self._transitioning = True
            self._capture_error_message = ""
            self.window.set_transition("正在打开麦克风…")
            self.window.set_engine_state(
                "READY" if self.engine.asr_ready else "LOADING"
            )

        def work() -> None:
            with self._capture_operation:
                try:
                    ok = (
                        self.capture.start(mode, mic, loop)
                        if self._listening
                        else False
                    )
                except Exception:
                    logger.error("capture start failed\n%s", traceback.format_exc())
                    ok = False
            with self._lock:
                cancelled = not self._listening or self._closing
                if not cancelled:
                    self._listening = bool(ok)
                    self._accept_audio = self._listening
                    self._transitioning = False
            if ok and not cancelled:
                self.window.post({"type": "recording", "value": True})
                self.window.post(
                    {
                        "type": "status",
                        "kind": "ok",
                        "message": "正在收音。自然说话，停顿约 1 秒后生成原文和译文。",
                    }
                )
                logger.info("capture started (%s)", mode)
            elif not cancelled:
                self.window.post({"type": "recording", "value": False})
                self.window.post(
                    {
                        "type": "status",
                        "kind": "error",
                        "message": self._capture_error_message
                        or "无法打开音频设备，请检查麦克风/系统声音设备选择",
                    }
                )

        threading.Thread(target=work, name="stemtrans-start", daemon=True).start()

    def stop_listening(self, reason: str = "") -> None:
        if reason:
            self._capture_error_message = reason
        with self._lock:
            if not self._listening:
                if not self._transitioning:
                    self.window.post({"type": "recording", "value": False})
                return
            self._listening = False
        self.window.set_recording(False)
        self._transitioning = True
        self.tts.interrupt()
        self.window.set_transition("正在结束收音，保留最后一句…")

        def work() -> None:
            with self._capture_operation:
                self.capture.stop()
            self._accept_audio = False
            self._transitioning = False
            self.window.post({"type": "recording", "value": False})
            self.window.post(
                {
                    "type": "status",
                    "kind": "error" if self._capture_error_message else "info",
                    "message": (
                        "已停止收音：" + self._capture_error_message
                        if self._capture_error_message
                        else "已停止收音；已收到的语音会继续处理，完成后可以再次开始。"
                    ),
                }
            )

        threading.Thread(target=work, name="stemtrans-stop", daemon=True).start()

    def set_mode(self, mode: str) -> None:
        if self._listening or self._transitioning:
            return
        mode = "offline" if mode == "offline" else "online"
        self.mode = mode
        self.settings["ui"]["mode"] = mode
        self._schedule_save()

    def set_mic_device(self, label: str) -> None:
        if self._listening or self._transitioning:
            return
        self.settings["audio"]["mic_device"] = clean_device_label(label)
        self._schedule_save()

    def set_loopback_device(self, label: str) -> None:
        if self._listening or self._transitioning:
            return
        self.settings["audio"]["loopback_device"] = clean_device_label(label)
        self._schedule_save()

    def set_language(self, language: str) -> None:
        if self._listening or self._transitioning:
            return
        self.settings["asr"]["language_single"] = (
            language if language in ("zh", "en", "auto") else "zh"
        )
        self._schedule_save()

    def set_pause(self, milliseconds: int) -> None:
        if self._listening or self._transitioning:
            return
        self.settings["audio"]["vad"]["min_silence_ms"] = max(
            700, min(2000, milliseconds)
        )
        self._schedule_save()

    def set_topmost(self, value: bool) -> None:
        self.settings["ui"]["always_on_top"] = bool(value)
        self._schedule_save()

    def set_opacity(self, value: float) -> None:
        self.settings["ui"]["opacity"] = max(0.5, min(1.0, float(value)))
        self._schedule_save()

    def set_tts_enabled(self, value: bool) -> None:
        self.tts.set_enabled(bool(value))
        self.settings["tts"]["enabled"] = False
        self._schedule_save()
        logger.info("TTS gate %s", "ARMED" if value else "disarmed")

    def copy_text(self, text: str) -> None:
        logger.debug("copied %d chars", len(text))

    def _schedule_save(self) -> None:
        if self._save_timer is not None:
            self._save_timer.cancel()
        self._save_timer = threading.Timer(0.6, lambda: save_settings(self.settings))
        self._save_timer.daemon = True
        self._save_timer.start()

    # -- shutdown ------------------------------------------------------------
    def shutdown(self) -> None:
        started = time.perf_counter()
        if self._closing:
            return
        self._closing = True
        logger.info("shutdown requested")

        def mark(label: str) -> None:
            logger.info("shutdown %-9s %.3fs", label, time.perf_counter() - started)

        with self._lock:
            self._listening = False
        try:
            self.capture.shutdown()
        except Exception:
            pass
        mark("capture")
        try:
            self.worker.stop()
            self.worker.join(timeout=0.2)
        except Exception:
            pass
        mark("worker")
        try:
            self.tts.shutdown(timeout=0.25)
        except Exception:
            pass
        mark("tts")
        # NOTE: engine.close() / gc.collect() are intentionally skipped on the
        # exit path.  Releasing a 1.1 GB mmap or waiting for Pa_Terminate can
        # take far longer than the 1 s exit budget; os._exit(0) below returns
        # every handle to the OS instantly.
        if self._save_timer is not None:
            self._save_timer.cancel()
        save_settings(self.settings)
        mark("settings")
        elapsed = time.perf_counter() - started
        logger.info("clean exit in %.3fs", elapsed)
        for handler in logger.handlers:
            try:
                handler.flush()
            except Exception:
                pass
        try:
            self.window.destroy()
        except Exception:
            pass
        # Hard guarantee: no non-daemon audio/SAPI thread can keep the process.
        os._exit(0)

    def run(self) -> None:
        try:
            self.window.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()


# ---------------------------------------------------------------------------
# Self-test / device listing (no GUI, no models loaded)
# ---------------------------------------------------------------------------
def self_test() -> int:
    print("=" * 74)
    print("StemTrans-Portable self-test")
    print("=" * 74)
    print(f"python      : {sys.version.split()[0]} ({platform.architecture()[0]})")
    print(f"platform    : {platform.platform()}")
    print(f"app dir     : {APP_DIR}")
    print(f"HF_HOME     : {os.environ.get('HF_HOME')}")
    print(f"HF_ENDPOINT : {os.environ.get('HF_ENDPOINT')}")
    print(f"threads     : OMP={os.environ.get('OMP_NUM_THREADS')}")
    print("-" * 74)

    modules = [
        ("numpy", "numpy"),
        ("customtkinter", "customtkinter"),
        ("PIL", "PIL"),
        ("pyaudiowpatch", "pyaudiowpatch"),
        ("onnxruntime", "onnxruntime"),
        ("faster_whisper", "faster_whisper"),
        ("zhconv", "zhconv"),
        ("llama_cpp", "llama_cpp"),
        ("pyttsx3", "pyttsx3"),
        ("requests", "requests"),
    ]
    failures = 0
    for label, module in modules:
        try:
            __import__(module)
            print(f"[ ok ] {label}")
        except Exception as exc:
            failures += 1
            print(f"[MISS] {label}: {exc}")

    print("-" * 74)
    settings = load_settings()
    engine = InferenceEngine(settings, Glossary.load(GLOSSARY_PATH), APP_DIR)
    for name, (ok, path) in engine.assets().items():
        size = ""
        if ok and os.path.isfile(path):
            size = f"  {os.path.getsize(path) / 1e6:.1f} MB"
        elif ok and os.path.isdir(path):
            size = "  (directory)"
        print(f"[{' ok ' if ok else 'MISS'}] {name:<4} {path}{size}")
        if not ok:
            failures += 1

    print("-" * 74)
    try:
        devices = enumerate_devices()
        for kind in ("input", "loopback"):
            print(f"{kind}: {len(devices[kind])} endpoint(s)")
            for dev in devices[kind]:
                print(
                    f"   {dev.index:>3}  {dev.label}  ({dev.channels} ch @ {dev.sample_rate} Hz)"
                )
    except Exception as exc:
        print(f"[warn] audio enumeration failed: {exc}")

    print("=" * 74)
    if failures:
        print(
            f"{failures} issue(s) found. Run:  python download_models.py --source china"
        )
        return 1
    print("All checks passed. Launch with:  run_app.bat")
    return 0


def list_devices() -> int:
    devices = enumerate_devices()
    for kind in ("input", "loopback"):
        print(f"\n[{kind}]")
        for dev in devices[kind]:
            print(
                f"  {dev.index:>3}  {dev.label}  ({dev.channels} ch @ {dev.sample_rate} Hz)"
            )
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="StemTrans-Portable",
        description="100% local CPU bilingual interpreter and English teleprompter",
    )
    parser.add_argument("--mode", choices=["online", "offline"], help="scene mode")
    parser.add_argument("--source", choices=["china", "global"], help="model mirror")
    parser.add_argument("--whisper", choices=["base", "small"], help="ASR size")
    parser.add_argument("--tts", dest="tts", action="store_true", help="enable TTS")
    parser.add_argument(
        "--no-tts", dest="tts", action="store_false", help="disable TTS"
    )
    parser.set_defaults(tts=None)
    parser.add_argument("--geometry", default=None, help="Tk geometry override")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.debug:
        logger.setLevel(logging.DEBUG)

    settings = load_settings()
    source = args.source or str(settings.get("network", {}).get("source", "china"))
    source = configure_network(source)
    settings["network"]["source"] = source

    if args.self_test:
        return self_test()
    if args.list_devices:
        return list_devices()

    if args.mode:
        settings["ui"]["mode"] = args.mode
    if args.whisper:
        settings["asr"]["model_size"] = args.whisper
        settings["asr"]["model_dir"] = f"models/whisper-{args.whisper}"
    if args.tts is not None:
        settings["tts"]["enabled"] = bool(args.tts)

    os.environ["HF_ENDPOINT"] = (
        "https://hf-mirror.com" if source == "china" else "https://huggingface.co"
    )
    logger.info(
        "StemTrans starting: source=%s mode=%s whisper=%s tts=%s",
        source,
        settings["ui"].get("mode"),
        settings["asr"].get("model_dir"),
        settings["tts"].get("enabled"),
    )

    # SIGINT (Ctrl+C in the console) must go through the same clean path.
    app: StemTransApp | None = None

    def _sigint(_signum, _frame):
        if app is not None:
            app.shutdown()

    try:
        signal.signal(signal.SIGINT, _sigint)
    except Exception:
        pass

    try:
        app = StemTransApp(settings)
    except Exception:
        logger.error("fatal startup error\n%s", traceback.format_exc())
        print(
            "\n无法启动。请检查是否已激活 stemtrans 环境并运行过 setup_conda_env.bat。\n"
            "Run  python main.py --self-test  for details.\n",
            file=sys.stderr,
        )
        return 1

    if args.geometry:
        try:
            app.window.geometry(args.geometry)
        except Exception:
            pass

    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
