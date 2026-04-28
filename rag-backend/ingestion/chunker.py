# ingestion/chunker.py
#
# CHANGES vs previous version:
#   - HierarchicalChunker.chunk_hierarchical() embeds parent_content
#     directly on each child chunk dict (no separate parents dict / SQLite).
#   - Return signature of chunk_hierarchical() changed:
#       OLD: (children: list[dict], parents: dict)
#       NEW: children: list[dict]   (parents embedded inline)
#
# ── BUG 1 FIX ─────────────────────────────────────────────────────────────
#   _group_by_section() now also starts a new group whenever the PAGE NUMBER
#   changes, in addition to the existing section_path / heading triggers.
#
#   ROOT CAUSE:
#     When PDF heading detection fails (very common for ship manuals that use
#     consistent font sizes with no clear size jumps), every block comes out
#     with section_path="" and type="text".  Neither the heading nor the
#     section_path trigger ever fires, so the ENTIRE document landed in one
#     giant group.  That group's meta_base was stamped with group[0]["page"]
#     (= page 1), so every child chunk across the whole PDF reported page 1.
#
#   FIX:
#     Add a third OR-condition: `block.get("page") != current[-1].get("page")`
#     This guarantees that blocks from different pages are always placed in
#     separate groups, even if heading detection completely fails.
#     Result: each chunk now carries the correct page number for its content.
#
# ── NEW: SENTENCE-BASED CHUNKING ──────────────────────────────────────────
#   Added two new strategies:
#     - "sentence"              : SentenceChunker (flat, no parent-child)
#     - "sentence_hierarchical" : SentenceHierarchicalChunker (RECOMMENDED)
#
#   WHY SENTENCE-BASED IS BETTER THAN CHARACTER-BASED:
#     Character chunking cuts mid-sentence, destroying semantic coherence.
#     Sentence-based chunking preserves complete thoughts, which significantly
#     improves embedding quality and retrieval precision — especially for
#     technical manuals where one sentence = one fact.
#
#   SPLITTER PRIORITY (auto-detected at import time):
#     1. nltk       — works on ALL Python versions including 3.14+
#                     handles abbreviations (Fig., e.g., i.e., No.)
#     2. spacy      — better linguistic accuracy, Python <3.14 only
#     3. regex      — zero deps, always works, fallback of last resort
#
#   OPTIMAL SIZES (for technical manuals & dense PDFs):
#     Child : 4 sentences, overlap 1  →  ~200-400 chars per chunk
#     Parent: 12 sentences, overlap 2 →  ~800-1200 chars per chunk
# ──────────────────────────────────────────────────────────────────────────

import os
import sys
import hashlib

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_text_splitters import (
    RecursiveCharacterTextSplitter,
    CharacterTextSplitter,
)
from config import (
    CHUNK_SIZE, CHUNK_OVERLAP,
    CHILD_CHUNK_SIZE, CHILD_CHUNK_OVERLAP,
    PARENT_CHUNK_SIZE, PARENT_CHUNK_OVERLAP,
)

# Atomic types — must never be re-chunked (splitting destroys their meaning)
_ATOMIC_TYPES = {"table", "image"}


# ─────────────────────────────────────────────────────────
# SENTENCE SPLITTER  (nltk → spacy → regex fallback)
# ─────────────────────────────────────────────────────────

