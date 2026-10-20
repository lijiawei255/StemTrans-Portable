"""CPU-only inference pipeline: Silero-guided Faster-Whisper + Qwen2.5 GGUF.

The module is deliberately import-safe: every heavy dependency (numpy,
faster_whisper, llama_cpp) is imported lazily inside the class that needs it.
That lets the GUI, the screenshot harness and ``--self-test`` run on a machine
where the models are not downloaded yet.

Glossary strategy
-----------------
1. **ASR stage** - glossary prompting is opt-in. The default transcribes
   without a terminology list so weak/short speech is less likely to echo it.
2. **LLM stage** - only the glossary pairs *relevant to the current sentence*
   (plus a core fallback set) are injected into the Qwen2.5 system prompt.
   This keeps the prompt short enough for a 1.5 B model on 4 CPU threads while
   still forcing a deterministic translation for domain vocabulary.
"""

from __future__ import annotations

import gc
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

LANG_NAMES = {"zh": "Simplified Chinese", "en": "English"}
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_PREFIX_RE = re.compile(
    r"^\s*(?:translation|translated text|译文|翻译|英文|中文|english|chinese)\s*[:：]\s*",
    re.IGNORECASE,
)
_FILLER_RE = re.compile(
    r"^\s*(?:sure|of course|certainly|here is|here's)\b[^:：\n]{0,40}[:：]\s*",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Glossary
# ---------------------------------------------------------------------------
class Glossary:
    """Bilingual term base loaded from ``config/glossary.json``."""

    def __init__(self, entries: Iterable[dict], meta: Optional[dict] = None) -> None:
        cleaned: list[dict] = []
        for item in entries or []:
            zh = str(item.get("zh", "")).strip()
            en = str(item.get("en", "")).strip()
            if not zh or not en:
                continue
            cleaned.append(
                {
                    "zh": zh,
                    "en": en,
                    "abbr": str(item.get("abbr", "")).strip(),
                }
            )
        self.entries = cleaned
        self.meta = meta or {}
        self._en_terms = self._unique(
            [e["en"] for e in cleaned] + [e["abbr"] for e in cleaned if e["abbr"]]
        )
        self._zh_terms = self._unique([e["zh"] for e in cleaned])
        self._en_patterns = [
            (
                term,
                re.compile(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", re.I),
            )
            for term in sorted(self._en_terms, key=len, reverse=True)
        ]

    @staticmethod
    def _unique(values: Iterable[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for value in values:
            key = value.lower()
            if value and key not in seen:
                seen.add(key)
                out.append(value)
        return out

    @classmethod
    def load(cls, path: str) -> "Glossary":
        if not os.path.isfile(path):
            return cls([])
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return cls(payload.get("entries", []), payload)

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def english_terms(self) -> list[str]:
        return list(self._en_terms)

    @property
    def chinese_terms(self) -> list[str]:
        return list(self._zh_terms)

    # -- prompts -------------------------------------------------------------
    def asr_initial_prompt(self, max_chars: int = 780) -> str:
        """Compact English term list for Whisper's decoder prefix."""
        terms: list[str] = []
        used = 0
        for term in self._en_terms:
            if used + len(term) + 2 > max_chars:
                break
            terms.append(term)
            used += len(term) + 2
        if not terms:
            return ""
        return (
            "Academic talk on mechatronics, embedded systems and control. "
            "Technical terms: " + ", ".join(terms) + "."
        )[:max_chars]

    def relevant_pairs(self, text: str, limit: int = 26) -> list[tuple[str, str]]:
        """Glossary rows that occur in *text*, padded with the core vocabulary."""
        low = text.lower()
        matched: list[tuple[str, str]] = []
        rest: list[tuple[str, str]] = []
        for entry in self.entries:
            pair = (entry["zh"], entry["en"])
            zh_hit = entry["zh"] in text
            en_hit = entry["en"].lower() in low
            abbr_hit = bool(entry["abbr"]) and re.search(
                rf"(?<![A-Za-z0-9]){re.escape(entry['abbr'])}(?![A-Za-z0-9])",
                text,
                re.IGNORECASE,
            )
            if zh_hit or en_hit or abbr_hit:
                matched.append(pair)
            else:
                rest.append(pair)
        if len(matched) < limit:
            matched.extend(rest[: limit - len(matched)])
        return matched[:limit]

    def system_prompt(self, src_lang: str, dst_lang: str, text: str) -> str:
        src_name = LANG_NAMES.get(src_lang, src_lang)
        dst_name = LANG_NAMES.get(dst_lang, dst_lang)
        pairs = self.relevant_pairs(text)
        if dst_lang == "en":
            block = "\n".join(f"- {zh} => {en}" for zh, en in pairs)
            direction = "Chinese => English"
        else:
            block = "\n".join(f"- {en} => {zh}" for zh, en in pairs)
            direction = "English => Chinese"

        return (
            "You are a professional simultaneous interpreter for a mechatronics "
            "research group (embedded hardware, control engineering, sensors, "
            "additive manufacturing).\n"
            f"Translate the user's utterance from {src_name} into {dst_name}.\n"
            "Rules:\n"
            "1. Output ONLY the translation. No quotes, no notes, no pinyin, "
            "no explanations, no language labels.\n"
            "2. Keep numbers, units, chip names, bus names and abbreviations "
            "unchanged (STM32, SPI, 20 kHz, 12 %, Q4_K_M ...).\n"
            "3. Use the terminology mapping below exactly when the concept appears.\n"
            "4. Spoken academic register: concise, natural, first person, "
            "one paragraph, suitable to be read aloud as a teleprompter.\n"
            "5. Never answer the content, never add information.\n"
            f"TERMINOLOGY ({direction}):\n{block}"
        )

    # -- matching ------------------------------------------------------------
    def match_english(self, text: str) -> list[str]:
        found: list[str] = []
        for term, pattern in self._en_patterns:
            if pattern.search(text):
                found.append(term)
        return found

    def match_chinese(self, text: str) -> list[str]:
        return [
            term
            for term in sorted(self._zh_terms, key=len, reverse=True)
            if term in text
        ]

    def match_display_terms(self, text: str, lang: str) -> list[str]:
        """Canonical glossary labels for the UI chips (longest first, deduped)."""
        if lang == "en":
            terms = self.match_english(text)
        else:
            terms = self.match_chinese(text)
        # Drop terms fully contained in a longer hit ("PID" inside "PID controller").
        out: list[str] = []
        for term in sorted(terms, key=len, reverse=True):
            if not any(term.lower() in other.lower() for other in out):
                out.append(term)
        return out[:8]

    def map_terms(self, text: str, src_lang: str) -> str:
        """Deterministic term substitution used by the offline fallback."""
        if src_lang == "zh":
            pairs = sorted(
                ((e["zh"], e["en"]) for e in self.entries),
                key=lambda p: len(p[0]),
                reverse=True,
            )
            for zh, en in pairs:
                text = text.replace(zh, f" {en} ")
        else:
            pairs = sorted(
                ((e["en"], e["zh"]) for e in self.entries),
                key=lambda p: len(p[0]),
                reverse=True,
            )
            for en, zh in pairs:
                text = re.sub(
                    rf"(?<![A-Za-z0-9]){re.escape(en)}(?![A-Za-z0-9])",
                    zh,
                    text,
                    flags=re.IGNORECASE,
                )
        return re.sub(r"\s{2,}", " ", text).strip()


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------
@dataclass
class AsrResult:
    text: str
    language: str
    language_probability: float
    audio_duration: float
    infer_seconds: float
    model: str = ""


@dataclass
class TranslationResult:
    source_text: str
    translated_text: str
    src_lang: str
    dst_lang: str
    engine: str
    terms: list[str] = field(default_factory=list)
    asr_seconds: float = 0.0
    llm_seconds: float = 0.0
    total_seconds: float = 0.0


class EngineNotReady(RuntimeError):
    """Raised when a required local model file is missing."""


# ---------------------------------------------------------------------------
# ASR
# ---------------------------------------------------------------------------
class ASREngine:
    """Faster-Whisper on CPU with int8 weights."""

    def __init__(self, cfg: dict, glossary: Glossary, root_dir: str) -> None:
        self.cfg = cfg or {}
        self.glossary = glossary
        self.root_dir = root_dir
        self.model = None
        self.model_label = ""
        self._lock = threading.RLock()
        self._loaded = False
        self._last_error = ""

    # -- paths ---------------------------------------------------------------
    def _model_dir(self) -> str:
        path = str(self.cfg.get("model_dir", "models/whisper-base"))
        return path if os.path.isabs(path) else os.path.join(self.root_dir, path)

    def local_model_path(self) -> Optional[str]:
        path = self._model_dir()
        if os.path.isfile(os.path.join(path, "model.bin")):
            return path
        return None

    def available(self) -> bool:
        return self.local_model_path() is not None

    # -- lifecycle -----------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            from faster_whisper import WhisperModel

            model_dir = self.local_model_path()
            if model_dir is None:
                raise EngineNotReady(
                    "Faster-Whisper 模型缺失 / missing model:\n"
                    f"  {self._model_dir()}\n"
                    "请先运行:  python download_models.py --source china"
                )

            compute_type = str(self.cfg.get("compute_type", "int8"))
            cpu_threads = int(self.cfg.get("cpu_threads", 4))
            try:
                self.model = WhisperModel(
                    model_dir,
                    device="cpu",
                    compute_type=compute_type,
                    cpu_threads=cpu_threads,
                    num_workers=1,
                    local_files_only=True,
                )
            except TypeError:
                # Older faster-whisper without local_files_only.
                self.model = WhisperModel(
                    model_dir,
                    device="cpu",
                    compute_type=compute_type,
                    cpu_threads=cpu_threads,
                    num_workers=1,
                )
            self.model_label = f"{os.path.basename(model_dir)}/{compute_type}"
            self._loaded = True
            self._last_error = ""

    def close(self) -> None:
        with self._lock:
            self.model = None
            self._loaded = False
            gc.collect()

    # -- inference -----------------------------------------------------------
    def transcribe(
        self,
        samples,
        language: Optional[str] = None,
        initial_prompt: Optional[str] = None,
    ) -> AsrResult:
        import numpy as np

        self.load()
        audio = np.asarray(samples, dtype=np.float32).reshape(-1)
        if audio.size == 0:
            return AsrResult("", language or "", 0.0, 0.0, 0.0, self.model_label)

        prompt = initial_prompt
        if prompt is None and self.cfg.get("use_glossary_prompt", False):
            prompt = self.glossary.asr_initial_prompt(
                int(self.cfg.get("initial_prompt_max_chars", 780))
            )

        started = time.perf_counter()
        segments, info = self.model.transcribe(
            audio,
            language=language,
            task="transcribe",
            beam_size=int(self.cfg.get("beam_size", 3)),
            best_of=1,
            patience=1.0,
            temperature=0.0,
            condition_on_previous_text=False,
            initial_prompt=prompt or None,
            vad_filter=False,
            without_timestamps=True,
            word_timestamps=False,
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4,
        )
        # VAD can admit clicks, breathing or noise. Do not turn uncertain
        # decoder output into a confident translation.
        accepted = [
            segment.text
            for segment in segments
            if float(getattr(segment, "no_speech_prob", 0.0)) < 0.6
            and float(getattr(segment, "avg_logprob", 0.0)) >= -1.0
            and float(getattr(segment, "compression_ratio", 1.0)) <= 2.4
        ]
        text = " ".join(accepted).strip()
        elapsed = time.perf_counter() - started

        text = re.sub(r"\s+", " ", text).strip()
        probability = float(getattr(info, "language_probability", 0.0) or 0.0)
        detected = str(getattr(info, "language", language or "") or "")
        if detected == "zh" or language == "zh":
            text = _to_simplified(text)
        return AsrResult(
            text=text,
            language=detected,
            language_probability=probability,
            audio_duration=len(audio) / 16000.0,
            infer_seconds=elapsed,
            model=self.model_label,
        )


# ---------------------------------------------------------------------------
# Translation backends
# ---------------------------------------------------------------------------
def _to_simplified(text: str) -> str:
    """Whisper usually emits Traditional Chinese; the glossary is Simplified."""
    if not text:
        return text
    try:
        from zhconv import convert

        return convert(text, "zh-cn")
    except Exception:
        return text


def _clean_translation(text: str, dst_lang: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    text = text.replace("<|im_end|>", "").replace("<|endoftext|>", "")
    text = text.split("\n\n")[0].strip()
    text = _FILLER_RE.sub("", text)
    text = _PREFIX_RE.sub("", text)
    text = text.strip().strip('"').strip("“”").strip("'").strip()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n+", " ", text)
    # Models sometimes wrap the answer in markdown bullets.
    text = re.sub(r"^\s*[-*]\s+", "", text)
    return text.strip()


def _language_ratio(text: str) -> float:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    cjk = sum(1 for ch in letters if _CJK_RE.match(ch))
    return cjk / len(letters)


class LlamaTranslator:
    """Qwen2.5 GGUF translator running on llama.cpp (CPU, mmap)."""

    def __init__(self, cfg: dict, glossary: Glossary, root_dir: str) -> None:
        self.cfg = cfg or {}
        self.glossary = glossary
        self.root_dir = root_dir
        self.model = None
        self._lock = threading.RLock()
        self._loaded = False

    def model_path(self) -> str:
        path = str(
            self.cfg.get("model_path", "models/qwen2.5-1.5b-instruct-q4_k_m.gguf")
        )
        return path if os.path.isabs(path) else os.path.join(self.root_dir, path)

    def available(self) -> bool:
        return os.path.isfile(self.model_path())

    def load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            path = self.model_path()
            if not os.path.isfile(path):
                raise EngineNotReady(
                    "Qwen2.5 GGUF 模型缺失 / missing model:\n"
                    f"  {path}\n"
                    "请先运行:  python download_models.py --source china --llm"
                )
            from llama_cpp import Llama

            self.model = Llama(
                model_path=path,
                n_ctx=int(self.cfg.get("n_ctx", 2048)),
                n_threads=int(self.cfg.get("n_threads", 4)),
                n_batch=int(self.cfg.get("n_batch", 256)),
                use_mmap=True,
                use_mlock=False,
                seed=42,
                verbose=False,
            )
            self._loaded = True

    def close(self) -> None:
        with self._lock:
            self.model = None
            self._loaded = False
            gc.collect()

    def translate(self, text: str, src_lang: str, dst_lang: str) -> tuple[str, float]:
        self.load()
        started = time.perf_counter()
        system = self.glossary.system_prompt(src_lang, dst_lang, text)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": text.strip()},
        ]
        max_tokens = int(self.cfg.get("max_tokens", 220))
        # Never let a long answer stall the live stream.
        max_tokens = max(48, min(max_tokens, 6 * len(text) + 48))
        try:
            output = self.model.create_chat_completion(
                messages=messages,
                max_tokens=max_tokens,
                temperature=float(self.cfg.get("temperature", 0.2)),
                top_p=float(self.cfg.get("top_p", 0.9)),
                repeat_penalty=float(self.cfg.get("repeat_penalty", 1.05)),
                stop=["<|im_end|>", "<|endoftext|>"],
            )
            content = output["choices"][0]["message"]["content"]
        except Exception:
            # Some GGUF builds reject an explicit stop token; retry plainly.
            output = self.model.create_chat_completion(
                messages=messages,
                max_tokens=max_tokens,
                temperature=float(self.cfg.get("temperature", 0.2)),
                top_p=float(self.cfg.get("top_p", 0.9)),
            )
            content = output["choices"][0]["message"]["content"]
        elapsed = time.perf_counter() - started
        return _clean_translation(str(content), dst_lang), elapsed

    def warmup(self) -> None:
        try:
            self.load()
            self.model.create_chat_completion(
                messages=[
                    {"role": "system", "content": "Translate Chinese to English."},
                    {"role": "user", "content": "你好"},
                ],
                max_tokens=8,
                temperature=0.0,
            )
        except Exception:
            pass


class FallbackTranslator:
    """Deterministic glossary-aware fallback when the GGUF is unavailable.

    This is *not* a neural translation: it maps every known term and keeps the
    rest of the sentence untouched.  It is explicitly labelled ``TERM-ALIGN`` in
    the UI so the user is never misled, while still being useful as a live
    terminology prompter during a talk.
    """

    engine_name = "term-align"

    def __init__(self, glossary: Glossary) -> None:
        self.glossary = glossary

    def available(self) -> bool:
        return True

    def load(self) -> None:
        return

    def close(self) -> None:
        return

    def translate(self, text: str, src_lang: str, dst_lang: str) -> tuple[str, float]:
        started = time.perf_counter()
        mapped = self.glossary.map_terms(text, src_lang)
        if src_lang == "zh" and dst_lang == "en":
            prefix = "[术语直译 / TERM-ALIGN] "
        elif src_lang == "en" and dst_lang == "zh":
            prefix = "[术语对照 / TERM-ALIGN] "
        else:
            prefix = "[TERM-ALIGN] "
        return prefix + mapped, time.perf_counter() - started


# ---------------------------------------------------------------------------
# Facade
# ---------------------------------------------------------------------------
class InferenceEngine:
    """Owns the ASR + translator pair and exposes one ``process`` call."""

    def __init__(self, settings: dict, glossary: Glossary, root_dir: str) -> None:
        self.settings = settings or {}
        self.glossary = glossary
        self.root_dir = root_dir
        self.asr = ASREngine(self.settings.get("asr", {}), glossary, root_dir)
        self.llm_cfg = self.settings.get("llm", {})
        self.primary = LlamaTranslator(self.llm_cfg, glossary, root_dir)
        self.fallback = FallbackTranslator(glossary)
        self._translator = self.primary
        self._status = "IDLE"
        self._status_lock = threading.Lock()
        self._preload_thread: Optional[threading.Thread] = None
        self._closed = False

    # -- status --------------------------------------------------------------
    @property
    def status(self) -> str:
        with self._status_lock:
            return self._status

    def _set_status(self, value: str) -> None:
        with self._status_lock:
            self._status = value

    def assets(self) -> dict[str, tuple[bool, str]]:
        vad_path = (
            self.settings.get("audio", {})
            .get("vad", {})
            .get("model_path", "models/silero_vad.onnx")
        )
        if not os.path.isabs(vad_path):
            vad_path = os.path.join(self.root_dir, vad_path)
        return {
            "asr": (self.asr.available(), self.asr._model_dir()),
            "llm": (self.primary.available(), self.primary.model_path()),
            "vad": (os.path.isfile(vad_path), vad_path),
        }

    @property
    def ready(self) -> bool:
        return self.asr._loaded and self._translator is not None

    @property
    def asr_ready(self) -> bool:
        return bool(self.asr._loaded)

    @property
    def translator_engine(self) -> str:
        if self._translator is self.primary and self.primary._loaded:
            return "qwen2.5-1.5b-q4_k_m"
        if isinstance(self._translator, FallbackTranslator):
            return self.fallback.engine_name
        return "unknown"

    # -- lifecycle -----------------------------------------------------------
    def preload(self, on_status: Optional[Callable[[str], None]] = None) -> None:
        def emit(message: str) -> None:
            self._set_status(message)
            if on_status:
                try:
                    on_status(message)
                except Exception:
                    pass

        if self._closed:
            return
        if not self.asr.available():
            emit("ASR MISSING")
            return
        emit("LOADING ASR")
        try:
            self.asr.load()
            emit("ASR READY")
        except Exception as exc:
            emit(f"ASR ERROR: {exc}")
            return

        emit("LOADING LLM")
        if self.primary.available():
            try:
                self.primary.load()
                if bool(self.llm_cfg.get("warmup", True)):
                    self.primary.warmup()
                self._translator = self.primary
                emit("READY")
            except Exception as exc:
                self._translator = self.fallback
                emit(f"LLM ERROR -> TERM-ALIGN: {exc}")
        else:
            self._translator = self.fallback
            emit("LLM MISSING -> TERM-ALIGN")

    def preload_async(self, on_status: Optional[Callable[[str], None]] = None) -> None:
        if self._preload_thread is not None and self._preload_thread.is_alive():
            return
        self._preload_thread = threading.Thread(
            target=self.preload,
            args=(on_status,),
            name="stemtrans-preload",
            daemon=True,
        )
        self._preload_thread.start()

    def wait_ready(self, timeout: Optional[float] = None) -> bool:
        thread = self._preload_thread
        if thread is not None:
            thread.join(timeout=timeout)
        return self.asr._loaded

    def close(self) -> None:
        self._closed = True
        for component in (self.asr, self.primary, self.fallback):
            try:
                component.close()
            except Exception:
                pass
        gc.collect()

    # -- main entry ----------------------------------------------------------
    def process(
        self,
        samples,
        src_lang: str,
        dst_lang: str,
        *,
        forced_asr_language: Optional[str] = None,
        auto_detect: bool = False,
        on_transcript: Optional[Callable[[str, str, str], None]] = None,
    ) -> TranslationResult:
        started = time.perf_counter()
        asr_result = self.asr.transcribe(
            samples,
            language=None if auto_detect else forced_asr_language,
            initial_prompt=None,
        )
        text = asr_result.text
        if len(text.strip()) < 1:
            return TranslationResult("", "", src_lang, dst_lang, "empty")

        if auto_detect:
            src_lang = _detect_language(text, asr_result.language)
            dst_lang = "en" if src_lang == "zh" else "zh"
        if on_transcript:
            on_transcript(text, src_lang, dst_lang)

        translator = self._translator
        try:
            translated, llm_seconds = translator.translate(text, src_lang, dst_lang)
            engine_name = (
                "qwen2.5-1.5b-q4_k_m"
                if translator is self.primary
                else self.fallback.engine_name
            )
        except Exception:
            translated, llm_seconds = self.fallback.translate(text, src_lang, dst_lang)
            engine_name = self.fallback.engine_name

        translated = _clean_translation(translated, dst_lang)
        if not translated or _looks_wrong(translated, dst_lang):
            fallback_text, llm_seconds = self.fallback.translate(
                text, src_lang, dst_lang
            )
            translated = _clean_translation(fallback_text, dst_lang)
            engine_name = self.fallback.engine_name

        terms = self.glossary.match_display_terms(
            translated, "en" if dst_lang == "en" else "zh"
        ) or self.glossary.match_display_terms(text, src_lang)

        return TranslationResult(
            source_text=text,
            translated_text=translated,
            src_lang=src_lang,
            dst_lang=dst_lang,
            engine=engine_name,
            terms=terms,
            asr_seconds=asr_result.infer_seconds,
            llm_seconds=llm_seconds,
            total_seconds=time.perf_counter() - started,
        )


def _detect_language(text: str, whisper_language: str = "") -> str:
    """Script-based language routing for the offline single-mic mode.

    Chinese technical speech is full of Latin acronyms ("我们用 STM32 通过 SPI
    读取 ADC"), so comparing raw letter counts misroutes it.  Counting CJK
    characters against Latin *word tokens* is stable for this use case; the
    Whisper classifier is only a fallback.
    """
    cjk = len(_CJK_RE.findall(text))
    latin_words = len(re.findall(r"[A-Za-z][A-Za-z0-9_+\-.]*", text))
    if cjk >= 2 and cjk >= latin_words * 2:
        return "zh"
    if latin_words > cjk:
        return "en"
    if whisper_language in ("zh", "en"):
        return whisper_language
    return "zh" if cjk else "en"


def _looks_wrong(text: str, dst_lang: str) -> bool:
    """Reject obvious failures (wrong language, prompt echo, huge repetition)."""
    if not text:
        return True
    ratio = _language_ratio(text)
    if dst_lang == "en" and ratio > 0.35:
        return True
    if dst_lang == "zh" and ratio < 0.15 and len(text) > 20:
        return True
    if "TERMINOLOGY" in text.upper() or "TRANSLATE THE USER" in text.upper():
        return True
    words = text.split()
    if len(words) > 8 and len(set(words)) < len(words) * 0.35:
        return True
    return False


if __name__ == "__main__":  # pragma: no cover - quick glossary smoke test
    import argparse

    parser = argparse.ArgumentParser(description="Glossary / prompt inspector")
    parser.add_argument("--glossary", default="config/glossary.json")
    parser.add_argument(
        "--text", default="我们用 STM32 通过 SPI 读取 ADC，PID 控制器超调 12%。"
    )
    args = parser.parse_args()
    glossary = Glossary.load(args.glossary)
    print(f"entries: {len(glossary)}")
    print(f"english terms: {len(glossary.english_terms)}")
    print("\n--- ASR prompt ---")
    print(glossary.asr_initial_prompt())
    print("\n--- system prompt ---")
    print(glossary.system_prompt("zh", "en", args.text))
    print("\n--- matched terms ---")
    print(glossary.match_display_terms(args.text, "zh"))
