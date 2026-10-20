#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""One-click model downloader with mainland-China / global dual sources.

Artifacts (all local, all inside ./models):
    1. Silero VAD v5 ONNX  (~2 MB)    speech / silence segmentation
    2. Faster-Whisper base (~145 MB)  or small (~484 MB)   int8 ASR
    3. Qwen2.5-1.5B-Instruct Q4_K_M GGUF (~1.1 GB)         zh<->en translation

Sources
    [1] 中国大陆 : HF-Mirror (https://hf-mirror.com) + ModelScope + jsDelivr
    [2] 海外     : HuggingFace official + GitHub raw

Every transfer is resumable: data is written to ``<file>.part`` and an HTTP
``Range`` request continues from the last byte.  A broken connection therefore
never restarts a 1.1 GB download from zero.

Examples
    python download_models.py                       # interactive
    python download_models.py --source china --all
    python download_models.py --source global --whisper small
    python download_models.py --only llm
    python download_models.py --verify
    python download_models.py --check-urls
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

APP_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(APP_DIR)
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

os.environ.setdefault("HF_HOME", os.path.abspath("./models/hf_cache"))
os.environ.setdefault("PYTHONUTF8", "1")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import requests
except Exception as exc:  # pragma: no cover
    print(f"[fatal] requests is required: {exc}")
    raise SystemExit(1)

HF_MIRROR = "https://hf-mirror.com"
HF_OFFICIAL = "https://huggingface.co"
MODELSCOPE = "https://www.modelscope.cn"
GITHUB_RAW = "https://raw.githubusercontent.com"
JSDELIVR = "https://cdn.jsdelivr.net/gh"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) StemTrans-Portable/1.0"
CHUNK = 1 << 20  # 1 MiB
SETTINGS = os.path.join(APP_DIR, "config", "settings.local.json")


# ---------------------------------------------------------------------------
# Artifact catalogue
# ---------------------------------------------------------------------------
@dataclass
class Artifact:
    key: str
    label: str
    dest: str
    urls: dict[str, list[str]]
    expected_bytes: int = 0
    min_bytes: int = 1
    magic: bytes = b""
    sha256: str = ""
    is_dir: bool = False
    optional: bool = False
    files: list[str] = field(default_factory=list)
    required_files: list[str] = field(default_factory=list)
    note: str = ""


def _whisper_artifact(size: str) -> Artifact:
    repo = f"Systran/faster-whisper-{size}"
    files = ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"]
    expected = {"base": 147_800_000, "small": 486_000_000}.get(size, 0)
    return Artifact(
        key=f"whisper-{size}",
        label=f"Faster-Whisper {size} (int8 ASR)",
        dest=f"models/whisper-{size}",
        is_dir=True,
        files=files,
        required_files=files,
        expected_bytes=expected,
        min_bytes=int(expected * 0.9) if expected else 1,
        optional=(size == "small"),
        urls={
            "china": [f"{HF_MIRROR}/{repo}/resolve/main/{{file}}"],
            "global": [f"{HF_OFFICIAL}/{repo}/resolve/main/{{file}}"],
        },
        note="CTranslate2 int8 weights",
    )


GGUF_FILE = "qwen2.5-1.5b-instruct-q4_k_m.gguf"
VAD_FILE = "src/silero_vad/data/silero_vad.onnx"
VAD_TAG = "v5.1.2"  # the 512-sample/32 ms model; master switched to 256 samples
VAD_SHA256 = "2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f"

