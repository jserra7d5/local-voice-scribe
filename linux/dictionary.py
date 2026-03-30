"""Dictionary loading and replacement rules."""

import re

from . import config as cfg


def load_dictionary() -> str | None:
    """Load dictionary words (non-replacement lines) for whisper initial_prompt."""
    if not cfg.DICTIONARY_FILE.exists():
        return None
    words = []
    for line in cfg.DICTIONARY_FILE.read_text().splitlines():
        word = line.strip()
        if word and "->" not in word:
            words.append(word)
    if not words:
        return None
    return ", ".join(words)


def load_replacements() -> list[tuple[str, str]]:
    """Load replacement rules (wrong -> right) from dictionary file."""
    if not cfg.DICTIONARY_FILE.exists():
        return []
    replacements = []
    for line in cfg.DICTIONARY_FILE.read_text().splitlines():
        if "->" in line:
            parts = line.split("->", 1)
            wrong = parts[0].strip()
            right = parts[1].strip()
            if wrong and right:
                replacements.append((wrong, right))
    return replacements


def apply_replacements(text: str) -> str:
    """Apply all replacement rules to text (case-insensitive)."""
    for wrong, right in load_replacements():
        try:
            pattern = re.compile(re.escape(wrong), re.IGNORECASE)
            text = pattern.sub(right, text)
        except re.error:
            pass
    return text
