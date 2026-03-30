"""Standalone dictionary editor (re-exported from overlay for convenience)."""

# The DictionaryEditor class lives in overlay.py alongside the Qt app.
# This module exists so it can be imported independently if needed.

from .overlay import DictionaryEditor

__all__ = ["DictionaryEditor"]
