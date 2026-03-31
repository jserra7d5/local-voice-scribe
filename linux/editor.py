"""Standalone dictionary editor (re-exported from settings for convenience)."""

# The DictionaryEditor class lives in settings.py alongside the Qt settings UI.
# This module exists so it can be imported independently if needed.

from .settings import DictionaryEditor

__all__ = ["DictionaryEditor"]
