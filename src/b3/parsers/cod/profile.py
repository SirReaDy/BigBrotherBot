"""Backwards-compatible re-export: the profile model moved up a level when the Quake3
family began sharing it. Import from :mod:`b3.parsers.profile` in new code."""

from b3.parsers.profile import GameProfile

__all__ = ["GameProfile"]