def _load_sentence_splitter():
    """
    Returns a callable: text -> list[str]

    Priority order (chosen for maximum Python version compatibility):
      1. nltk  — works on ALL Python versions including 3.14+, no binary deps,
                 handles abbreviations (Fig., e.g., i.e., No.) via punkt model
      2. spacy — better linguistic accuracy, but Python <3.14 only (no 3.14 wheels)
      3. regex — zero deps, always works, fallback of last resort

    Called ONCE at module import time — result cached in _split_sentences.
    All subsequent calls go directly to the cached function with no overhead.
    """

    # ── 1. nltk (preferred — Python 3.14 compatible) ──────────────────────
    try:
        import nltk

        # punkt_tab is required in nltk 3.9+ — download both for safety
        for resource in ("tokenizers/punkt_tab", "tokenizers/punkt"):
            try:
                nltk.data.find(resource)
            except LookupError:
                # resource name without path prefix for download()
                nltk.download(resource.split("/")[-1], quiet=True)

        from nltk.tokenize import sent_tokenize

        def _nltk_split(text: str) -> list[str]:
            return [s.strip() for s in sent_tokenize(text) if s.strip()]

        print("[CHUNKER] Sentence splitter: nltk")
        return _nltk_split

    except ImportError:
        pass

    # ── 2. spacy (Python 3.9–3.13 only — no 3.14 wheels as of Apr 2026) ──
    try:
        import spacy

        try:
            # Full pipeline — disable unused components for speed
            nlp = spacy.load(
                "en_core_web_sm",
                disable=["ner", "tagger", "lemmatizer"],
            )
        except OSError:
            # Model not downloaded — fall back to blank + sentencizer
            nlp = spacy.blank("en")
            if "sentencizer" not in nlp.pipe_names:
                nlp.add_pipe("sentencizer")

        def _spacy_split(text: str) -> list[str]:
            doc = nlp(text)
            return [s.text.strip() for s in doc.sents if s.text.strip()]

        print("[CHUNKER] Sentence splitter: spacy")
        return _spacy_split

    except ImportError:
        pass

    # ── 3. Regex fallback — zero deps, always available ───────────────────
    # Splits on ". " / "! " / "? " followed by a capital letter.
    # Does NOT handle abbreviations — installs nltk for production use.
    import re as _re
    _SENT_RE = _re.compile(r'(?<=[.!?])\s+(?=[A-Z])')

    def _regex_split(text: str) -> list[str]:
        parts = _SENT_RE.split(text)
        return [p.strip() for p in parts if p.strip()]

    print(
        "[CHUNKER] Sentence splitter: regex fallback "
        "(install nltk for better quality: pip install nltk==3.9.1)"
    )
    return _regex_split


# Module-level singleton — splitter resolved once at import, never again
_split_sentences = _load_sentence_splitter()


def _sentences_to_chunks(
    sentences: list[str],
    window   : int,
    overlap  : int,
) -> list[str]:
    """
    Sliding-window sentence grouping.

    Args:
        sentences : pre-split sentence list
        window    : number of sentences per chunk
        overlap   : number of sentences to repeat from the previous chunk
                    (always whole sentences — no mid-sentence cuts)

    Example  window=4, overlap=1:
        chunk 0 → sents [0, 1, 2, 3]
        chunk 1 → sents [3, 4, 5, 6]   ← sent 3 repeated for continuity
        chunk 2 → sents [6, 7, 8, 9]

    Always returns at least one chunk even if len(sentences) < window.
    Empty sentence lists return an empty list.
    """
    if not sentences:
        return []

    chunks: list[str] = []
    step  : int       = max(1, window - overlap)
    i     : int       = 0

    while i < len(sentences):
        window_sents = sentences[i : i + window]
        chunks.append(" ".join(window_sents))
        if i + window >= len(sentences):
            break
        i += step

    return chunks


# ─────────────────────────────────────────────────────────
# BASE CHUNKER
# ─────────────────────────────────────────────────────────

class BaseChunker:
    def __init__(self, chunk_size: int = CHUNK_SIZE, chunk_overlap: int = CHUNK_OVERLAP):
        self.chunk_size    = chunk_size
        self.chunk_overlap = chunk_overlap
        self.strategy_name = "base"

    def chunk(self, text: str) -> list[str]:
        raise NotImplementedError("Subclasses must implement chunk()")

    def chunk_documents(self, docs: list[dict]) -> list[dict]:
        result: list[dict] = []
        for doc in docs:
            if doc.get("type") in _ATOMIC_TYPES:
                doc["chunk_index"]  = 0
                doc["total_chunks"] = 1
                doc["strategy"]     = "none"
                result.append(doc)
                continue

            sub_chunks = self.chunk(doc["content"])
            total      = len(sub_chunks)

            for i, sub in enumerate(sub_chunks):
                new_doc                 = doc.copy()
                new_doc["content"]      = sub
                new_doc["chunk_index"]  = i
                new_doc["total_chunks"] = total
                new_doc["strategy"]     = self.strategy_name
                result.append(new_doc)

        return result

    def get_stats(self, chunks: list[str]) -> dict:
        if not chunks:
            return {}
        lengths = [len(c) for c in chunks]
        return {
            "strategy"    : self.strategy_name,
            "total_chunks": len(chunks),
            "avg_length"  : round(sum(lengths) / len(lengths)),
            "min_length"  : min(lengths),
            "max_length"  : max(lengths),
        }


# ─────────────────────────────────────────────────────────
# STRATEGY 1 — FIXED SIZE
# ─────────────────────────────────────────────────────────

