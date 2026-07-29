"""Ravaged: bare lines out, length-prefixed frames back. See `parser` and `profiles`."""

from b3.parsers.ravaged.parser import RavParser
from b3.parsers.ravaged.profiles import ALL, RAVAGED

__all__ = ["ALL", "RAVAGED", "RavParser"]