ARTIFACTS: dict[str, Artifact] = {
    "vad": Artifact(
        key="vad",
        label="Silero VAD v5.1.2 (ONNX Runtime)",
        dest="models/silero_vad.onnx",
        expected_bytes=2_327_524,
        min_bytes=1_000_000,
        magic=b"\x08",
        sha256=VAD_SHA256,
        urls={
            # Pinned tag matters: the master branch changed the ONNX graph to a
            # 256-sample window, which is incompatible with our 512-sample hop.
            "china": [
                f"{JSDELIVR}/snakers4/silero-vad@{VAD_TAG}/{VAD_FILE}",
                f"{GITHUB_RAW}/snakers4/silero-vad/{VAD_TAG}/{VAD_FILE}",
            ],
            "global": [
                f"{GITHUB_RAW}/snakers4/silero-vad/{VAD_TAG}/{VAD_FILE}",
                f"{JSDELIVR}/snakers4/silero-vad@{VAD_TAG}/{VAD_FILE}",
            ],
        },
        note="no PyTorch needed, executed directly by onnxruntime",
    ),
    "whisper-base": _whisper_artifact("base"),
    "whisper-small": _whisper_artifact("small"),
    "llm": Artifact(
        key="llm",
        label="Qwen2.5-1.5B-Instruct Q4_K_M (translation)",
        dest=f"models/{GGUF_FILE}",
        expected_bytes=1_117_320_736,
        min_bytes=1_000_000_000,
        magic=b"GGUF",
        urls={
            "china": [
                f"{MODELSCOPE}/models/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/master/{GGUF_FILE}",
                f"{HF_MIRROR}/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/{GGUF_FILE}",
            ],
            "global": [
                f"{HF_OFFICIAL}/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/{GGUF_FILE}",
            ],
        },
        note="llama.cpp CPU, mmap, no GPU",
    ),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def human(bytes_: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(bytes_) < 1024.0:
            return f"{bytes_:.1f} {unit}"
        bytes_ /= 1024.0
    return f"{bytes_:.1f} TB"


class Progress:
    """tqdm if available, otherwise a compact percentage line."""

    def __init__(self, total: int, label: str) -> None:
        self.total = total
        self.label = label
        self.start = time.time()
        self._last = 0.0
        self._bar = None
        if total > 0:
            try:
                from tqdm import tqdm

                self._bar = tqdm(
                    total=total,
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                    desc=label[:28],
                    ascii=True,
                    leave=True,
                )
            except Exception:
                self._bar = None

    def update(self, n: int) -> None:
        if self._bar is not None:
            self._bar.update(n)
            return
        now = time.time()
        if now - self._last < 0.5:
            return
        self._last = now
        if self.total <= 0:
            print(f"  {self.label}: {human(n)}", end="\r")
            return
        pct = min(100.0, n * 100.0 / self.total)
        speed = n / max(0.001, now - self.start)
        print(
            f"  {self.label}: {pct:5.1f}%  {human(n)} / {human(self.total)}  "
            f"({human(speed)}/s)",
            end="\r",
        )

    def close(self) -> None:
        if self._bar is not None:
            self._bar.close()
        else:
            print()


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _finalize(
    part: str,
    dest: str,
    expected_bytes: int = 0,
    min_bytes: int = 1,
    magic: bytes = b"",
    sha256: str = "",
) -> None:
    """Validate a completed ``.part`` then atomically promote it."""
    size = os.path.getsize(part)
    if expected_bytes and size < int(expected_bytes * 0.995):
        raise RuntimeError(f"incomplete file: {size} of {expected_bytes} bytes")
    if size < min_bytes:
        raise RuntimeError(f"file too small: {size} < {min_bytes}")
    if magic:
        with open(part, "rb") as handle:
            head = handle.read(len(magic))
        if head != magic:
            raise RuntimeError(f"bad file signature {head!r} (expected {magic!r})")
    if sha256:
        digest = _sha256(part)
        if digest.lower() != sha256.lower():
            raise RuntimeError(f"sha256 mismatch: {digest[:16]}... != {sha256[:16]}...")
    os.replace(part, dest)


def http_download(
    url: str,
    dest: str,
    expected_bytes: int = 0,
    min_bytes: int = 1,
    magic: bytes = b"",
    sha256: str = "",
    retries: int = 3,
) -> None:
    """Resumable download with Range requests and a ``.part`` file."""
    part = dest + ".part"
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}

    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        existing = os.path.getsize(part) if os.path.isfile(part) else 0
        if expected_bytes and existing > expected_bytes:
            os.remove(part)
            existing = 0
        if expected_bytes and existing == expected_bytes and existing > 0:
            try:
                _finalize(part, dest, expected_bytes, min_bytes, magic, sha256)
                return
            except Exception as exc:
                print(f"  partial file rejected ({exc}); restarting")
                try:
                    os.remove(part)
                except OSError:
                    pass
                existing = 0

        request_headers = dict(headers)
        if existing:
            request_headers["Range"] = f"bytes={existing}-"
        try:
            with requests.get(
                url,
                headers=request_headers,
                stream=True,
                timeout=(20, 90),
                allow_redirects=True,
            ) as response:
                if response.status_code == 416 and existing:
                    # Already complete (server disagrees on the exact size).
                    _finalize(part, dest, expected_bytes, min_bytes, magic, sha256)
                    return
                response.raise_for_status()
                content_type = (response.headers.get("Content-Type") or "").lower()
                if content_type.startswith("text/html"):
                    raise RuntimeError(
                        "server returned an HTML page (mirror error / login wall)"
                    )

                append = bool(existing and response.status_code == 206)
                if existing and not append:
                    existing = 0
                    print("  mirror ignored Range; restarting this file")
                total = 0
                if response.status_code == 206:
                    content_range = response.headers.get("Content-Range", "")
                    if "/" in content_range:
                        try:
                            total = int(content_range.rsplit("/", 1)[1])
                        except ValueError:
                            total = 0
                if total <= 0:
                    try:
                        total = existing + int(
                            response.headers.get("Content-Length", 0)
                        )
                    except (TypeError, ValueError):
                        total = 0
                if expected_bytes:
                    total = expected_bytes

                progress = Progress(total, os.path.basename(dest))
                written = existing
                mode = "ab" if append else "wb"
                with open(part, mode) as handle:
                    for chunk in response.iter_content(chunk_size=CHUNK):
                        if not chunk:
                            continue
                        handle.write(chunk)
                        written += len(chunk)
                        progress.update(len(chunk))
                progress.close()

                try:
                    _finalize(part, dest, expected_bytes, min_bytes, magic, sha256)
                except Exception:
                    # A corrupted / wrong-version partial must not be resumed
                    # from on the next source.
                    try:
                        os.remove(part)
                    except OSError:
                        pass
                    raise
                return
        except Exception as exc:
            last_error = exc
            print(f"  attempt {attempt}/{retries} failed: {exc}")
            if attempt < retries:
                time.sleep(2.0 * attempt)
    raise RuntimeError(f"download failed after {retries} attempts: {last_error}")


def download_artifact(artifact: Artifact, source: str, whisper_size: str = "") -> bool:
    base_urls = artifact.urls["china" if source == "china" else "global"]
    if artifact.is_dir:
        os.makedirs(os.path.join(APP_DIR, artifact.dest), exist_ok=True)
        ok = True
        for filename in artifact.files:
            dest = os.path.join(APP_DIR, artifact.dest, filename)
            if os.path.isfile(dest) and os.path.getsize(dest) > 0:
                print(f"  [skip] {artifact.dest}/{filename}")
                continue
            success = False
            for template in base_urls:
                url = template.format(file=filename)
                print(f"\n[{artifact.key}] {filename}\n  <- {url}")
                try:
                    http_download(
                        url,
                        dest,
                        expected_bytes=0,
                        min_bytes=(50_000_000 if filename == "model.bin" else 1_000),
                    )
                    success = True
                    break
                except Exception as exc:
                    print(f"  source failed: {exc}")
            if not success:
                ok = False
        # Post-check the directory.
        missing = [
            name
            for name in artifact.required_files
            if not os.path.isfile(os.path.join(APP_DIR, artifact.dest, name))
        ]
        if missing:
            print(f"[error] {artifact.key}: missing {missing}")
            return False
        total = sum(
            os.path.getsize(os.path.join(APP_DIR, artifact.dest, name))
            for name in artifact.files
            if os.path.isfile(os.path.join(APP_DIR, artifact.dest, name))
        )
        if total < artifact.min_bytes:
            print(f"[error] {artifact.key}: total size {human(total)} is too small")
            return False
        print(f"[ok] {artifact.label}: {total / 1e6:.1f} MB")
        return ok

    dest = os.path.join(APP_DIR, artifact.dest)
    if os.path.isfile(dest) and os.path.getsize(dest) >= artifact.min_bytes:
        if artifact.sha256 and _sha256(dest).lower() != artifact.sha256.lower():
            print(
                f"[warn] {artifact.dest} exists but its SHA-256 does not match "
                "the pinned build; re-downloading"
            )
        else:
            print(
                f"[skip] {artifact.dest} already present "
                f"({human(os.path.getsize(dest))})"
            )
            return True

    for url in base_urls:
        print(f"\n[{artifact.key}] {artifact.label}\n  <- {url}")
        try:
            http_download(
                url,
                dest,
                expected_bytes=artifact.expected_bytes,
                min_bytes=artifact.min_bytes,
                magic=artifact.magic,
                sha256=artifact.sha256,
            )
            print(f"[ok] {artifact.dest} ({human(os.path.getsize(dest))})")
            return True
        except Exception as exc:
            print(f"  source failed: {exc}")
    print(f"[error] could not download {artifact.key} from any source")
    return False


# ---------------------------------------------------------------------------
# Verification / source inspection
# ---------------------------------------------------------------------------
def verify() -> int:
    print("Artifact status")
    print("-" * 78)
    failures = 0
    for artifact in ARTIFACTS.values():
        if artifact.is_dir:
            path = os.path.join(APP_DIR, artifact.dest)
            present = all(
                os.path.isfile(os.path.join(path, name))
                for name in artifact.required_files
            )
            if present:
                total = sum(
                    os.path.getsize(os.path.join(path, name))
                    for name in artifact.files
                    if os.path.isfile(os.path.join(path, name))
                )
                print(f"[ ok ] {artifact.label:<44} {human(total)}")
            else:
                if artifact.optional:
                    print(f"[opt ] {artifact.label:<44} not installed")
                else:
                    failures += 1
                    print(f"[MISS] {artifact.label:<44} {path}")
            continue
        path = os.path.join(APP_DIR, artifact.dest)
        if not os.path.isfile(path):
            failures += 1
            print(f"[MISS] {artifact.label:<44} {path}")
            continue
        size = os.path.getsize(path)
        signature_ok = True
        if artifact.magic:
            with open(path, "rb") as handle:
                signature_ok = handle.read(len(artifact.magic)) == artifact.magic
        digest_ok = True
        if artifact.sha256:
            digest_ok = _sha256(path).lower() == artifact.sha256.lower()
        if size >= artifact.min_bytes and signature_ok and digest_ok:
            print(f"[ ok ] {artifact.label:<44} {human(size)}")
        else:
            failures += 1
            print(
                f"[BAD ] {artifact.label:<44} {human(size)} "
                f"signature={'ok' if signature_ok else 'bad'} "
                f"sha256={'ok' if digest_ok else 'bad'}"
            )
    print("-" * 78)
    print(
        "All artifacts ready." if failures == 0 else f"{failures} artifact(s) missing."
    )
    return 0 if failures == 0 else 1


def check_urls(source: str) -> int:
    print(f"HEAD check for source '{source}'")
    headers = {"User-Agent": USER_AGENT}
    failures = 0
    for artifact in ARTIFACTS.values():
        print(f"\n{artifact.key}:")
        for url in artifact.urls[source]:
            try:
                resolved = url.format(
                    file=(artifact.files[0] if artifact.files else "")
                )
                response = requests.head(
                    resolved, allow_redirects=True, timeout=(10, 20), headers=headers
                )
                size = response.headers.get("Content-Length", "?")
                print(
                    f"  {response.status_code}  "
                    f"{human(int(size)) if size.isdigit() else size:<10}  {resolved}"
                )
                if response.status_code >= 400:
                    failures += 1
            except Exception as exc:
                failures += 1
                print(f"  ERR  {type(exc).__name__}: {url}")
    return 0 if failures == 0 else 1


def clean_partials() -> int:
    removed = 0
    for root, _dirs, files in os.walk(os.path.join(APP_DIR, "models")):
        for name in files:
            if name.endswith(".part"):
                path = os.path.join(root, name)
                try:
                    os.remove(path)
                    removed += 1
                    print(f"removed {path}")
                except Exception as exc:
                    print(f"cannot remove {path}: {exc}")
    print(f"{removed} partial file(s) removed.")
    return 0


# ---------------------------------------------------------------------------
# Settings persistence
# ---------------------------------------------------------------------------
def save_source(source: str) -> None:
    payload = {}
    if os.path.isfile(SETTINGS):
        try:
            with open(SETTINGS, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            payload = {}
    payload.setdefault("network", {})["source"] = source
    try:
        with open(SETTINGS + ".tmp", "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(SETTINGS + ".tmp", SETTINGS)
        print(f"[ok] saved network.source = {source} in config/settings.local.json")
    except Exception as exc:
        print(f"[warn] could not update settings.local.json: {exc}")


# ---------------------------------------------------------------------------
# Interactive flow
# ---------------------------------------------------------------------------
def ask(prompt: str, default: str) -> str:
    try:
        value = input(prompt).strip()
    except EOFError:
        return default
    return value or default


def interactive_source() -> str:
    print(
        "\n选择模型下载源 / Choose a download source\n"
        "  [1] 中国大陆极速镜像  (HF-Mirror + ModelScope + jsDelivr)\n"
        "  [2] 海外官方直连      (HuggingFace + GitHub raw)\n"
    )
    return "global" if ask("  请输入 1 或 2 [默认 1]: ", "1") == "2" else "china"


def interactive_plan(source: str) -> tuple[str, bool, bool, bool]:
    size = ask("\nWhisper 模型大小 base / small [默认 base]: ", "base").lower()
    if size not in ("base", "small"):
        size = "base"
    want_llm = ask(
        "\n下载 Qwen2.5-1.5B-Instruct Q4_K_M 翻译模型 (~1.1 GB)? [Y/n]: ", "y"
    ).lower() not in ("n", "no")
    want_vad = ask("下载 Silero VAD (~2 MB)? [Y/n]: ", "y").lower() not in ("n", "no")
    return source, size == "small", want_llm, want_vad


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="StemTrans model downloader (China mirror / global official)"
    )
    parser.add_argument("--source", choices=["china", "global"])
    parser.add_argument("--whisper", choices=["base", "small"], default="base")
    parser.add_argument(
        "--all", action="store_true", help="no prompts, download everything"
    )
    parser.add_argument("--yes", action="store_true", help="assume yes for prompts")
    parser.add_argument(
        "--only", choices=["vad", "whisper", "llm"], help="single artifact"
    )
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--no-vad", action="store_true")
    parser.add_argument("--no-whisper", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--check-urls", action="store_true")
    parser.add_argument("--clean-partials", action="store_true")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)

    if args.verify:
        return verify()
    if args.clean_partials:
        return clean_partials()
    if args.list:
        print("Artifacts")
        for artifact in ARTIFACTS.values():
            print(
                f"  {artifact.key:<14} {artifact.label:<46} "
                f"~{human(artifact.expected_bytes) if artifact.expected_bytes else 'dir'}"
            )
        print(
            "\nSources: china = HF-Mirror/ModelScope/jsDelivr, global = HuggingFace/GitHub"
        )
        return 0

    # Choose source before anything network-related.
    if args.source:
        source = args.source
    elif args.all or args.yes or args.only:
        source = "china"
    else:
        source = interactive_source()
    os.environ["HF_ENDPOINT"] = HF_MIRROR if source == "china" else HF_OFFICIAL
    print(f"\nHF_ENDPOINT = {os.environ['HF_ENDPOINT']}")
    print(f"cache       = {os.path.abspath('./models/hf_cache')}")
    if args.check_urls:
        return check_urls(source)

    whisper_size = args.whisper
    want_llm = not args.no_llm
    want_vad = not args.no_vad
    want_whisper = not args.no_whisper
    if not (args.all or args.yes or args.only):
        source, small, want_llm, want_vad = interactive_plan(source)
        whisper_size = "small" if small else "base"
        os.environ["HF_ENDPOINT"] = HF_MIRROR if source == "china" else HF_OFFICIAL

    selected: list[Artifact] = []
    if args.only == "vad":
        selected = [ARTIFACTS["vad"]]
    elif args.only == "llm":
        selected = [ARTIFACTS["llm"]]
    elif args.only == "whisper":
        selected = [ARTIFACTS[f"whisper-{whisper_size}"]]
    else:
        if want_vad:
            selected.append(ARTIFACTS["vad"])
        if want_whisper:
            selected.append(ARTIFACTS[f"whisper-{whisper_size}"])
        if want_llm:
            selected.append(ARTIFACTS["llm"])

    print("\nPlan:")
    for artifact in selected:
        print(f"  - {artifact.label}")
    if not (args.all or args.yes or args.only):
        if ask("\n开始下载? [Y/n]: ", "y").lower() in ("n", "no"):
            print("Cancelled.")
            return 0

    started = time.time()
    results: list[tuple[str, bool]] = []
    for artifact in selected:
        print("\n" + "=" * 78)
        try:
            ok = download_artifact(artifact, source, whisper_size)
        except KeyboardInterrupt:
            print("\nInterrupted. Re-run the same command to resume from .part files.")
            return 130
        except Exception as exc:
            print(f"[error] {artifact.key}: {exc}")
            ok = False
        results.append((artifact.key, ok))

    save_source(source)
    print("\n" + "=" * 78)
    for key, ok in results:
        print(f"  [{'ok' if ok else 'FAIL'}] {key}")
    print(f"elapsed: {time.time() - started:.1f}s")
    if all(ok for _key, ok in results):
        print("\nAll selected models are ready. Start the app with run_app.bat")
        return 0
    print(
        "\nSome downloads failed. Tips:\n"
        "  - China: python download_models.py --source china --all\n"
        "  - Global: python download_models.py --source global --all\n"
        "  - Resume always works: just run the same command again.\n"
        "  - Check a mirror: python download_models.py --check-urls --source china"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