class FixedSizeChunker(BaseChunker):
    """
    Splits text into fixed-size character chunks with overlap.
    Simplest strategy — fast but cuts mid-sentence.
    Use only when document structure is unknown or irrelevant.
    """

    def __init__(self, chunk_size: int = CHUNK_SIZE, chunk_overlap: int = CHUNK_OVERLAP):
        super().__init__(chunk_size, chunk_overlap)
        self.strategy_name = "fixed_size"
        self._splitter = CharacterTextSplitter(
            chunk_size    = self.chunk_size,
            chunk_overlap = self.chunk_overlap,
            separator     = "\n"
        )

    def chunk(self, text: str) -> list[str]:
        chunks = self._splitter.split_text(text)
        return [c.strip() for c in chunks if c.strip()]


# ─────────────────────────────────────────────────────────
# STRATEGY 2 — RECURSIVE
# ─────────────────────────────────────────────────────────

class RecursiveChunker(BaseChunker):
    """
    Recursively splits on paragraph → line → sentence → word boundaries.
    Better than fixed-size (respects natural breaks) but still character-based.
    """

    def __init__(self, chunk_size: int = CHUNK_SIZE, chunk_overlap: int = CHUNK_OVERLAP):
        super().__init__(chunk_size, chunk_overlap)
        self.strategy_name = "recursive"
        self._splitter = RecursiveCharacterTextSplitter(
            chunk_size    = self.chunk_size,
            chunk_overlap = self.chunk_overlap,
            separators    = ["\n\n", "\n", ". ", "! ", "? ", ", ", " ", ""]
        )

    def chunk(self, text: str) -> list[str]:
        chunks = self._splitter.split_text(text)
        return [c.strip() for c in chunks if c.strip()]


# ─────────────────────────────────────────────────────────
# STRATEGY 3 — HIERARCHICAL PARENT-CHILD  (character-based)
# ─────────────────────────────────────────────────────────

