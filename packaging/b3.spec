# PyInstaller spec for the standalone builds: `b3` (`b3.exe` on Windows), with its own Python inside.
#
#   pyinstaller packaging/b3.spec              -> dist/b3/b3 and its _internal folder
#   pyinstaller packaging/b3.spec --distpath onefile -- --onefile -> onefile/b3, one file
#
# The same spec on Windows, Linux and macOS. PyInstaller does not cross-compile, so the release
# workflow runs it once on each.
#
# The folder build is what the release zips. The one-file build is the same program unpacked into a
# temporary directory at every start, which is slower to start and what antivirus software is most
# suspicious of. It is offered because one file is what people
# expect a download to be.
#
# Three things PyInstaller cannot work out on its own, and every one of them fails only at run time:
#
# * **The bundled plugins are imported by name** (`importlib.import_module` in `b3.core.pluginmgr`),
#   so the import graph never reaches them. Every module under `b3` is collected.
# * **Alembic reads migration scripts as files**, from `b3/storage/migrations`. Compiled into the
#   archive, they would not be there to read, and `b3 db upgrade` would find no revisions at all.
# * **SQLAlchemy imports a database driver from the URL**, at connect time. The three the image and
#   the extras carry are named here, or a MySQL URL would fail with "No module named pymysql".

import argparse
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

options = argparse.ArgumentParser()
options.add_argument("--onefile", action="store_true")
onefile = options.parse_args().onefile

ROOT = Path(SPECPATH).resolve().parent.parent  # noqa: F821 - SPECPATH is defined by PyInstaller
SRC = ROOT / "src"

hiddenimports = [
    *collect_submodules("b3"),
    # Drivers SQLAlchemy loads from a URL, and the sftp:// log source's transport.
    "pymysql",
    *collect_submodules("psycopg"),
    "paramiko",
    # Alembic's own modules, which `env.py` reaches only once it is read from disk.
    *collect_submodules("alembic"),
    *collect_submodules("sqlalchemy.dialects"),
]

datas = [
    # `include_py_files`, because these are the migration scripts Alembic reads as files.
    *collect_data_files("b3", include_py_files=True, includes=["storage/migrations/**/*"]),
    *collect_data_files("b3", includes=["data/*"]),
    # The plugin configs `b3 init` copies into a new server's folder for the operator to edit.
    *collect_data_files("b3", includes=["examples/*.yaml"]),
    # Windows (and many slim Linux installs) ship no time zone database; `zoneinfo` reads this package's files instead.
    *collect_data_files("tzdata"),
]

analysis = Analysis(  # noqa: F821
    [str(Path(SPECPATH) / "entry.py")],  # noqa: F821
    pathex=[str(SRC)],
    hiddenimports=hiddenimports,
    datas=datas,
    # Development tools that a dependency mentions and the bot never runs.
    excludes=["tkinter", "pytest", "mypy", "ruff"],
)
archive = PYZ(analysis.pure)  # noqa: F821

common = {
    "name": "b3",
    "console": True,  # a console program: it logs to the terminal and reads `b3 init`'s prompts
    "upx": False,  # UPX-packed executables are flagged by antivirus far more often, for no gain
}

if onefile:
    exe = EXE(archive, analysis.scripts, analysis.binaries, analysis.datas, [], **common)  # noqa: F821
else:
    exe = EXE(archive, analysis.scripts, [], exclude_binaries=True, **common)  # noqa: F821
    COLLECT(exe, analysis.binaries, analysis.datas, name="b3", upx=False)  # noqa: F821
