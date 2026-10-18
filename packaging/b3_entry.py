"""The script PyInstaller freezes into the standalone `b3` executable.

The console script pip generates does exactly this; a frozen build has no pip, so it needs it spelled out.
"""

import sys

from b3.cli import main

if __name__ == "__main__":
    sys.exit(main())
