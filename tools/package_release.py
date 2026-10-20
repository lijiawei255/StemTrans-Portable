"""Archive a pristine build, verifying the file manifest before packaging."""

import argparse
import json
from pathlib import Path
import zipfile

from build_portable import ROOT, digest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    folder = args.folder.resolve()
    manifest = json.loads((folder / "build-manifest.json").read_text(encoding="utf-8"))
    if manifest["dirty"]:
        raise RuntimeError("Release must be built from a committed, clean checkout")
    files = {path.relative_to(folder).as_posix(): path for path in folder.rglob("*") if path.is_file()}
    if set(files) != set(manifest["files"]) | {"build-manifest.json"}:
        raise RuntimeError("Build contains unexpected or missing files (possibly local config/logs)")
    for name, expected in manifest["files"].items():
        if files[name].stat().st_size != expected["bytes"] or digest(files[name]) != expected["sha256"]:
            raise RuntimeError(f"Build file changed: {name}")
    target = ROOT / "release" / f"StemTrans-Portable-v{manifest['version']}-windows-x64.zip"
    target.parent.mkdir(exist_ok=True)
    if target.exists():
        raise FileExistsError(target)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for name, path in sorted(files.items()):
            archive.write(path, "StemTrans-Portable/" + name)
    with zipfile.ZipFile(target) as archive:
        damaged = archive.testzip()
        if damaged:
            raise RuntimeError(f"ZIP CRC failure: {damaged}")
    if target.stat().st_size >= 2_000_000_000:
        raise RuntimeError("ZIP exceeds the release asset size budget")
    checksums = [f"{digest(path)}  {path.name}" for path in sorted(target.parent.glob("*.zip"))]
    (target.parent / "SHA256SUMS.txt").write_text("\n".join(checksums) + "\n", encoding="ascii")
    print(f"{target} ({target.stat().st_size:,} bytes)", flush=True)


if __name__ == "__main__":
    main()
