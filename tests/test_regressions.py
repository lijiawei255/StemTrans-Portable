"""Regression tests for lost speech, stale sessions and ASR hallucination guards.

Run with the stemtrans environment: python -m unittest discover -s tests -v
No audio devices, microphone recordings or model downloads are needed.
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

import main
import download_models
from core.audio_capture import (
    AudioSegment,
    SileroVadOnnx,
    StreamingResampler,
    UtteranceSegmenter,
    VAD_FRAME,
)
from core.inference_engine import (
    ASREngine,
    Glossary,
    TranslationResult,
    _detect_language,
)
from core.tts_worker import TTSGate, TTSWorker


class SequenceVad:
    def __init__(self, probabilities):
        self.probabilities = iter(probabilities)

    def __call__(self, frame):
        return next(self.probabilities)

    def reset(self):
        pass


def feed(probabilities, **kwargs):
    segmenter = UtteranceSegmenter(SequenceVad(probabilities), **kwargs)
    audio = np.tile(
        np.sin(np.arange(VAD_FRAME) / 10).astype(np.float32), len(probabilities)
    )
    return segmenter, segmenter.push(audio, now=len(audio) / 16000)


class SegmentationTests(unittest.TestCase):
    def test_silero_receives_context_and_resets_it(self):
        vad = object.__new__(SileroVadOnnx)
        vad._state_shape = [2, 1, 128]
        vad._input_names = ["input", "state", "sr"]
        vad._output_names = ["output", "stateN"]
        vad.session = Mock()
        vad.session.run.return_value = [
            np.array([[0.8]]),
            np.zeros((2, 1, 128), np.float32),
        ]
        vad.reset()
        first = np.arange(512, dtype=np.float32)
        vad(first)
        inputs = vad.session.run.call_args.args[1]["input"]
        self.assertEqual(inputs.shape, (1, 576))
        np.testing.assert_array_equal(inputs[0, :64], 0)
        vad(np.ones(512, dtype=np.float32))
        inputs = vad.session.run.call_args.args[1]["input"]
        np.testing.assert_array_equal(inputs[0, :64], first[-64:])
        vad.reset()
        np.testing.assert_array_equal(vad._context, 0)

    def test_thinking_pause_does_not_split_sentence(self):
        _, segments = feed([0.9] * 40 + [0.0] * 24 + [0.9] * 40 + [0.0] * 40)
        self.assertEqual(len(segments), 1)  # 768 ms pause, one utterance
        self.assertGreater(len(segments[0][0]) / 16000, 3.3)

    def test_quiet_syllables_survive_hysteresis(self):
        _, segments = feed([0.9] * 10 + [0.4] * 50 + [0.0] * 40)
        self.assertEqual(len(segments), 1)
        self.assertGreater(len(segments[0][0]) / 16000, 1.9)

    def test_twelve_second_sentence_is_not_cut_at_seven_seconds(self):
        _, segments = feed([0.9] * 380 + [0.0] * 40)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0][1], "silence")

    def test_limit_carries_overlap_without_dropping_audio(self):
        segmenter, segments = feed([0.9] * 100, max_utterance_s=2)
        segments.append(segmenter.flush())
        self.assertEqual(segments[0][1], "max_length")
        self.assertGreaterEqual(sum(len(s[0]) for s in segments), 100 * VAD_FRAME)

    def test_stop_flush_retains_partial_frame(self):
        segmenter, results = feed([0.9] * 20)
        self.assertFalse(results)
        segmenter.push(np.ones(127, dtype=np.float32), 1)
        tail = segmenter.flush()
        self.assertEqual(len(tail[0]), 20 * VAD_FRAME + 127)

    def test_click_is_not_a_sentence(self):
        _, segments = feed([0.9] * 3 + [0.0] * 50)
        self.assertFalse(segments)

    def test_resampler_is_independent_of_callback_boundaries(self):
        for rate in (44100, 48000):
            audio = np.sin(np.arange(rate) * 2 * np.pi * 440 / rate).astype(np.float32)
            whole = StreamingResampler(rate).process(audio)
            stream = StreamingResampler(rate)
            chunks = np.concatenate(
                [stream.process(audio[i : i + 257]) for i in range(0, rate, 257)]
            )
            self.assertEqual(len(whole), len(chunks))
            np.testing.assert_allclose(whole, chunks, atol=1e-5)
            self.assertLessEqual(abs(len(chunks) - 16000), 1)


class RecognitionTests(unittest.TestCase):
    def recognizer(self, segments):
        engine = ASREngine({}, Glossary([]), ".")
        engine._loaded = True
        engine.model = Mock()
        engine.model.transcribe.return_value = (
            iter(segments),
            SimpleNamespace(language="zh", language_probability=0.99),
        )
        return engine

    def test_reject_noise_and_repetitive_decoder_outputs(self):
        engine = self.recognizer(
            [
                SimpleNamespace(
                    text="thanks for watching",
                    no_speech_prob=0.91,
                    avg_logprob=-0.3,
                    compression_ratio=1,
                ),
                SimpleNamespace(
                    text="unsupported",
                    no_speech_prob=0.1,
                    avg_logprob=-1.5,
                    compression_ratio=1,
                ),
                SimpleNamespace(
                    text="repeat",
                    no_speech_prob=0.1,
                    avg_logprob=-0.2,
                    compression_ratio=3,
                ),
                SimpleNamespace(
                    text="你好",
                    no_speech_prob=0.1,
                    avg_logprob=-0.2,
                    compression_ratio=1,
                ),
            ]
        )
        result = engine.transcribe(np.ones(16000, np.float32), language="zh")
        self.assertEqual(result.text, "你好")
        options = engine.model.transcribe.call_args.kwargs
        self.assertEqual(options["task"], "transcribe")
        self.assertIsNone(options["initial_prompt"])
        self.assertEqual(options["language"], "zh")

    def test_technical_chinese_routes_to_english(self):
        self.assertEqual(_detect_language("我们用 STM32 通过 SPI 读取 ADC", "en"), "zh")
        self.assertEqual(_detect_language("The ADC reads the signal", "en"), "en")


class SettingsTests(unittest.TestCase):
    def test_local_preferences_override_defaults_without_changing_public_file(self):
        with tempfile.TemporaryDirectory() as directory:
            public = Path(directory) / "settings.json"
            local = Path(directory) / "settings.local.json"
            public.write_text(
                '{"version":2,"audio":{"mic_device":""}}', encoding="utf-8"
            )
            original = public.read_bytes()
            local.write_text(
                '{"audio":{"mic_device":"Private microphone"}}', encoding="utf-8"
            )
            with (
                patch.object(main, "SETTINGS_PATH", str(public)),
                patch.object(main, "LOCAL_SETTINGS_PATH", str(local)),
            ):
                settings = main.load_settings()
                self.assertEqual(settings["audio"]["mic_device"], "Private microphone")
                self.assertEqual(settings["audio"]["vad"]["min_silence_ms"], 1100)
                settings["ui"]["opacity"] = 0.8
                main.save_settings(settings)
                self.assertEqual(main.load_settings()["ui"]["opacity"], 0.8)
            self.assertEqual(public.read_bytes(), original)
            self.assertFalse(Path(str(local) + ".tmp").exists())

    def test_missing_local_file_uses_shareable_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            public = Path(directory) / "settings.json"
            public.write_text(
                '{"version":2,"audio":{"mic_device":""}}', encoding="utf-8"
            )
            with (
                patch.object(main, "SETTINGS_PATH", str(public)),
                patch.object(
                    main, "LOCAL_SETTINGS_PATH", str(Path(directory) / "missing.json")
                ),
            ):
                settings = main.load_settings()
                self.assertEqual(settings["audio"]["mic_device"], "")
                self.assertFalse(settings["tts"]["enabled"])

    def test_downloader_preserves_local_device_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / "settings.local.json"
            local.write_text(
                '{"audio":{"mic_device":"Private microphone"}}', encoding="utf-8"
            )
            with patch.object(download_models, "SETTINGS", str(local)):
                download_models.save_source("global")
            saved = json.loads(local.read_text(encoding="utf-8"))
            self.assertEqual(saved["audio"]["mic_device"], "Private microphone")
            self.assertEqual(saved["network"]["source"], "global")


class PipelineTests(unittest.TestCase):
    def test_device_error_during_startup_requests_stop(self):
        app = self.app()
        app._transitioning = True
        main.StemTransApp._on_capture_error(app, "mic", "device failed")
        self.assertTrue(any(e["type"] == "capture_failure" for e in app.window.events))

    def app(self):
        window = SimpleNamespace(
            events=[], post=lambda event: window.events.append(event)
        )
        app = SimpleNamespace(
            settings=copy.deepcopy(main.DEFAULT_SETTINGS),
            session=1,
            mode="offline",
            window=window,
            _listening=True,
            tts=Mock(enabled=False),
            engine=Mock(),
        )
        app.engine.process.return_value = TranslationResult(
            "你好", "Hello", "zh", "en", "qwen"
        )
        return app

    def segment(self):
        return AudioSegment("mic", np.zeros(16000, np.float32), 16000, 0, 1, 0.1)

    def test_queue_overload_preserves_already_accepted_speech_and_reports_loss(self):
        app = self.app()
        app.settings["pipeline"]["queue_size"] = 1
        worker = main.ProcessingWorker(app)
        first = self.segment()
        worker.submit(first)
        worker.submit(self.segment())
        self.assertIs(worker.queue.get_nowait().segment, first)
        self.assertTrue(any(e["type"] == "overload" for e in app.window.events))

    def test_routing_is_snapshotted_at_capture_not_processing_time(self):
        app = self.app()
        app.settings["asr"]["language_single"] = "zh"
        worker = main.ProcessingWorker(app)
        worker.submit(self.segment())
        app.mode = "online"
        app.settings["asr"]["language_single"] = "en"
        worker._process(worker.queue.get_nowait())
        self.assertEqual(
            app.engine.process.call_args.kwargs["forced_asr_language"], "zh"
        )
        self.assertFalse(app.engine.process.call_args.kwargs["auto_detect"])

    def test_new_session_discards_inflight_result_and_voice(self):
        app = self.app()
        app.tts.enabled = True

        def finish(*args, **kwargs):
            app.session += 1
            return TranslationResult("旧内容", "Old content", "zh", "en", "qwen")

        app.engine.process.side_effect = finish
        worker = main.ProcessingWorker(app)
        worker.submit(self.segment())
        worker._process(worker.queue.get_nowait())
        self.assertFalse(any(e["type"] == "segment" for e in app.window.events))
        app.tts.say.assert_not_called()

    def test_stop_keeps_tail_but_does_not_speak(self):
        app = self.app()
        app._listening = False
        app._accept_audio = True
        app._closing = False
        app.gate = TTSGate()
        app.tts.enabled = True
        app.worker = main.ProcessingWorker(app)
        main.StemTransApp._on_segment(app, self.segment())
        self.assertEqual(app.worker.queue.qsize(), 1)
        app.worker._process(app.worker.queue.get_nowait())
        self.assertTrue(any(e["type"] == "segment" for e in app.window.events))
        app.tts.say.assert_not_called()

    def test_automatic_speech_never_restores_from_saved_setting(self):
        with (
            patch.object(main.os.path, "isfile", return_value=True),
            patch(
                "builtins.open",
                unittest.mock.mock_open(
                    read_data='{"tts":{"enabled":true},"version":1}'
                ),
            ),
        ):
            settings = main.load_settings()
        self.assertFalse(settings["tts"]["enabled"])
        self.assertEqual(settings["audio"]["vad"]["min_silence_ms"], 1100)
        self.assertEqual(settings["version"], 2)

    def test_tts_disable_requests_owner_thread_cancellation(self):
        tts = TTSWorker(TTSGate())
        tts.set_enabled(True)
        tts._queue.put(("hello", "en"))
        tts.set_enabled(False)
        self.assertTrue(tts._cancel_speech.is_set())
        self.assertTrue(tts._queue.empty())


if __name__ == "__main__":
    unittest.main()
