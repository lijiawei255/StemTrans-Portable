"""StemTrans-Portable core package.

Modules
-------
audio_capture
    WASAPI shared-mode capture (microphone + loopback), streaming resampling,
    Silero-VAD ONNX segmentation and the TTS anti-feedback gate consumer.
inference_engine
    Faster-Whisper int8 ASR, Qwen2.5 GGUF translation through llama.cpp and
    the mechatronics glossary prompt builder.
tts_worker
    Optional offline SAPI5 voice output plus the global TTSGate mutex.
"""

__all__ = ["audio_capture", "inference_engine", "tts_worker"]
__version__ = "1.0.0"
