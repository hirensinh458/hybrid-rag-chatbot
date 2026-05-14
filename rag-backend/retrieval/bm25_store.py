# retrieval/bm25_store.py
#
# CHANGES vs previous version:
#   - _tokenize() function added — replaces bare .lower().split() everywhere.
#     Same function runs at BOTH index time (_rebuild) and query time (search)
#     so the vocabulary is always consistent.
#
#   WHY THIS MATTERS:
#     BM25 is a bag-of-words model. It scores chunks by TF * IDF per token.
#     IDF naturally downweights common words, but they still:
#       1. Add noise to score computation in short 300-token chunks
#       2. Waste slots in the BM25 vocabulary
#       3. Can cause misleading matches when a stopword is rare in a small corpus
#     Ship manuals are technical — every token that survives filtering carries
#     real signal. "engine cooling system failure" → 4 meaningful tokens, not 6
#     with "the" and "is" diluting the match.
#
#   DESIGN CHOICES:
#     - Hardcoded stopword set: zero new dependencies (no NLTK download).
#       ~80 words covering all genuinely meaningless English tokens.
#     - Kept: hyphens (anti-corrosion), degree symbol (°C), slashes (/),
#       dots in decimals (3.2), percent (%), underscores.
#     - Dropped: commas, colons, parentheses, quotes, etc.
#     - Minimum token length: 2 chars (filters "a", "i" etc not in stoplist)
#     - NO stemming: "cooling" and "cool" are different in a manual context.
#       "Cooling system" ≠ "cool", don't conflate them.

import os
import re
import sys
import pickle
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import BM25_PATH, settings   # add `settings` to existing import

from rank_bm25 import BM25Okapi


# ─────────────────────────────────────────────────────────
# TOKENIZER
# ─────────────────────────────────────────────────────────

# Curated English stopwords — safe to remove in a technical/maritime domain.
# Deliberately NOT including domain-relevant words even if common:
#   kept: "system", "pressure", "level", "oil", "water", "check", "use"
#   removed: "the", "is", "in", "at", "by", "of", "a", ...
_STOPWORDS: frozenset[str] = frozenset({
    # Articles
    "a", "an", "the",
    # Coordinating conjunctions
    "and", "or", "but", "nor", "so", "yet",
    # Prepositions
    "in", "on", "at", "to", "for", "of", "with", "by", "from", "into",
    "through", "during", "before", "after", "above", "below", "between",
    "out", "off", "over", "under", "up", "about", "against", "along",
    "among", "around", "as", "except", "per", "than", "toward", "within",
    # Auxiliary / linking verbs
    "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did",
    "will", "would", "could", "should", "may", "might", "shall", "can",
    "need", "must", "am",
    # Pronouns
    "it", "its", "this", "that", "these", "those",
    "i", "we", "you", "he", "she", "they",
    "me", "us", "him", "her", "them",
    "my", "our", "your", "his", "their",
    # Interrogatives / relatives
    "what", "which", "who", "whom", "when", "where", "how", "why",
    # Negation / quantifiers / adverbs
    "not", "no", "also", "if", "then", "just", "only", "very", "too",
    "again", "further", "once", "here", "there",
    # Determiners
    "all", "any", "both", "each", "few", "more", "most", "other",
    "some", "such", "same", "either", "neither",
    # Misc function words
    "than", "s", "t", "re",
})

# Strip characters that carry no lexical meaning.
# Kept intentionally:  . (decimals: 3.2)  - (hyphenated: anti-corrosion)
#                      ° (units: 60°C)     / (fractions: 1/2, and/or)
#                      % (percentages)      _ (underscores in identifiers)
_PUNCT_STRIP = re.compile(r"[^\w\s.\-°/%]")

# Collapse multiple whitespace into one
_WHITESPACE  = re.compile(r"\s+")


def _tokenize(text: str) -> list[str]:
    """
    Normalize and tokenize a text string for BM25 indexing and querying.

    Steps:
      1. Lowercase
      2. Strip non-meaningful punctuation (keep . - ° / % _)
      3. Split on whitespace
      4. Drop stopwords and single-character tokens

    Must be called identically at index time (_rebuild) and query time (search)
    so the vocabulary always matches. Both calls use this same function.

    Example:
        "The engine cooling-water temperature must be 60°C!"
        → ["engine", "cooling-water", "temperature", "60°c"]

    Args:
        text: raw string (chunk content or query)

    Returns:
        list of meaningful tokens, lowercase, punctuation-stripped
    """
    # 1. Lowercase
    text = text.lower()

    # 2. Strip punctuation (keep . - ° / % _)
    text = _PUNCT_STRIP.sub(" ", text)

    # 3. Collapse whitespace
    text = _WHITESPACE.sub(" ", text).strip()

    # 4. Split, then filter stopwords and very short tokens
    return [
        token
        for token in text.split()
        if len(token) >= 2 and token not in _STOPWORDS
    ]


# ─────────────────────────────────────────────────────────
# BM25 STORE
# ─────────────────────────────────────────────────────────

