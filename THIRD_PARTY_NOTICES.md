# Third-party components

StemTrans source is MIT licensed, copyright 2026 JIAWEI LI. The portable
distribution also contains independent third-party software and model weights.
Their respective licenses apply; see `licenses/` and the version/hash inventory
in `build-manifest.json`.

Models are distributed without changes to their upstream weight files:

| Component | Upstream | License |
| --- | --- | --- |
| Silero VAD v5.1.2 | https://github.com/snakers4/silero-vad/tree/v5.1.2 | MIT |
| Whisper base, CTranslate2 conversion | https://huggingface.co/Systran/faster-whisper-base | MIT (OpenAI / SYSTRAN) |
| Qwen2.5-1.5B-Instruct Q4_K_M | https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF | Apache-2.0 |

Python, Tcl/Tk, CustomTkinter, llama-cpp-python / llama.cpp, Faster-Whisper,
CTranslate2, ONNX Runtime, NumPy, PyAudioWPatch / PortAudio, Pillow, SAPI bindings,
and their dependencies retain their upstream notices in `licenses/`.
The build inventory includes build tools as well as runtime packages; it does
not imply every installed package is loaded by the application.

PyAV includes dynamically linked FFmpeg and codec libraries. FFmpeg's upstream
Windows build reports LGPL-3.0-or-later, and also links x264/x265, whose GPL
licenses apply to those components and combinations where required. The
upstream build patches and complete codec sources are supplied as well.
These libraries are not modified by StemTrans.
Replacement compatible libraries may be used in `_internal/av.libs`.
Reverse engineering for debugging modifications to these LGPL libraries is
permitted. The matching source and build scripts are provided in the release's
third-party source archive; upstream build provenance and license texts are
included in `licenses/ffmpeg/`.

Unmodified MPL components (including pyttsx3, certifi and tqdm's applicable
portions) retain their notices. Corresponding source is also provided in the
third-party source archive. StemTrans's source tag includes its build scripts.
