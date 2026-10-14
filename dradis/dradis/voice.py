"""
voice.py
────────
Text for a speaker: everything DRADIS says through an HA voice assistant.

Car Mode's `to_spoken` is the base — icons, markup, links, units, compass points
and dates are already handled there — but it was written for CarPlay reading a
Telegram notification, and it leaves in what a TTS engine spells out: the
underscore in "Temporali_2" came out as "trattino basso", the hyphen in
"15:55 - pioggia" and "nord-ovest" as "trattino". This module finishes the job
for speech only; Car Mode's Telegram output is not touched.

Pure, idempotent, no I/O.
"""

import re

from car_mode import to_spoken

_RANGE_WORD = {"it": "a", "en": "to"}

_UNDERSCORE   = re.compile(r"_+")
_BULLET       = re.compile(r"(?m)^\s*[-*+]\s+")
_SPACED_DASH  = re.compile(r"\s+[-‐‑‒]+\s+")
_NUM_RANGE    = re.compile(r"(?<=\d)\s*-\s*(?=\d)")
_WORD_HYPHEN  = re.compile(r"(?<=\w)-(?=\w)")
_SLASH_WORDS  = re.compile(r"(?<=\w)/(?=\w)|(?<![\w])/(?=\w)")
_LEFTOVER     = re.compile(r"[#*~^|<>\[\]{}`]")
_MULTI_COMMA  = re.compile(r"\s*,\s*(,\s*)+")
_SPACES       = re.compile(r"[ \t]+")
_DECIMAL_POINT = re.compile(r"(?<=\d)\.(?=\d)")


def for_voice(text: str, lang: str = "it") -> str:
    """`text` (Telegram HTML, Markdown or plain) as words a TTS reads well."""
    lang = lang if lang in _RANGE_WORD else "en"
    # Before to_spoken: list markers are only recognisable at line starts,
    # and to_spoken joins the lines.
    text = _BULLET.sub("", text or "")
    text = _UNDERSCORE.sub(" ", text)
    text = to_spoken(text, lang)
    if lang == "it":
        text = _DECIMAL_POINT.sub(",", text)                     # 27.5 → 27,5 ("virgola")
    text = _NUM_RANGE.sub(f" {_RANGE_WORD[lang]} ", text)      # 10-15 km → 10 a 15 km
    text = _SPACED_DASH.sub(", ", text)                          # 15:55 - pioggia
    text = _WORD_HYPHEN.sub(" ", text)                           # nord-ovest
    text = _SLASH_WORDS.sub(" ", text)                           # /rain, e/o
    text = _LEFTOVER.sub(" ", text)
    text = _MULTI_COMMA.sub(", ", text)
    text = _SPACES.sub(" ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return text.strip(" ,")
