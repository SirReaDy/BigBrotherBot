"""What `b3.exe` runs: the same `main` the `b3` console script does."""

from b3.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
