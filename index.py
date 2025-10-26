"""
In-memory indexes and helpers for fast word lookups in exact and partial modes.

This module focuses on performance while keeping logic simple:
- Translate wildcard patterns to regex safely (escape meta chars first).
- Use pre-computed data structures to reduce scanning:
    * len_to_indices: map from word length to indices (helps exact mode quickly).
    * pos_char_index: per-position character -> set of word indices (fast filter).
    * Simple Bloom filters to skip impossible patterns early (cheap heuristics).

Matching modes:
- exact: '?' matches exactly one char; '*' can match 0+ chars; pattern must
    match the whole word (anchored).
- partial: treat as if wrapped by '*' on both sides; '*' also works inside.

All comments are in simple English to satisfy the assignment requirement.
"""

from __future__ import annotations

from typing import Dict, List, Set, Tuple
import re
import functools
import threading

class _SimpleBloom:
    """Very small bitset-based Bloom-like filter (not a full Bloom filter).

    It is used as a quick negative test: if a character/bigram was never seen
    in the corpus, we can skip evaluating expensive regex for that pattern.
    """
    __slots__ = ("_bits", "_size", "_mask")

    def __init__(self, bits_power_of_two: int = 20):
        size = 1 << int(bits_power_of_two)
        self._size = size
        self._bits = 0
        self._mask = size - 1

    @staticmethod
    def _h1(x: int) -> int:
        x ^= (x >> 33)
        x *= 0xff51afd7ed558ccd & ((1 << 64) - 1)
        x &= ((1 << 64) - 1)
        return x

    @staticmethod
    def _h2(x: int) -> int:
        x ^= (x >> 29)
        x *= 0xc4ceb9fe1a85ec53 & ((1 << 64) - 1)
        x &= ((1 << 64) - 1)
        return x

    def _hashes(self, s: str) -> Tuple[int, int, int]:
        h = hash(s)
        if h < 0:
            h = -h
        h1 = self._h1(h) & self._mask
        h2 = self._h2(h) & self._mask
        h3 = (h1 * 1315423911 + h2 * 2654435761) & self._mask
        return h1, h2, h3

    def add(self, s: str) -> None:
        """Set bits for a string using 3 simple hash positions."""
        for h in self._hashes(s):
            self._bits |= (1 << h)

    def maybe_contains(self, s: str) -> bool:
        """Return False if we are sure 's' was not added; otherwise True.

        This can return True for some strings that were not added (false positive),
        but this is acceptable because it only affects performance, not correctness.
        """
        for h in self._hashes(s):
            if (self._bits >> h) & 1 == 0:
                return False
        return True

_blooms_built = False
_blooms_lock = threading.Lock()
_bloom_words = _SimpleBloom(bits_power_of_two=20)
_bloom_letters = _SimpleBloom(bits_power_of_two=16)
_bloom_bigrams = _SimpleBloom(bits_power_of_two=18)


def _init_blooms(words_lower: List[str]) -> None:
    """Initialize global Bloom-like structures once using the word list."""
    global _blooms_built
    if _blooms_built:
        return
    with _blooms_lock:
        if _blooms_built:
            return
        for wl in words_lower:
            _bloom_words.add(wl)
            for ch in set(wl):
                _bloom_letters.add(ch)
            if len(wl) >= 2:
                for i in range(len(wl) - 1):
                    _bloom_bigrams.add(wl[i : i + 2])
        _blooms_built = True


