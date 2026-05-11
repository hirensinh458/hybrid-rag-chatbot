# embeddings/embedder.py
#
# CHANGES vs original:
#   - Default model changed to BAAI/bge-small-en-v1.5 (384-dim, ~130MB)
#     Same BGE quality characteristics as bge-base, half the size.
#   - OllamaEmbedder REMOVED — one less moving part for offline use.
#   - EmbedderFactory updated accordingly.

import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sentence_transformers import SentenceTransformer
from config import EMBEDDING_MODEL, HF_TOKEN

# ── HuggingFace Hub login ─────────────────────────────────
if HF_TOKEN:
    try:
        from huggingface_hub import login
        login(token=HF_TOKEN)
        os.environ["HUGGINGFACE_HUB_TOKEN"] = HF_TOKEN
        print("  [EMBEDDER] HuggingFace Hub login successful.")
    except Exception as e:
        print(f"  [EMBEDDER] HuggingFace Hub login failed: {e}")

# BGE asymmetric retrieval prefix — used for QUERIES only, not documents
_BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


# ─────────────────────────────────────────────────────────
# BASE EMBEDDER
# ─────────────────────────────────────────────────────────

class BaseEmbedder:
    """
    Abstract base class for all embedding strategies.
    Every embedder must implement embed_text and embed_documents.

    NOTE ON BGE ASYMMETRIC RETRIEVAL:
    BGE models are trained with different prompts for queries vs documents.
    Subclasses that use BGE should apply _BGE_QUERY_PREFIX to embed_text()
    but NOT to embed_documents(). This gives ~10% better MTEB retrieval scores.
    """

    def __init__(self):
        self.model_name    = "base"
        self.embedding_dim = None

    def embed_text(self, text: str) -> list[float]:
        """Embed a single query string → vector."""
        raise NotImplementedError("Subclasses must implement embed_text()")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of document strings → list of vectors."""
        raise NotImplementedError("Subclasses must implement embed_documents()")

    def get_info(self) -> dict:
        return {
            "model"        : self.model_name,
            "embedding_dim": self.embedding_dim,
        }


# ─────────────────────────────────────────────────────────
# HUGGINGFACE EMBEDDER (only embedder — Ollama removed)
# Default: BAAI/bge-small-en-v1.5
# ─────────────────────────────────────────────────────────

class HuggingFaceEmbedder(BaseEmbedder):
    """
    Uses sentence-transformers locally.
    No API key needed. Runs fully offline after first download.

    Default model: BAAI/bge-small-en-v1.5
      ✅ ~130MB (half of bge-base)  ✅ 384 dims
      ✅ Same BGE quality           ✅ Asymmetric retrieval support

    BGE ASYMMETRIC RETRIEVAL:
      embed_text()      → applies query prefix (used for search queries)
      embed_documents() → no prefix          (used for indexing chunks)
    """

    QUERY_PREFIX = _BGE_QUERY_PREFIX

    def __init__(self, model_name: str = EMBEDDING_MODEL):
        super().__init__()
        self.model_name = model_name

        # Disable BGE prefix if model isn't a BGE variant
        if "bge" not in model_name.lower():
            self.QUERY_PREFIX = ""

        print(f"  [EMBEDDER] Loading HuggingFace model: {model_name}")
        self._model        = SentenceTransformer(model_name)
        self.embedding_dim = self._model.get_sentence_embedding_dimension()
        print(f"  [EMBEDDER] Ready! Dimensions: {self.embedding_dim}")

    def embed_text(self, text: str) -> list[float]:
        """
        Embed a single search query.
        BGE models get a special query prefix for asymmetric retrieval.
        """
        query = self.QUERY_PREFIX + text.strip() if self.QUERY_PREFIX else text.strip()
        return self._model.encode(
            query,
            convert_to_numpy     = True,
            normalize_embeddings = True,
        ).tolist()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """
        Embed document chunks — no query prefix.
        BGE asymmetric: documents are embedded as-is.
        """
        if not texts:
            return []
        print(f"  [EMBEDDER] Embedding {len(texts)} chunks...")
        vectors = self._model.encode(
            texts,
            batch_size           = 64,
            show_progress_bar    = len(texts) > 50,
            convert_to_numpy     = True,
            normalize_embeddings = True,
        )
        return vectors.tolist()


# ─────────────────────────────────────────────────────────
# EMBEDDER FACTORY
# ─────────────────────────────────────────────────────────

class EmbedderFactory:
    """
    Returns the embedder. Only HuggingFace is supported now —
    Ollama removed to reduce moving parts for offline deployment.

    Usage:
        embedder = EmbedderFactory.get("huggingface")
        embedder = EmbedderFactory.get()   # same — huggingface is the only option
    """

    PROVIDERS: dict[str, type[BaseEmbedder]] = {
        "huggingface": HuggingFaceEmbedder,
    }

    @staticmethod
    def get(provider: str = "huggingface", **kwargs) -> BaseEmbedder:
        provider = provider.lower().strip()
        if provider not in EmbedderFactory.PROVIDERS:
            raise ValueError(
                f"Unknown provider '{provider}'. "
                f"Available: {list(EmbedderFactory.PROVIDERS.keys())}"
            )
        return EmbedderFactory.PROVIDERS[provider](**kwargs)

    @staticmethod
    def available_providers() -> list[str]:
        return list(EmbedderFactory.PROVIDERS.keys())


__all__ = [
    "BaseEmbedder",
    "HuggingFaceEmbedder",
    "EmbedderFactory",
]