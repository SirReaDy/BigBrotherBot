"""Build the standalone `b3` executables a release attaches, for the platform this runs on.

    python tools/build_binary.py            # both layouts, into release/
    python tools/build_binary.py --onefile  # just the single file

Two layouts, because they suit different people. The **single file** is one download that runs: it
unpacks itself to a temporary directory on every start, which costs a second or two and is what some
antivirus products dislike. The **bundle** is a directory with `b3` (or `b3.exe`) inside, zipped: it
starts at once and is the one to run as a service.

Needs PyInstaller and the package installed with every extra — a standalone build has no pip to add
one later, so `api`, `mysql`, `postgres`, `sftp` and `completion` all go in:

    pip install ".[api,mysql,postgres,sftp,completion]" "pyinstaller>=6.10,<7"

PyInstaller does not cross-compile: a Windows build comes from Windows, and so on. The release
workflow runs this once per platform.
"""

from __future__ import annotations

import argparse
import hashlib
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "packaging" / "b3_entry.py"
SEP = ";" if sys.platform == "win32" else ":"


def target() -> str:
    """`windows-x64`, `linux-arm64`, `macos-arm64`: the suffix every asset name carries."""
    system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, sys.platform)
    machine = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)
    return f"{system}-{arch}"


def version() -> str:
    sys.path.insert(0, str(ROOT / "src"))
    import b3

    return b3.__version__


def pyinstaller(mode: str, work: Path) -> Path:
    """Run PyInstaller once; return what it produced (the file, or the bundle directory)."""
    command = [
        sys.executable, "-m", "PyInstaller", str(ENTRY),
        f"--{mode}",
        "--name", "b3",
        "--console",
        "--noconfirm",
        "--clean",
        "--distpath", str(work / "dist"),
        "--workpath", str(work / "build"),
        "--specpath", str(work),
        # Plugins and parsers are found by name at run time, so nothing imports them for the
        # analysis to follow; every submodule goes in.
        "--collect-submodules", "b3",
        # The bundled GeoIP database and its licence.
        "--collect-data", "b3",
        # Alembic reads migrations as source files from a directory, not as imported modules.
        "--add-data", f"{ROOT / 'src' / 'b3' / 'storage' / 'migrations'}{SEP}b3/storage/migrations",
        # Same reason as `b3`: optional backends are imported by URL scheme, not by name in code.
        "--collect-submodules", "sqlalchemy.dialects",
        "--collect-submodules", "uvicorn",
        "--hidden-import", "pymysql",
        "--hidden-import", "psycopg",
        "--hidden-import", "paramiko",
        "--hidden-import", "argcomplete",
        "--copy-metadata", "b3ng",
    ]
    subprocess.run(command, check=True, cwd=ROOT)
    name = "b3.exe" if sys.platform == "win32" and mode == "onefile" else "b3"
    return work / "dist" / name


def smoke(executable: Path) -> None:
    """Run it: a build that cannot start is the failure worth catching before it is uploaded."""
    for args in (["--version"], ["games"]):
        result = subprocess.run([str(executable), *args], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise SystemExit(f"{executable} {' '.join(args)} failed:\n{result.stdout}{result.stderr}")
        print(f"{executable.name} {' '.join(args)}: {result.stdout.splitlines()[0]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--onefile", action="store_true", help="only the single-file executable")
    parser.add_argument("--onedir", action="store_true", help="only the bundle")
    parser.add_argument("--out", type=Path, default=ROOT / "release", help="where the assets go")
    args = parser.parse_args()
    modes = [m for m, on in (("onefile", args.onefile), ("onedir", args.onedir)) if on]
    modes = modes or ["onefile", "onedir"]

    stem = f"b3-{version()}-{target()}"
    work = ROOT / "build" / "pyinstaller"
    args.out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for mode in modes:
        shutil.rmtree(work, ignore_errors=True)
        built = pyinstaller(mode, work)
        if mode == "onefile":
            smoke(built)
            asset = args.out / (stem + (".exe" if sys.platform == "win32" else ""))
            shutil.copy2(built, asset)
        else:
            smoke(built / ("b3.exe" if sys.platform == "win32" else "b3"))
            # The directory inside the archive carries the version, so unpacking two releases side
            # by side does not merge them.
            if sys.platform == "win32":
                asset = args.out / f"{stem}-bundle.zip"
                with zipfile.ZipFile(asset, "w", zipfile.ZIP_DEFLATED) as archive:
                    for path in sorted(built.rglob("*")):
                        archive.write(path, Path(stem) / path.relative_to(built))
            else:
                # tar, not zip, outside Windows: it keeps the executable bit.
                asset = args.out / f"{stem}-bundle.tar.gz"
                with tarfile.open(asset, "w:gz") as archive:
                    archive.add(built, arcname=stem)
        written.append(asset)

    for asset in written:
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        print(f"{digest}  {asset.name}  ({asset.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
