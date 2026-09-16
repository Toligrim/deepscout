from __future__ import annotations

import re
from dataclasses import dataclass

# Splits on anything that isn't a Unicode "word" character, plus underscore (\w treats
# `_` as a word char, but URL path/filename segments commonly use it as a separator —
# \w is Unicode-aware by default in Python 3's re module, so this handles Cyrillic/etc
# query terms the same way as ASCII ones. This naturally separates URL path segments,
# filenames, and query params (/, -, _, ., ?, &, = all end up as separators) without
# any URL-specific parsing.
_TOKEN_SPLIT = re.compile(r"[\W_]+", re.UNICODE)

# Below this length a token is noise (single letters, stray punctuation remnants) more
# often than a meaningful query term — dropped from both query and candidate tokens.
_MIN_TOKEN_LEN = 2

HIGH_THRESHOLD = 0.6


@dataclass
class RelevanceResult:
    score: float
    tier: str  # "high" | "possible" | "discovered"


def tokenize(text: str | None) -> set[str]:
    if not text:
        return set()
    return {t for t in _TOKEN_SPLIT.split(text.lower()) if len(t) >= _MIN_TOKEN_LEN}


def score_relevance(
    query: str,
    url: str,
    title: str | None = None,
    snippet: str | None = None,
) -> RelevanceResult:
    """Cheap, deterministic lexical relevance — no fetch, no LLM.

    For each query token, take the best match weight among where it was found: the
    URL itself (1.0 — path/filename matches are the strongest signal), the title
    (0.6), or the snippet (0.3). Average those weights over all query tokens for a
    0..1 score. A URL is never dropped for scoring 0 ("discovered") — Deep Search is a
    discovery tool, low-confidence results stay visible, just ranked last.
    """
    query_tokens = tokenize(query)
    if not query_tokens:
        return RelevanceResult(score=0.0, tier="discovered")

    url_tokens = tokenize(url)
    title_tokens = tokenize(title)
    snippet_tokens = tokenize(snippet)

    total = 0.0
    for token in query_tokens:
        if token in url_tokens:
            total += 1.0
        elif token in title_tokens:
            total += 0.6
        elif token in snippet_tokens:
            total += 0.3
    score = total / len(query_tokens)

    if score >= HIGH_THRESHOLD:
        tier = "high"
    elif score > 0:
        tier = "possible"
    else:
        tier = "discovered"
    return RelevanceResult(score=round(score, 4), tier=tier)
