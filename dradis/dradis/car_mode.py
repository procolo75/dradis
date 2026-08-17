"""
car_mode.py
───────────
Turns a DRADIS Telegram message into something a car can read out loud.

Every alert DRADIS sends is built to be LOOKED AT: an emoji heading each line,
<b> tags, abbreviated units, separators like `·` and `—`, and for weather a radar
chart riding along as a photo. Behind the wheel that format collapses. CarPlay
hands the notification to the speech engine, which announces emoji by name
("cloud with lightning and rain"), spells `45°` and `2/4` as symbols, reads a URL
character by character, and — when a photo is attached — often says nothing at
all beyond "Image".

This module is a deterministic sanitiser, not a rewriter. It costs no tokens, it
adds no latency to an urgent message, and it applies to every message DRADIS
sends today or adds later, because it works on the finished string rather than on
each monitor's formatting code.

It imports nothing that opens a socket or needs `/data/options.json`, following
the rule `live_monitors/snapshot.py` states for its own formatter: the wording
must be unit-testable without conjuring python-telegram-bot and the LLM SDKs into
existence. The Car Mode ON/OFF gate therefore lives in `bot/state.for_car`, not
here — this module only knows how to speak, never whether it should.

`to_spoken` is IDEMPOTENT. `send_telegram` applies it on the way out, so a caller
that already sanitised its own text (the live-monitor photo branch, for one) must
not have its message mangled a second time.
"""

import html
import re

# ── Step 1-4: markup and links ────────────────────────────────────────────────
# The label of a link carries the meaning; the href is the part that gets spelled
# out one character at a time. Only two sites in DRADIS emit an anchor —
# `live_monitors/snapshot.py` (the map link) and `live_monitors/seismic.py` (INGV
# plus Google Maps) — and both write the destination into the label already.
_ANCHOR_RE = re.compile(r"<a\s[^>]*>(.*?)</a>", re.DOTALL | re.IGNORECASE)
_TAG_RE    = re.compile(r"</?[a-zA-Z][^>]*>")
_URL_RE    = re.compile(r"https?://\S+")

# ── Step 5: emoji ─────────────────────────────────────────────────────────────
# Deliberately range-based rather than a list: DRADIS gains monitors, and every
# new one brings its own icons. The ranges cover the pictographic planes plus the
# older symbol blocks the alerts actually reach into — ⏱ and ⏸ live at U+23xx, ⚡
# and 🧭's neighbours at U+26xx-27xx, ⬅ at U+2Bxx — and strip the variation
# selectors and ZWJ that hold composed emoji together.
#
# `°` (U+00B0) and `·` (U+00B7) are NOT in any of these ranges, on purpose: they
# are meaningful text and step 6 turns them into words.
# Written as codepoints rather than literals on purpose: half of what has to be
# matched here is invisible in an editor (the variation selectors and the ZWJ),
# and a range typed as characters is a range nobody can review.
_EMOJI_RANGES = [
    (0x1F000, 0x1FAFF),  # pictographs, transport, geometric, extended-A
    (0x2190, 0x21FF),    # arrows
    (0x2300, 0x23FF),    # clock and media symbols
    (0x2600, 0x27BF),    # misc symbols and dingbats
    (0x2B00, 0x2BFF),    # arrows and stars
    (0xFE00, 0xFE0F),    # variation selectors
    (0x200D, 0x200D),    # zero-width joiner
    (0x20E3, 0x20E3),    # combining enclosing keycap
    (0x2139, 0x2139),    # information source
    (0x3030, 0x3030),    # wavy dash
    (0x303D, 0x303D),    # part alternation mark
]

_EMOJI_RE = re.compile(
    "[" + "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in _EMOJI_RANGES) + "]+"
)

# ── Step 6: units ─────────────────────────────────────────────────────────────
# ORDER IS SUBSTANCE HERE. `km/h` must be consumed before `km`, or the compound
# unit comes out as "chilometri/h" — the exact kind of half-converted string that
# reads worse than the original. The list is applied in sequence for that reason;
# a dict would leave the ordering to chance.
_UNITS = {
    "it": [
        (re.compile(r"±"),                       "più o meno "),
        (re.compile(r"\bkm/h\b", re.IGNORECASE), "chilometri orari"),
        (re.compile(r"\bmm/h\b", re.IGNORECASE), "millimetri all'ora"),
        (re.compile(r"\bm/s\b",  re.IGNORECASE), "metri al secondo"),
        (re.compile(r"\bkm²",    re.IGNORECASE), "chilometri quadrati"),
        (re.compile(r"\bkm\b",   re.IGNORECASE), "chilometri"),
        (re.compile(r"\bmm\b",   re.IGNORECASE), "millimetri"),
        (re.compile(r"\bmin\b",  re.IGNORECASE), "minuti"),
        (re.compile(r"\bm\b"),                   "metri"),
        (re.compile(r"°C\b"),                    " gradi"),
        (re.compile(r"°"),                       " gradi"),
        (re.compile(r"%"),                       " per cento"),
        # "Anello 2/4" is a ratio, not a date: only between digits.
        (re.compile(r"(?<=\d)\s*/\s*(?=\d)"),    " su "),
    ],
    "en": [
        (re.compile(r"±"),                       "plus or minus "),
        (re.compile(r"\bkm/h\b", re.IGNORECASE), "kilometres per hour"),
        (re.compile(r"\bmm/h\b", re.IGNORECASE), "millimetres per hour"),
        (re.compile(r"\bm/s\b",  re.IGNORECASE), "metres per second"),
        (re.compile(r"\bkm²",    re.IGNORECASE), "square kilometres"),
        (re.compile(r"\bkm\b",   re.IGNORECASE), "kilometres"),
        (re.compile(r"\bmm\b",   re.IGNORECASE), "millimetres"),
        (re.compile(r"\bmin\b",  re.IGNORECASE), "minutes"),
        (re.compile(r"\bm\b"),                   "metres"),
        (re.compile(r"°C\b"),                    " degrees"),
        (re.compile(r"°"),                       " degrees"),
        (re.compile(r"%"),                       " percent"),
        (re.compile(r"(?<=\d)\s*/\s*(?=\d)"),    " of "),
    ],
}

