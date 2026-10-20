"""Opt-in checks of the actual frozen executable. Never runs on normal launch."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
import traceback
import wave


def run_checks(app_module, fixture_dir, report_path=None, check_audio=False):
    import numpy as np

    from core.audio_capture import SileroVadOnnx, UtteranceSegmenter
    from core.inference_engine import Glossary, InferenceEngine, _to_simplified

    report = {
        "frozen": bool(getattr(sys, "frozen", False)),
        "checks": [],
        "passed": False,
    }
    destination = Path(report_path or Path(app_module.LOG_DIR) / "runtime-check.json")
    fixtures = Path(fixture_dir).resolve()
    started = time.monotonic()

    def save():
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def passed(name, **details):
        report["checks"].append({"name": name, **details})
        save()

    def require(condition, message):
        if not condition:
            raise RuntimeError(message)

    try:
        # Use distributable defaults, not developer device selections.
        settings = app_module.deep_merge(
            app_module.DEFAULT_SETTINGS,
            json.loads(Path(app_module.SETTINGS_PATH).read_text(encoding="utf-8")),
        )
        settings["tts"]["enabled"] = False
        require(_to_simplified("麥克風與英文😀") == "麦克风与英文😀", "Windows NLS")
        engine = InferenceEngine(settings, Glossary.load(app_module.GLOSSARY_PATH), app_module.APP_DIR)
        engine.preload(on_status=lambda message: app_module.logger.info("verify: %s", message))
        require(engine.status == "READY", f"models not ready: {engine.status}")
        passed("models_loaded", seconds=round(time.monotonic() - started, 2))
        for language in ("zh", "en"):
            with wave.open(str(fixtures / f"regression_{language}.wav"), "rb") as handle:
                require(
                    (handle.getframerate(), handle.getnchannels(), handle.getsampwidth()) == (16000, 1, 2),
                    "fixtures must be 16 kHz mono PCM16",
                )
                samples = np.frombuffer(handle.readframes(handle.getnframes()), np.int16).astype(np.float32) / 32768
            samples = np.concatenate((np.zeros(8000, np.float32), samples, np.zeros(24000, np.float32)))
            vad = settings["audio"]["vad"]
            segmenter = UtteranceSegmenter(
                SileroVadOnnx(str(Path(app_module.APP_DIR) / vad["model_path"])),
                **{key: vad[key] for key in ("threshold", "min_silence_ms", "min_speech_ms", "speech_pad_ms", "max_utterance_s")},
            )
            turns = []
            for index in range(0, len(samples), 320):
                turns.extend(segmenter.push(samples[index:index + 320], index / 16000))
            tail = segmenter.flush()
            if tail is not None:
                turns.append(tail)
            require(len(turns) == 1, f"{language}: expected one complete utterance, got {len(turns)}")
            result = engine.process(turns[0][0], language, "en" if language == "zh" else "zh", forced_asr_language=language)
            require(bool(result.source_text and result.translated_text), f"{language}: empty result")
            require(result.engine != "term-align", "LLM translation fell back")
            keywords = ("麦克风", "英文") if language == "zh" else ("microphone", "sentence")
            require(all(word in result.source_text.lower() for word in keywords), f"truncated recognition: {result.source_text}")
            passed(f"speech_{language}", source=result.source_text, translation=result.translated_text,
                   seconds=round(result.total_seconds, 2), duration=round(len(turns[0][0]) / 16000, 2))
        for label, audio in (
            ("silence", np.zeros(32000, np.float32)),
            ("quiet_noise", np.random.default_rng(42).normal(0, 0.002, 32000).astype(np.float32)),
        ):
            require(not engine.asr.transcribe(audio, language="zh").text, f"false recognition: {label}")
            passed(label)
        if check_audio:
            _check_audio(app_module, fixtures, engine, require, passed)
        _check_window(app_module, settings, check_audio, require, passed)
        report.update(passed=True, seconds=round(time.monotonic() - started, 2))
    except Exception:
        report["error"] = traceback.format_exc()
        app_module.logger.exception("runtime verification failed")
    save()
    # Same bounded process exit as the GUI: native audio/model teardown must
    # not keep a windowless diagnostic process alive after its report is saved.
    for handler in app_module.logger.handlers:
        handler.flush()
    os._exit(0 if report["passed"] else 1)


def _check_audio(app_module, fixtures, engine, require, passed):
    import pyaudiowpatch as pyaudio
    import winsound

    from core.audio_capture import ChannelCapture, enumerate_devices
    from core.tts_worker import TTSGate, TTSWorker

    pa = pyaudio.PyAudio()
    devices = enumerate_devices(pa)
    require(bool(devices["input"] and devices["loopback"]), "microphone or loopback unavailable")
    target = next((device for device in devices["loopback"] if device.is_default), devices["loopback"][0])
    segments, errors = [], []
    worker = ChannelCapture(
        pa, target, "loopback", gate=TTSGate(), on_segment=segments.append,
        on_level=lambda *args: None, on_status=lambda *args: None,
        on_error=lambda *args: errors.append(args),
    )
    try:
        worker.start()
        require(worker.opened.wait(5) and not worker.open_error, worker.open_error or "loopback open timeout")
        winsound.PlaySound(str(fixtures / "regression_en.wav"), winsound.SND_FILENAME | winsound.SND_ASYNC)
        deadline = time.monotonic() + 18
        while time.monotonic() < deadline and not segments and not errors:
            time.sleep(0.1)
        require(bool(segments) and not errors, f"loopback did not deliver final sentence: {errors}")
    finally:
        winsound.PlaySound(None, 0)
        worker.stop()
        worker.join(2)
    require(not worker.is_alive(), "loopback did not stop")
    source = " ".join(engine.asr.transcribe(segment.samples, language="en").text for segment in segments)
    require("microphone" in source.lower() and "sentence" in source.lower(), source)
    passed("loopback_tail_before_stop", source=source)
    gate = TTSGate()
    speaker = TTSWorker(gate)
    speaker.set_enabled(True)
    speaker.say("This is the portable application speech check. " * 6, "en")
    deadline = time.monotonic() + 8
    while not gate.is_blocked() and time.monotonic() < deadline:
        time.sleep(0.02)
    require(gate.is_blocked(), "SAPI did not start")
    time.sleep(0.25)
    speaker.set_enabled(False)
    deadline = time.monotonic() + 2
    while gate.is_blocked() and time.monotonic() < deadline:
        time.sleep(0.02)
    require(not gate.is_blocked(), "SAPI cancellation did not release capture")
    speaker.shutdown()
    passed("sapi_interrupt_and_cooldown")


def _check_window(app_module, settings, check_audio, require, passed):
    settings["ui"].update(mode="online", always_on_top=False)
    # Keep defaults pristine and let the caller persist its report after the
    # window closes, instead of using production shutdown's immediate exit.
    original_save = app_module.save_settings
    app_module.save_settings = lambda value: None
    app = app_module.StemTransApp(settings)
    errors = []
    state = {"step": 0, "at": time.monotonic()}

    def finish(error=None):
        if error:
            errors.append(str(error))
        app._closing = True
        app.capture.shutdown()
        app.worker.stop()
        app.tts.shutdown()
        if app._save_timer is not None:
            app._save_timer.cancel()
        app.window.destroy()

    app.window.report_callback_exception = lambda *args: finish(args)

    def tick():
        try:
            require(time.monotonic() - state["at"] < 45, "GUI lifecycle timeout")
            if state["step"] == 0 and app.engine.status == "READY":
                app.window.set_settings_open(True)
                app.window.update_idletasks()
                app.window.set_settings_open(False)
                passed("gui_conversation_and_settings")
                if not check_audio:
                    finish()
                    return
                app.start_listening()
                state["step"] = 1
            elif state["step"] == 1 and app._listening and not app._transitioning and app.window._recording:
                require(app.capture.active_channels == ["mic", "loopback"], "online channels")
                app.stop_listening()
                state["step"] = 2
            elif state["step"] == 2 and not app._transitioning and not app.worker.queue.unfinished_tasks:
                require(not app.capture.running and not app.window._recording, "stop did not complete")
                app.set_mode("offline")
                app.window.set_mode("offline", notify=False)
                app.start_listening()
                state["step"] = 3
            elif state["step"] == 3 and app._listening and not app._transitioning and app.window._recording:
                require(app.capture.active_channels == ["mic"], "single mic channels")
                passed("gui_start_stop_mode_restart")
                finish()
                return
        except Exception as exc:
            finish(exc)
            return
        app.window.after(80, tick)

    app.window.after(80, tick)
    app.window.mainloop()
    app_module.save_settings = original_save
    require(not errors, f"GUI errors: {errors}")