class BM25Store:
    """
    Persistent BM25 sparse index for keyword-based retrieval.

    Persists to disk (pickle) so the index survives backend restarts.
    Used as the sparse leg of the hybrid BM25 + dense vector retrieval.

    The _tokenize() function is used consistently at both:
      - Index time: _rebuild() tokenizes every stored chunk's content
      - Query time: search() tokenizes the incoming query string
    This consistency is required — BM25 scores are only meaningful if
    the vocabulary at index time matches the vocabulary at query time.
    """

    # REPLACE the existing __init__ method inside class BM25Store with this:

    def __init__(self, path: str = None, tenant_slug: str = None):
        """
        Initialise the BM25 store.

        Resolution order for the pickle file path:
        1. Explicit `path` argument (legacy / internal callers) — used as-is.
        2. `tenant_slug` provided → data/bm25/bm25_{tenant_slug}.pkl
            (per-tenant isolation; directory created automatically).
        3. Neither provided → global BM25_PATH constant (single-tenant dev mode,
            backward compatible with all existing callers).

        Args:
            path:        Explicit file path. Takes priority over tenant_slug.
            tenant_slug: Tenant identifier. Derives path automatically when set.
        """
        if path is not None:
            # Legacy / explicit path — unchanged behaviour
            self._path = path
        elif tenant_slug:
            # Multi-tenant: one .pkl file per tenant under data/bm25/
            bm25_dir = Path(settings.qdrant_path).parent / "bm25"
            bm25_dir.mkdir(parents=True, exist_ok=True)
            self._path = str(bm25_dir / f"bm25_{tenant_slug}.pkl")
        else:
            # Single-tenant dev mode fallback
            self._path = BM25_PATH

        self._chunks: list[dict] = []
        self._bm25: BM25Okapi    = None
        self._load()

    # ── persistence ───────────────────────────────────────

    def _load(self) -> None:
        if not Path(self._path).exists():
            print("  [BM25] No saved index — will build on first ingest")
            return
        try:
            with open(self._path, "rb") as f:
                data         = pickle.load(f)
            self._chunks = data.get("chunks", [])
            self._bm25   = data.get("bm25")
            print(f"  [BM25] Loaded {len(self._chunks)} docs from disk")
        except Exception as e:
            print(f"  [BM25] Load failed ({e}) — starting fresh")
            self._chunks = []
            self._bm25   = None

    def _save(self) -> None:
        try:
            with open(self._path, "wb") as f:
                pickle.dump({"chunks": self._chunks, "bm25": self._bm25}, f)
        except Exception as e:
            print(f"  [BM25] Save failed: {e}")

    def _rebuild(self) -> None:
        """
        Rebuild BM25Okapi from current _chunks.

        CHANGED: was `c["content"].lower().split()`
                 now `_tokenize(c["content"])`

        This removes stopwords and normalises punctuation at index time,
        matching exactly what search() does at query time.
        """
        if not self._chunks:
            self._bm25 = None
            return

        tokenized  = [_tokenize(c["content"]) for c in self._chunks]
        self._bm25 = BM25Okapi(tokenized)

    # ── write ─────────────────────────────────────────────

    def build(self, chunks: list[dict]) -> None:
        """Replace entire index with a new set of chunks."""
        self._chunks = chunks
        self._rebuild()
        self._save()
        print(f"  [BM25] Index built with {len(chunks)} documents.")

    def add(self, chunks: list[dict]) -> None:
        """Append new chunks to the existing index."""
        if not chunks:
            return
        self._chunks.extend(chunks)
        self._rebuild()
        self._save()
        print(f"  [BM25] Index now has {len(self._chunks)} docs")

    def delete_by_source(self, filename: str) -> int:
        """
        Remove all chunks from a specific source file.
        Rebuilds and saves after removal.
        Returns number of chunks removed.
        """
        before        = len(self._chunks)
        self._chunks  = [c for c in self._chunks if c.get("source") != filename]
        removed       = before - len(self._chunks)
        self._rebuild()
        self._save()
        print(
            f"  [BM25] Removed {removed} chunks for source='{filename}'. "
            f"Index now has {len(self._chunks)} docs"
        )
        return removed

    def reset(self) -> None:
        """Wipe the entire index from memory and disk."""
        self._chunks = []
        self._bm25   = None
        if Path(self._path).exists():
            Path(self._path).unlink()
        print("  [BM25] Index reset")

    # ── read ──────────────────────────────────────────────

    def search(self, query: str, top_k: int = 20) -> list[dict]:
        """
        Search the BM25 index for the top_k most relevant chunks.

        CHANGED: was `query.lower().split()`
                 now `_tokenize(query)`

        Using the same _tokenize() as _rebuild() guarantees the query tokens
        exist in the same vocabulary as the indexed tokens. A query token that
        was never seen during indexing contributes nothing to BM25 — that's
        correct behaviour, not a bug.

        Chunks with score == 0 are filtered out (no keyword overlap at all).

        Args:
            query:  raw user query string
            top_k:  max results to return

        Returns:
            list of chunk dicts with added "score" field, sorted descending
        """
        if not self._bm25 or not self._chunks:
            return []

        # CHANGED: use _tokenize instead of .lower().split()
        tokens = _tokenize(query)

        if not tokens:
            # Query was entirely stopwords — no signal, return empty
            print("  [BM25] Query reduced to empty after tokenization — no results")
            return []

        scores = self._bm25.get_scores(tokens)
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)

        results: list[dict] = []
        for idx, score in ranked[:top_k]:
            if score <= 0:
                continue   # no keyword overlap with this chunk at all
            chunk          = self._chunks[idx].copy()
            chunk["score"] = round(float(score), 4)
            results.append(chunk)

        return results

    def __len__(self) -> int:
        return len(self._chunks)


__all__ = ["BM25Store", "_tokenize"]