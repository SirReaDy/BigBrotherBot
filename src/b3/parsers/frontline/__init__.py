"""Frontlines: Fuel of War: an MD5 challenge login, and a roster reply that is the event stream.

See `parser` and `profiles`.
"""

from b3.parsers.frontline.parser import FlParser
from b3.parsers.frontline.profiles import ALL, FRONTLINE

__all__ = ["ALL", "FRONTLINE", "FlParser"]
