"""Quote verification against the paper text and Location mapping. (Phase 3, BUILD_PLAN §7.5)

A model quote is kept only if code finds it in `Document.text`. Both sides are reduced to a *match key*:
Unicode NFKC (which also expands ligatures), case-folded, typographic quotes unified, every hyphen and dash
dropped (so line-break hyphenation and "state-of-the-art" vs "state of the art" do not matter), soft
hyphens and zero-width marks dropped, whitespace collapsed. A character map takes key offsets back to
offsets in `Document.text`.

Tiers, first hit wins:
1. `exact`: the quote key is a substring of the document key (search regions first, then the whole text).
2. `exact_nonum`: the same after deleting isolated 1–4 digit tokens on both sides, which removes review
   line numbers pasted inside a quote (the document text has them stripped already).
3. `fuzzy`: `rapidfuzz.fuzz.partial_ratio_alignment` ≥ 92 against windows of two consecutive paragraphs.
4. otherwise the quote is dropped and counted by the caller.

The stored quote is always the document's own text at the matched span, never the model's version.
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_left
from dataclasses import dataclass
from typing import Literal

from rapidfuzz import fuzz

from onus.ingest.lines import Locator, normalise
from onus.schemas.document import Document
from onus.schemas.result import Location

FUZZY_MIN = 92.0
MIN_QUOTE_CHARS = 20

MatchMethod = Literal["exact", "exact_nonum", "fuzzy"]

_DROP = set("-\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe63\uff0d\u00ad\u200b\u200c\u200d\u2060\ufeff")
_INVISIBLE_DROP = set("\u00ad\u200b\u200c\u200d\u2060\ufeff")
_MAP = {"“": '"', "”": '"', "„": '"', "«": '"', "»": '"', "‘": "'", "’": "'", "‚": "'", "`": "'", "´": "'"}
# Isolated 1–4 digit tokens: not part of a decimal, a thousands group, a percentage or a word.
_ISOLATED_INT = re.compile(r"(?<![\w.,])\d{1,4}(?![\w%]|[.,]\d) ?")


@dataclass(frozen=True)
class QuoteMatch:
    start: int  # offsets into Document.text
    end: int
    method: MatchMethod
    score: float


def keyify(text: str) -> tuple[str, list[int]]:
    """Match key of `text` and, per key character, its offset in `text`."""
    out: list[str] = []
    idx: list[int] = []
    after_dash = False  # "interac- tion": a dash then whitespace after a letter joins the word
    for i, ch in enumerate(text):
        for c in unicodedata.normalize("NFKC", ch).casefold():
            c = _MAP.get(c, c)
            if c in _DROP:
                after_dash = c not in _INVISIBLE_DROP or after_dash
                continue
            if c.isspace():
                if not out or out[-1] == " " or (after_dash and out[-1].isalnum()):
                    continue
                c = " "
            after_dash = False
            out.append(c)
            idx.append(i)
    if out and out[-1] == " ":
        out.pop()
        idx.pop()
    return "".join(out), idx


def match_key(text: str) -> str:
    return keyify(text)[0]


def _strip_ints(key: str, idx: list[int]) -> tuple[str, list[int]]:
    keep = [True] * len(key)
    for m in _ISOLATED_INT.finditer(key):
        for k in range(m.start(), m.end()):
            keep[k] = False
    chars: list[str] = []
    out_idx: list[int] = []
    for k, c in enumerate(key):
        if not keep[k] or (c == " " and (not chars or chars[-1] == " ")):
            continue
        chars.append(c)
        out_idx.append(idx[k])
    if chars and chars[-1] == " ":
        chars.pop()
        out_idx.pop()
    return "".join(chars), out_idx


def _is_word(c: str) -> bool:
    return c.isalnum()


class QuoteVerifier:
    """Finds model quotes in one Document and maps them to `Location`s."""

    def __init__(self, doc: Document):
        self.doc = doc
        self.locator = Locator(doc)
        self.key, self.idx = keyify(doc.text)
        self._nonum: tuple[str, list[int]] | None = None

    # ------------------------------------------------------------ offsets

    def key_pos(self, offset: int) -> int:
        """First key position whose source offset is >= `offset`."""
        return bisect_left(self.idx, offset)

    def _span(self, idx: list[int], ks: int, ke: int) -> tuple[int, int]:
        return idx[ks], idx[ke - 1] + 1

    def _snap(self, start: int, end: int) -> tuple[int, int]:
        """Extend a span to whole words and trim surrounding whitespace."""
        t = self.doc.text
        while start > 0 and _is_word(t[start - 1]) and _is_word(t[start]):
            start -= 1
        while end < len(t) and _is_word(t[end - 1]) and _is_word(t[end]):
            end += 1
        while start < end and t[start].isspace():
            start += 1
        while end > start and t[end - 1].isspace():
            end -= 1
        return start, end

    # ------------------------------------------------------------ search

    def find(self, quote: str, regions: list[tuple[int, int]] | None = None) -> QuoteMatch | None:
        """Locate `quote`; `regions` (offset ranges of Document.text) are searched before the whole text."""
        qkey, _ = keyify(quote)
        if len(qkey) < MIN_QUOTE_CHARS:
            return None
        windows = [(self.key_pos(s), self.key_pos(e)) for s, e in regions or []] + [(0, len(self.key))]

        for ks, ke in windows:
            pos = self.key.find(qkey, ks, ke)
            if pos >= 0:
                return QuoteMatch(*self._snap(*self._span(self.idx, pos, pos + len(qkey))), "exact", 100.0)

        q2, _ = _strip_ints(qkey, list(range(len(qkey))))
        if q2 != qkey and len(q2) >= MIN_QUOTE_CHARS:
            k2, i2 = self._nonum_key()
            for s, e in [(s, e) for s, e in regions or []] + [(0, len(self.doc.text))]:
                ks, ke = bisect_left(i2, s), bisect_left(i2, e)
                pos = k2.find(q2, ks, ke)
                if pos >= 0:
                    return QuoteMatch(*self._snap(*self._span(i2, pos, pos + len(q2))), "exact_nonum", 100.0)

        return self._fuzzy(qkey, regions)

    def _nonum_key(self) -> tuple[str, list[int]]:
        if self._nonum is None:
            self._nonum = _strip_ints(self.key, self.idx)
        return self._nonum

    def _fuzzy(self, qkey: str, regions: list[tuple[int, int]] | None) -> QuoteMatch | None:
        paras = self.doc.paragraphs
        in_region = [i for i, p in enumerate(paras) if any(p.start < e and p.end > s for s, e in regions or [])]
        for candidates in (in_region, range(len(paras))):
            best: tuple[float, int, int] | None = None
            for i in candidates:
                ks = self.key_pos(paras[i].start)
                ke = self.key_pos(paras[min(i + 1, len(paras) - 1)].end)
                window = self.key[ks:ke]
                if len(window) < 0.9 * len(qkey):
                    continue
                al = fuzz.partial_ratio_alignment(qkey, window, score_cutoff=FUZZY_MIN)
                if al is None or al.score < FUZZY_MIN:
                    continue
                ds, de = ks + al.dest_start, ks + al.dest_end
                if de <= ds:
                    continue
                if best is None or al.score > best[0]:
                    best = (al.score, ds, de)
            if best is not None:
                start, end = self._snap(*self._span(self.idx, best[1], best[2]))
                return QuoteMatch(start, end, "fuzzy", round(best[0], 1))
        return None

    # ------------------------------------------------------------ locate

    def locate(self, start: int, end: int) -> Location | None:
        """`Location` for a span; a span starting in a heading moves to the first paragraph it reaches."""
        loc = self.locator.locate(start, end)
        if loc is not None:
            return loc
        for p in self.doc.paragraphs:
            if start <= p.start < end:
                return self.locator.locate(p.start, end)
        return None

    def sentences_spanned(self, start: int, end: int) -> int:
        n = 0
        for p in self.doc.paragraphs:
            if p.end <= start or p.start >= end:
                continue
            n += sum(1 for s, e in p.sentences if s < end and e > start)
        return n

    def text(self, start: int, end: int) -> str:
        return normalise(self.doc.text[start:end])
