"""Build a complete Windows distribution without deleting previous builds."""

from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
from importlib.metadata import distributions
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MODELS = (
    "silero_vad.onnx",
    "whisper-base/config.json",
    "whisper-base/model.bin",
    "whisper-base/tokenizer.json",
    "whisper-base/vocabulary.txt",
    "qwen2.5-1.5b-instruct-q4_k_m.gguf",
)


def digest(path):
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def copy_licenses(target):
    shutil.copytree(ROOT / "licenses", target)
    packages = {}
    # Keep installed package notices, including native dependency notices
    # embedded in wheels. The inventory describes the build environment.
    excluded = {"pip", "ruff", "pyflakes", "wheel", "zhconv"}
    for package in distributions():
        name = package.metadata["Name"]
        if name.lower() in excluded:
            continue
        packages[name] = package.version
        for file in package.files or []:
            path = Path(str(file))
            if not any(word in path.name.lower() for word in ("license", "copying", "notice")):
                continue
            source = Path(package.locate_file(file)).resolve()
            if not source.is_file():
                continue
            # Metadata paths sometimes contain ../; never use them as targets.
            destination = target / "python-packages" / name / path.name
            if destination.exists():
                destination = destination.with_name(digest(source)[:10] + "-" + path.name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    python_license = Path(sys.prefix) / "LICENSE_PYTHON.txt"
    if not python_license.is_file():
        python_license = Path(sys.prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise FileNotFoundError("Python runtime license not found")
    shutil.copy2(python_license, target / "Python-LICENSE.txt")
    for metadata in (Path(sys.prefix) / "conda-meta").glob("*.json"):
        info = json.loads(metadata.read_text(encoding="utf-8"))
        source = Path(info.get("link", {}).get("source", "")) / "info" / "licenses"
        if source.is_dir():
            shutil.copytree(source, target / "conda-packages" / info["name"], dirs_exist_ok=True)
    return packages


def audit_native_dependencies(workspace):
    """Fail if an unrelated program supplied any native binary."""
    allowed = (Path(sys.prefix).resolve(), Path(os.environ["SystemRoot"]).resolve())
    entries = ast.literal_eval(next(workspace.rglob("Analysis-00.toc")).read_text(encoding="utf-8"))

    def visit(value):
        if not isinstance(value, (tuple, list)):
            return
        if len(value) == 3 and value[-1] in ("BINARY", "EXTENSION") and isinstance(value[1], str):
            source = Path(value[1]).resolve()
            if not any(source.is_relative_to(parent) for parent in allowed):
                raise RuntimeError(f"Native dependency outside selected Python/Windows: {source}")
        for item in value:
            visit(item)

    visit(entries)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-name", default="StemTrans-Portable")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.output_name):
        parser.error("output-name must be a simple directory name")
    destination = ROOT / "dist" / args.output_name
    if destination.exists():
        parser.error(f"{destination} already exists; use a new --output-name")
    for relative in MODELS:
        if not (ROOT / "models" / relative).is_file():
            raise FileNotFoundError(f"Missing model: {relative}; run download_models.py first")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    workspace = ROOT / "build" / stamp
    workspace.mkdir(parents=True)
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--windowed", "--onedir", "--contents-directory", "_internal",
        "--name", "StemTrans-Portable",
        "--distpath", str(workspace / "output"),
        "--workpath", str(workspace / "work"),
        "--specpath", str(workspace),
        "--exclude-module", "zhconv",
        "--exclude-module", "pytest",
        "--exclude-module", "torch",
    ]
    for package in ("customtkinter", "llama_cpp", "faster_whisper", "ctranslate2", "onnxruntime", "pyaudiowpatch"):
        command.extend(("--collect-all", package))
    for package in ("pyttsx3", "comtypes"):
        command.extend(("--collect-submodules", package))
    command.append(str(ROOT / "main.py"))
    environment = os.environ.copy()
    # Other developer tools can place incompatible SQLite/OpenSSL DLLs on
    # PATH. Resolve native dependencies only from this Python and Windows.
    windows = Path(os.environ["SystemRoot"])
    environment["PATH"] = os.pathsep.join(str(path) for path in (
        Path(sys.prefix), Path(sys.prefix) / "Library/bin",
        Path(sys.prefix) / "DLLs", Path(sys.prefix) / "Scripts",
        windows / "System32", windows,
    ))
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    subprocess.run(command, cwd=ROOT, env=environment, check=True)
    audit_native_dependencies(workspace)
    output = workspace / "output" / "StemTrans-Portable"
    for name in ("README.md", "LICENSE", "QUICKSTART.txt", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(ROOT / name, output / name)
    (output / "config").mkdir()
    for name in ("settings.json", "glossary.json"):
        shutil.copy2(ROOT / "config" / name, output / "config" / name)
    (output / "assets").mkdir()
    for image in (ROOT / "assets").glob("*.png"):
        shutil.copy2(image, output / "assets" / image.name)
    for relative in MODELS:
        target = output / "models" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "models" / relative, target)
    packages = copy_licenses(output / "licenses")
    version = re.search(r'VERSION = "([^"]+)"', (ROOT / "ui/main_window.py").read_text(encoding="utf-8")).group(1)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip())
    manifest = {
        "version": version, "commit": commit, "dirty": dirty,
        "built_at": stamp, "python": sys.version.split()[0], "platform": "windows-x64",
        "build_packages": packages,
        "files": {
            file.relative_to(output).as_posix(): {"bytes": file.stat().st_size, "sha256": digest(file)}
            for file in sorted(output.rglob("*")) if file.is_file()
        },
    }
    (output / "build-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    output.rename(destination)
    print(f"Complete portable distribution: {destination}", flush=True)


if __name__ == "__main__":
    main()
