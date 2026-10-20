"""Collect corresponding source archives and upstream notices for a release.

Downloads data only; never executes downloaded build scripts. FFmpeg's pinned
recipe is parsed as an AST to obtain source URLs and published SHA256 values.
"""

import ast
from concurrent.futures import ThreadPoolExecutor
import hashlib
from importlib.metadata import version
import io
import json
from pathlib import Path
import tarfile
import zipfile

import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "release" / "third-party-sources"
NOTICES = ROOT / "licenses" / "ffmpeg"
RECIPE = "https://raw.githubusercontent.com/PyAV-Org/pyav-ffmpeg/8.1.1-2/scripts/pkg.py"


def get(url):
    response = requests.get(url, timeout=90)
    response.raise_for_status()
    return response.content


def collect(item):
    name, url, expected = item
    target = OUT / name
    payload = target.read_bytes() if target.exists() else get(url)
    digest = hashlib.sha256(payload).hexdigest()
    if expected and digest != expected:
        raise ValueError(f"Source hash mismatch: {name}")
    target.write_bytes(payload)
    # Preserve license texts from all native dependency source archives.
    # No archive paths are ever extracted into the filesystem.
    if tarfile.is_tarfile(target):
        with tarfile.open(target) as archive:
            for member in archive:
                basename = Path(member.name).name
                if (member.isfile() and member.size < 300_000
                        and any(word in basename.lower() for word in ("license", "copying", "notice", "copyright"))
                        and member.name.count("/") <= 3):
                    data = archive.extractfile(member).read()
                    safe = NOTICES / name / (hashlib.sha256(data).hexdigest()[:10] + "-" + basename)
                    safe.parent.mkdir(parents=True, exist_ok=True)
                    safe.write_bytes(data)
    print(f"Source ready: {name} ({len(payload):,} bytes)", flush=True)
    return {"file": name, "url": url, "sha256": digest}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    NOTICES.mkdir(parents=True, exist_ok=True)
    recipe = get(RECIPE)
    (NOTICES / "upstream-pkg.py.txt").write_bytes(recipe)
    items = []
    skip = {"gmp", "unistring", "nettle", "gnutls", "alsa-lib"}
    for node in ast.walk(ast.parse(recipe)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Package"):
            continue
        values = {keyword.arg: ast.literal_eval(keyword.value) for keyword in node.keywords
                  if keyword.arg in ("name", "source_url", "sha256")}
        if values["name"] in skip:
            continue
        url = values["source_url"].replace("http://", "https://")
        suffix = ".tar.bz2" if url.endswith(".bz2") else ".tar.xz" if url.endswith(".xz") else ".tar.gz"
        items.append((values["name"] + suffix, url, values["sha256"]))
    items.append(("pyav-ffmpeg-build-8.1.1-2.tar.gz",
                  "https://api.github.com/repos/PyAV-Org/pyav-ffmpeg/tarball/8.1.1-2", None))
    items.extend((
        ("libiconv-1.19.tar.gz", "https://ftp.gnu.org/pub/gnu/libiconv/libiconv-1.19.tar.gz",
         "88dd96a8c0464eca144fc791ae60cd31cd8ee78321e67397e25fc095c4a19aa6"),
        ("zlib-1.3.2.tar.gz", "https://zlib.net/zlib-1.3.2.tar.gz", None),
    ))
    recipe_ref = "045d46aedb9fdbd93b0900b1e20c1a6c73f23072"
    recipe_files = ("PKGBUILD", "0003-add-cp65001-as-utf8-alias.patch",
                    "0002-fix-cr-for-awk-in-configure.all.patch", "fix-pointer-buf.patch", "iconv.pc")
    recipe_target = OUT / "libiconv-msys2-build.zip"
    with zipfile.ZipFile(recipe_target, "w") as archive:
        for filename in recipe_files:
            url = f"https://raw.githubusercontent.com/msys2/MINGW-packages/{recipe_ref}/mingw-w64-libiconv/{filename}"
            archive.writestr(filename, get(url))
    for package in ("av", "pyttsx3", "certifi", "tqdm"):
        metadata = json.loads(get(f"https://pypi.org/pypi/{package}/{version(package)}/json"))
        source = next(file for file in metadata["urls"] if file["packagetype"] == "sdist")
        items.append((source["filename"], source["url"], source["digests"]["sha256"]))
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(collect, item) for item in items]
        manifest = [future.result() for future in futures]
    manifest.append({
        "file": recipe_target.name,
        "url": f"https://github.com/msys2/MINGW-packages/tree/{recipe_ref}/mingw-w64-libiconv",
        "sha256": hashlib.sha256(recipe_target.read_bytes()).hexdigest(),
    })
    (OUT / "SOURCES.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (NOTICES / "SOURCES.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    # Include the exact upstream recipe/patch set alongside every source.
    recipe_archive = OUT / "pyav-ffmpeg-build-8.1.1-2.tar.gz"
    with tarfile.open(fileobj=io.BytesIO(recipe_archive.read_bytes())) as archive:
        for member in archive:
            if member.isfile() and "/patches/" in member.name:
                target = NOTICES / "patches" / Path(member.name).name
                target.parent.mkdir(exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
    target = ROOT / "release" / "StemTrans-Portable-v1.1.0-third-party-sources.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as archive:
        for path in sorted(OUT.iterdir()):
            archive.write(path, "third-party-sources/" + path.name)
    print(target, flush=True)


if __name__ == "__main__":
    main()