class HierarchicalChunker(BaseChunker):
    """
    Small-to-big retrieval using CHARACTER-based splitting.

    Child chunks (CHILD_CHUNK_SIZE chars) → embedded in Qdrant for precise retrieval.
    Parent text  (PARENT_CHUNK_SIZE chars) → stored inline on each child as
    parent_content metadata field.

    At retrieval time HybridRetriever reads chunk["parent_content"] directly
    from the Qdrant payload — zero extra DB round-trip.

    NOTE: For better retrieval quality, prefer SentenceHierarchicalChunker
    which uses sentence boundaries instead of character counts.
    """

    def __init__(
        self,
        child_size    : int = CHILD_CHUNK_SIZE,
        child_overlap : int = CHILD_CHUNK_OVERLAP,
        parent_size   : int = PARENT_CHUNK_SIZE,
        parent_overlap: int = PARENT_CHUNK_OVERLAP,
    ):
        super().__init__(child_size, child_overlap)
        self.strategy_name  = "hierarchical"
        self.child_size     = child_size
        self.child_overlap  = child_overlap
        self.parent_size    = parent_size
        self.parent_overlap = parent_overlap

        self._child_splitter = RecursiveCharacterTextSplitter(
            chunk_size    = child_size,
            chunk_overlap = child_overlap,
            separators    = ["\n\n", "\n", ". ", "! ", "? ", ", ", " ", ""],
        )
        self._parent_splitter = RecursiveCharacterTextSplitter(
            chunk_size    = parent_size,
            chunk_overlap = parent_overlap,
            separators    = ["\n\n", "\n", ". ", "! ", "? ", ", ", " ", ""],
        )

    def chunk(self, text: str) -> list[str]:
        """Flat child-only chunking (used by chunk_documents)."""
        return [c.strip() for c in self._child_splitter.split_text(text) if c.strip()]

    def chunk_hierarchical(self, blocks: list[dict]) -> list[dict]:
        """
        Full hierarchical chunking pipeline.

        Args:
            blocks : structured blocks from any loader (PDFLoader, etc.)

        Returns:
            children : list[dict]
                Each child dict has all metadata PLUS:
                  - parent_content : str  — the larger parent passage
                  - parent_id      : str  — stable hash ID (kept for reference)
        """
        children: list[dict] = []

        text_blocks   = [b for b in blocks if b.get("type") not in _ATOMIC_TYPES]
        atomic_blocks = [b for b in blocks if b.get("type")     in _ATOMIC_TYPES]

        # ── 1. Text / heading / bullet blocks ─────────────────────────────
        groups = self._group_by_section(text_blocks)

        for g_idx, group in enumerate(groups):
            combined  = "\n\n".join(b["content"] for b in group)
            meta_base = {
                "source"      : group[0]["source"],
                "page"        : group[0]["page"],
                "type"        : group[0].get("type", "text"),
                "heading"     : group[0].get("heading", ""),
                "section_path": group[0].get("section_path", ""),
                "bbox"        : group[0].get("bbox"),
                "page_width"  : group[0].get("page_width"),
                "page_height" : group[0].get("page_height"),
            }

            parent_texts = self._parent_splitter.split_text(combined)

            for p_idx, parent_text in enumerate(parent_texts):
                parent_id = self._make_parent_id(
                    meta_base["source"],
                    meta_base["page"],
                    meta_base["section_path"],
                    g_idx * 1000 + p_idx,
                )
                child_texts = self._child_splitter.split_text(parent_text)
                total_c     = len(child_texts)

                for c_idx, child_text in enumerate(child_texts):
                    if not child_text.strip():
                        continue
                    children.append({
                        **meta_base,
                        "content"       : child_text,
                        "parent_content": parent_text,
                        "parent_id"     : parent_id,
                        "chunk_index"   : c_idx,
                        "total_chunks"  : total_c,
                        "strategy"      : self.strategy_name,
                    })

        # ── 2. Atomic blocks — self-contained ──────────────────────────────
        for a_idx, block in enumerate(atomic_blocks):
            content = block.get("content", "").strip()
            if not content:
                continue

            parent_id = self._make_parent_id(
                block["source"],
                block["page"],
                block.get("section_path", ""),
                100_000 + a_idx,
            )

            children.append({
                **block,
                "parent_content": content,
                "parent_id"     : parent_id,
                "chunk_index"   : 0,
                "total_chunks"  : 1,
                "strategy"      : self.strategy_name,
            })

        print(
            f"  [CHUNKER] {len(children)} children "
            f"(parent_content embedded inline) from {len(blocks)} blocks"
        )
        return children

    # ── Shared helpers — also used by SentenceHierarchicalChunker ─────────

    @staticmethod
    def _make_parent_id(source: str, page: int, section: str, idx: int) -> str:
        """Stable deterministic hash ID for a parent passage."""
        raw = f"{source}|p{page}|{section}|{idx}"
        return "par_" + hashlib.md5(raw.encode()).hexdigest()[:12]

    @staticmethod
    def _group_by_section(blocks: list[dict]) -> list[list[dict]]:
        """
        Split a flat list of blocks into groups that will become separate
        parent passages.

        A new group is started when ANY of the following is true:
          1. The block is a heading   — explicit section start
          2. The section_path changes — heading breadcrumb changed
          3. The PAGE NUMBER changes  — ← BUG 1 FIX

        Condition 3 is the critical addition.  When PDF heading detection
        fails (no clear font-size jumps), conditions 1 and 2 never fire,
        causing the entire document to collapse into a single group. All
        children of that group then inherit group[0]["page"] = 1.

        By also breaking on page changes we guarantee that blocks from
        different pages always end up in different groups, so each child
        chunk carries the correct page number even when section detection
        is completely unavailable.
        """
        if not blocks:
            return []

        groups : list[list[dict]] = []
        current: list[dict]       = [blocks[0]]

        for block in blocks[1:]:
            new_section = (
                block.get("type") == "heading"
                or block.get("section_path") != current[-1].get("section_path")
                or block.get("page")         != current[-1].get("page")   # ← BUG 1 FIX
            )
            if new_section:
                groups.append(current)
                current = [block]
            else:
                current.append(block)

        if current:
            groups.append(current)
        return groups


# ─────────────────────────────────────────────────────────
# STRATEGY 4 — SENTENCE  (flat, no parent-child)
# ─────────────────────────────────────────────────────────

class SentenceChunker(BaseChunker):
    """
    Flat sentence-window chunker.

    Each chunk = `window` consecutive sentences joined into one string,
    with `overlap` sentences repeated from the previous chunk for continuity.

    Advantages over character-based flat chunking:
      - Never cuts mid-sentence → embeddings capture complete thoughts
      - Overlap is always a full sentence → no half-ideas at boundaries
      - Consistent semantic density regardless of sentence length variance

    Default parameters:
      window=4  → ~250 chars average, fits embedding model sweet spot
      overlap=1 → one shared sentence between adjacent chunks

    Use this for flat (non-hierarchical) pipelines. For best retrieval
    quality use SentenceHierarchicalChunker instead.
    """

    def __init__(self, window: int = 4, overlap: int = 1):
        # chunk_size / chunk_overlap unused — sentence count drives splitting
        super().__init__(chunk_size=0, chunk_overlap=0)
        self.strategy_name = "sentence"
        self.window        = window
        self.overlap       = overlap

    def chunk(self, text: str) -> list[str]:
        sentences = _split_sentences(text)
        return _sentences_to_chunks(sentences, self.window, self.overlap)