def _letter_segments(pattern: str) -> List[str]:
    """Split pattern into literal segments, removing '?' and '*' separators."""
    segs: List[str] = []
    cur = []
    for ch in pattern:
        if ch in ('?', '*'):
            if cur:
                segs.append(''.join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        segs.append(''.join(cur))
    return segs


def should_skip_pattern(pattern: str) -> bool:
    """Fast check to skip impossible patterns before regex.

    - If pattern has no letters (only '?' and '*'), we cannot rule it out.
    - If a literal letter is not in the corpus (by Bloom), skip.
    - If a literal bigram never appears (by Bloom), skip.
    """
    if not pattern:
        return True
    p = pattern.lower()
    has_letter = any(ch not in ('?', '*') for ch in p)
    if not has_letter:
        return False
    for ch in set(c for c in p if c not in ('?', '*')):
        if not _bloom_letters.maybe_contains(ch):
            return True
    for seg in _letter_segments(p):
        if len(seg) >= 2:
            for i in range(len(seg) - 1):
                bg = seg[i : i + 2]
                if not _bloom_bigrams.maybe_contains(bg):
                    return True
    return False


@functools.lru_cache(maxsize=256)
def _compile_regex_body(body: str, anchored: bool) -> re.Pattern:
    """Compile a regex from pre-escaped body; optionally anchor to ^...$.

    We keep a small LRU cache because many patterns repeat across clients.
    """
    pat = f"^{body}$" if anchored else body
    return re.compile(pat, re.IGNORECASE)


def _wildcard_to_regex_body(pattern: str, allow_star: bool, partial: bool) -> str:
    """Convert our wildcard pattern to a safe regex body string.

    - Escape regex meta characters so user input is safe.
    - Replace '?' with '.'; optionally replace '*' with '.*' when allowed.
    - In partial mode, wrap with '.*' to allow substring matches.
    """
    esc = []
    for ch in pattern:
        if ch in ".^$+{}[]|()\\":
            esc.append("\\" + ch)
        elif ch == '?':
            esc.append('.')
        elif ch == '*' and allow_star:
            esc.append('.*')
        else:
            esc.append(ch)
    body = ''.join(esc)
    if partial:
        return '.*' + body + '.*'
    return body


class WordIndex:
    """Word index to accelerate both exact and partial matching.

    Data members:
    - words: original words (case preserved)
    - words_lower: lowercase version for case-insensitive search
    - len_to_indices: map length -> list of indices
    - pos_char_index: for each length, list[ position -> {char -> set(indices)} ]
    """
    def __init__(self, words: List[str]):
        self.words: List[str] = words
        self.words_lower: List[str] = [w.lower() for w in words]
        self.len_to_indices: Dict[int, List[int]] = {}
        self.pos_char_index: Dict[int, List[Dict[str, Set[int]]]] = {}
        self._build_indexes()
        _init_blooms(self.words_lower)

    def _build_indexes(self) -> None:
        """Build length buckets and per-position character index."""
        for i, w in enumerate(self.words_lower):
            L = len(w)
            self.len_to_indices.setdefault(L, []).append(i)
        for L, idxs in self.len_to_indices.items():
            by_pos: List[Dict[str, Set[int]]] = [dict() for _ in range(L)]
            for i in idxs:
                wl = self.words_lower[i]
                for p, ch in enumerate(wl):
                    d = by_pos[p]
                    s = d.get(ch)
                    if s is None:
                        s = set()
                        d[ch] = s
                    s.add(i)
            self.pos_char_index[L] = by_pos

    # ---------- Exact mode ----------
    def _exact_indices_via_pos_index(self, pattern: str) -> List[int]:
        """Return candidate word indices that match an exact '?' pattern.

        We only use the fast position/character index when the pattern does
        not contain '*', because '*' changes the length relationship.
        """
        L = len(pattern)
        idxs = self.len_to_indices.get(L)
        if not idxs:
            return []
        pat = pattern.lower()
        fixed_positions = [(i, c) for i, c in enumerate(pat) if c != '?']
        if not fixed_positions:
            return list(idxs)
        by_pos = self.pos_char_index[L]
        candidate: Set[int] | None = None
        for pos, ch in fixed_positions:
            s = by_pos[pos].get(ch, set())
            if candidate is None:
                candidate = set(s)
            else:
                candidate &= s
            if not candidate:
                return []
        assert candidate is not None
        cand = candidate
        return [i for i in idxs if i in cand]

    def find_exact(self, pattern: str) -> List[str]:
        """Find words that match the pattern exactly (anchors, '?' and '*')."""
        if should_skip_pattern(pattern):
            return []
        if '*' not in pattern:
            idxs = self._exact_indices_via_pos_index(pattern)
            return [self.words[i] for i in idxs]
        min_len = sum(1 for ch in pattern if ch != '*')
        body = _wildcard_to_regex_body(pattern, allow_star=True, partial=False)
        rx = _compile_regex_body(body, anchored=True)
        result: List[str] = []
        for L, idxs in self.len_to_indices.items():
            if L < min_len:
                continue
            for i in idxs:
                if rx.fullmatch(self.words[i]) is not None:
                    result.append(self.words[i])
        return result

    def count_exact(self, pattern: str) -> int:
        """Count words matching in exact mode without materializing the list."""
        if should_skip_pattern(pattern):
            return 0
        if '*' not in pattern:
            return len(self._exact_indices_via_pos_index(pattern))
        min_len = sum(1 for ch in pattern if ch != '*')
        body = _wildcard_to_regex_body(pattern, allow_star=True, partial=False)
        rx = _compile_regex_body(body, anchored=True)
        cnt = 0
        for L, idxs in self.len_to_indices.items():
            if L < min_len:
                continue
            for i in idxs:
                if rx.fullmatch(self.words[i]) is not None:
                    cnt += 1
        return cnt

    # ---------- Partial mode ----------
    def find_partial(self, pattern: str) -> List[str]:
        """Find words that contain a substring matching the pattern.

        If the pattern is only '?' characters, we simply return words that are
        at least that length.
        """
        if should_skip_pattern(pattern):
            return []
        if '*' not in pattern:
            L = len(pattern)
            if all(ch == '?' for ch in pattern):
                return [w for w in self.words if len(w) >= L]
            body = _wildcard_to_regex_body(pattern, allow_star=False, partial=True)
            rx = _compile_regex_body(body, anchored=False)
            result2: List[str] = []
            for LL, idxs in self.len_to_indices.items():
                if LL < L:
                    continue
                for i in idxs:
                    if rx.search(self.words[i]) is not None:
                        result2.append(self.words[i])
            return result2
        min_len = sum(1 for ch in pattern if ch != '*')
        body = _wildcard_to_regex_body(pattern, allow_star=True, partial=True)
        rx = _compile_regex_body(body, anchored=False)
        out: List[str] = []
        for LL, idxs in self.len_to_indices.items():
            if LL < min_len:
                continue
            for i in idxs:
                if rx.search(self.words[i]) is not None:
                    out.append(self.words[i])
        return out

    def count_partial(self, pattern: str) -> int:
        """Count matches for partial mode without building the list."""
        if should_skip_pattern(pattern):
            return 0
        if '*' not in pattern:
            L = len(pattern)
            if all(ch == '?' for ch in pattern):
                return sum(len(idxs) for LL, idxs in self.len_to_indices.items() if LL >= L)
            body = _wildcard_to_regex_body(pattern, allow_star=False, partial=True)
            rx = _compile_regex_body(body, anchored=False)
            cnt = 0
            for LL, idxs in self.len_to_indices.items():
                if LL < L:
                    continue
                for i in idxs:
                    if rx.search(self.words[i]) is not None:
                        cnt += 1
            return cnt
        min_len = sum(1 for ch in pattern if ch != '*')
        body = _wildcard_to_regex_body(pattern, allow_star=True, partial=True)
        rx = _compile_regex_body(body, anchored=False)
        cnt2 = 0
        for LL, idxs in self.len_to_indices.items():
            if LL < min_len:
                continue
            for i in idxs:
                if rx.search(self.words[i]) is not None:
                    cnt2 += 1
        return cnt2


def handle_batch(index: 'WordIndex', patterns: List[str], mode: str = 'exact') -> List[List[str]]:
    """Helper to process a list of patterns in given mode; returns match lists."""
    out: List[List[str]] = []
    for pat in patterns:
        if mode == 'partial':
            out.append(index.find_partial(pat))
        else:
            out.append(index.find_exact(pat))
    return out