# `geo.direction_label` returns 8-point compass abbreviations — N, NE, E, SE, S,
# SO, O, NO in Italian. Spoken, most are harmless letter names and one is a
# disaster: "a 12 km a O" comes out as "a 12 chilometri a o", where the bearing
# has become the conjunction "or". They all get spelled out.
#
# Matched case-sensitively and whole-word, which is what keeps `\bE\b` away from
# the Italian conjunction "e" and `\bO\b` from anything lowercase. Two-letter
# points are listed first for readability; `\b` already prevents `\bN\b` from
# biting into "NE".
_COMPASS = {
    "it": [("NE", "nord-est"), ("SE", "sud-est"), ("SO", "sud-ovest"), ("NO", "nord-ovest"),
           ("N", "nord"), ("E", "est"), ("S", "sud"), ("O", "ovest")],
    "en": [("NE", "northeast"), ("SE", "southeast"), ("SW", "southwest"), ("NW", "northwest"),
           ("N", "north"), ("E", "east"), ("S", "south"), ("W", "west")],
}

_COMPASS_RE = {
    lang: [(re.compile(rf"\b{abbr}\b"), word) for abbr, word in pairs]
    for lang, pairs in _COMPASS.items()
}

# Separators that carry a pause when read but a symbol name when spoken. The
# plain ASCII hyphen is deliberately absent — "nord-est" needs it.
_SEPARATORS = re.compile(r"\s*[·•—–]\s*")

# A line already ending in one of these does not need a full stop bolted on.
_TERMINALS = ".!?…:;,"

_WHITESPACE = re.compile(r"\s+")


def to_spoken(text: str, lang: str = "it") -> str:
    """Rewrite a finished Telegram HTML message as one block of plain prose.

    `text` is the string the caller was about to send, markup and all — one input
    format for every call site, so there is no chance of a message reaching the
    speech engine through a path nobody sanitised.

    `lang` selects the unit vocabulary. Live monitors pass their own
    `cfg["language"]`; everything else takes the "it" default, matching every
    other default in the project. There is no global language setting to consult.
    """
    if not text:
        return ""

    units = _UNITS.get(lang, _UNITS["it"])

    # 1-2. Links become their own label; remaining markup goes.
    text = _ANCHOR_RE.sub(r"\1", text)
    text = _TAG_RE.sub("", text)
    # 3. Entities resolve only AFTER the tags are gone, so a literal `&lt;b&gt;` in
    #    the text is not confused with real markup. Unescaping can itself expose
    #    something tag-shaped, which is then stripped too — both because "less
    #    than b greater than" is not worth reading aloud, and because leaving it
    #    would break idempotency: a second pass would strip what the first kept.
    text = html.unescape(text)
    text = _TAG_RE.sub("", text)
    # 4. A bare URL nobody wrapped in an anchor.
    text = _URL_RE.sub("", text)
    # 5.
    text = _EMOJI_RE.sub("", text)
    # 6.
    text = _SEPARATORS.sub(", ", text)
    for pattern, replacement in units:
        text = pattern.sub(replacement, text)
    for pattern, replacement in _COMPASS_RE.get(lang, _COMPASS_RE["it"]):
        text = pattern.sub(replacement, text)

    # 7. Each surviving line was a standalone fact; as a sentence it needs an end,
    #    or the speech engine runs two of them together into nonsense.
    sentences = []
    for line in text.split("\n"):
        line = _WHITESPACE.sub(" ", line).strip()
        # A line reduced to punctuation by the steps above (a lone emoji, a bare
        # link) is not a sentence and must not become a stray full stop.
        if not line or not any(ch.isalnum() for ch in line):
            continue
        if line[-1] not in _TERMINALS:
            line += "."
        sentences.append(line)

    return " ".join(sentences)