# ─────────────────────────────────────────────────────────
# STRATEGY 5 — SENTENCE HIERARCHICAL  ★ RECOMMENDED ★
# ─────────────────────────────────────────────────────────

class SentenceHierarchicalChunker(HierarchicalChunker):
    """
    Parent-child hierarchical chunking driven entirely by SENTENCE BOUNDARIES.

    WHY THIS IS BETTER THAN CHARACTER-BASED HIERARCHICAL:
    ───────────────────────────────────────────────────────
      ✓ Child chunks never cut mid-sentence
        → embeddings represent complete, coherent facts
      ✓ Parent passages respect paragraph structure
        → LLM receives full context without artificial truncation
      ✓ Overlap is always a complete sentence
        → no half-ideas lost at chunk boundaries
      ✓ Works well for technical manuals, dense PDFs, ship manuals
        → one sentence = one fact = one retrievable unit

    SIZES (optimised for technical documentation):
    ─────────────────────────────────────────────
      Child : 4 sentences,  overlap 1  →  ~200-400 chars
              Small enough for precise Qdrant vector retrieval.

      Parent: 12 sentences, overlap 2  →  ~800-1200 chars
              Large enough to give the LLM full paragraph context.
              Well under typical 512-token parent passage limit.

    PIPELINE:
    ──────────
      1. _group_by_section()  group blocks by page/heading  [inherited]
      2. combined text → _split_sentences()  (nltk/spacy/regex)
      3. parent windows  : 12 sentences, overlap 2
      4. child windows   : 4 sentences,  overlap 1  (per parent)
      5. Each child stores parent_content inline               [inherited]

    PARENT-CHILD CONTAINMENT GUARANTEE:
    ─────────────────────────────────────
      Children are split from their parent's sentence list (step 4),
      NOT from the full document. This means every child is semantically
      contained within its parent — the relationship is always coherent.
      This is the core requirement for correct small-to-big retrieval.
    """

    def __init__(
        self,
        child_window   : int = 4,    # sentences per child chunk
        child_overlap  : int = 1,    # sentence overlap between children
        parent_window  : int = 12,   # sentences per parent passage
        parent_overlap : int = 2,    # sentence overlap between parents
    ):
        # Bypass HierarchicalChunker.__init__ — we don't use its char splitters
        BaseChunker.__init__(self, chunk_size=0, chunk_overlap=0)
        self.strategy_name  = "sentence_hierarchical"
        self.child_window   = child_window
        self.child_overlap  = child_overlap
        self.parent_window  = parent_window
        self.parent_overlap = parent_overlap

    def chunk(self, text: str) -> list[str]:
        """Flat child-only chunking (used by chunk_documents)."""
        sentences = _split_sentences(text)
        return _sentences_to_chunks(sentences, self.child_window, self.child_overlap)

    def chunk_hierarchical(self, blocks: list[dict]) -> list[dict]:
        """
        Full sentence-based hierarchical pipeline.

        Return signature matches HierarchicalChunker.chunk_hierarchical():
            list[dict]  — children with parent_content embedded inline.

        All metadata fields (source, page, heading, section_path, bbox,
        page_width, page_height) are preserved from the source blocks.
        """
        children      : list[dict] = []
        text_blocks   = [b for b in blocks if b.get("type") not in _ATOMIC_TYPES]
        atomic_blocks = [b for b in blocks if b.get("type")     in _ATOMIC_TYPES]

        # ── Step 1: group blocks by section/page (inherited, unchanged) ───
        groups = self._group_by_section(text_blocks)

        for g_idx, group in enumerate(groups):
            combined  = "\n\n".join(b["content"] for b in group)
            meta_base = {
                "source"      : group[0]["source"],
                "page"        : group[0]["page"],
                "type"        : group[0].get("type", "text"),
                "heading"     : group[0].get("heading", ""),
                "section_path": group[0].get("section_path", ""),
                # bbox from first block in group (best approximation for group)
                "bbox"        : group[0].get("bbox"),
                "page_width"  : group[0].get("page_width"),
                "page_height" : group[0].get("page_height"),
            }

            # ── Step 2: split combined text into sentences ────────────────
            all_sentences = _split_sentences(combined)
            if not all_sentences:
                continue

            # ── Step 3: build parent-sized windows ───────────────────────
            parent_texts = _sentences_to_chunks(
                all_sentences, self.parent_window, self.parent_overlap
            )

            for p_idx, parent_text in enumerate(parent_texts):
                parent_id = self._make_parent_id(   # inherited from HierarchicalChunker
                    meta_base["source"],
                    meta_base["page"],
                    meta_base["section_path"],
                    g_idx * 1000 + p_idx,
                )

                # ── Step 4: build child windows from THIS parent ──────────
                # Children are split from parent sentences — not the full doc.
                # This guarantees every child is contained within its parent.
                parent_sents = _split_sentences(parent_text)
                child_texts  = _sentences_to_chunks(
                    parent_sents, self.child_window, self.child_overlap
                )
                total_c = len(child_texts)

                for c_idx, child_text in enumerate(child_texts):
                    if not child_text.strip():
                        continue
                    children.append({
                        **meta_base,
                        "content"       : child_text,
                        "parent_content": parent_text,   # ← inline parent
                        "parent_id"     : parent_id,
                        "chunk_index"   : c_idx,
                        "total_chunks"  : total_c,
                        "strategy"      : self.strategy_name,
                    })

        # ── Atomic blocks (tables, images) — pass through unchanged ───────
        for a_idx, block in enumerate(atomic_blocks):
            content = block.get("content", "").strip()
            if not content:
                continue
            parent_id = self._make_parent_id(
                block["source"],
                block["page"],
                block.get("section_path", ""),
                100_000 + a_idx,
            )
            children.append({
                **block,
                "parent_content": content,   # atomic: parent == child
                "parent_id"     : parent_id,
                "chunk_index"   : 0,
                "total_chunks"  : 1,
                "strategy"      : self.strategy_name,
            })

        print(
            f"  [CHUNKER] {len(children)} sentence-based children "
            f"(parent_content embedded inline) from {len(blocks)} blocks"
        )
        return children


# ─────────────────────────────────────────────────────────
# CHUNKER FACTORY
# ─────────────────────────────────────────────────────────

class ChunkerFactory:
    """
    Central registry for all chunking strategies.

    Strategies (in recommended order):
      "sentence_hierarchical" — ★ RECOMMENDED — sentence-based parent-child
      "hierarchical"          — character-based parent-child (legacy)
      "sentence"              — flat sentence-window chunking
      "recursive"             — flat recursive character chunking
      "fixed"                 — flat fixed-size character chunking

    Usage:
        chunker = ChunkerFactory.get("sentence_hierarchical")
        children = chunker.chunk_hierarchical(blocks)   # for PDFs
        chunks   = chunker.chunk_documents(docs)        # for flat docs
    """

    STRATEGIES: dict[str, type[BaseChunker]] = {
        "sentence_hierarchical": SentenceHierarchicalChunker,  # ★ default
        "hierarchical"         : HierarchicalChunker,
        "sentence"             : SentenceChunker,
        "recursive"            : RecursiveChunker,
        "fixed"                : FixedSizeChunker,
    }

    @staticmethod
    def get(strategy: str = "sentence_hierarchical", **kwargs) -> BaseChunker:
        """
        Instantiate a chunker by strategy name.

        Args:
            strategy : one of STRATEGIES keys (default: "sentence_hierarchical")
            **kwargs : forwarded to the chunker's __init__

        Raises:
            ValueError : if strategy name is not recognised
        """
        strategy = strategy.lower()
        if strategy not in ChunkerFactory.STRATEGIES:
            raise ValueError(
                f"Unknown strategy '{strategy}'. "
                f"Choose from: {list(ChunkerFactory.STRATEGIES.keys())}"
            )
        return ChunkerFactory.STRATEGIES[strategy](**kwargs)

    @staticmethod
    def available_strategies() -> list[str]:
        """Return list of all registered strategy names."""
        return list(ChunkerFactory.STRATEGIES.keys())


# ─────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────

__all__ = [
    # Base
    "BaseChunker",
    # Character-based strategies
    "FixedSizeChunker",
    "RecursiveChunker",
    "HierarchicalChunker",
    # Sentence-based strategies (recommended)
    "SentenceChunker",
    "SentenceHierarchicalChunker",
    # Utilities
    "ChunkerFactory",
]