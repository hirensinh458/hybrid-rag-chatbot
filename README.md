# 🚢 MarineDoc — Hybrid RAG Chatbot

> **AI-powered ship manual assistant.** Ask questions about your vessel's technical documentation and get precise, grounded answers — online, on the ship's intranet, or fully offline at sea.

[![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi)](https://fastapi.tiangolo.com)
[![Qdrant](https://img.shields.io/badge/Vector_DB-Qdrant-red)](https://qdrant.tech)
[![Groq](https://img.shields.io/badge/LLM-Groq_Llama_3.1-orange)](https://groq.com)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker)](https://docker.com)

---

## 📌 What Is This?

MarineDoc is a **Retrieval-Augmented Generation (RAG) system** built for maritime use. Ship engineers and crew can query PDF technical manuals (engine manuals, safety guides, maintenance documents) and receive answers directly cited from the source, with clickable references that open the exact page.

The system is designed around a core real-world constraint: **ships are not always connected to the internet.** It operates in three distinct modes:

| Mode | Condition | Behavior |
|------|-----------|----------|
| **Mode 1 — Full Online** | Server has internet | AI-generated answers via Groq (Llama 3.1), streamed in real time |
| **Mode 2 — At Sea (Intranet)** | Server reachable, no internet | Server-side hybrid retrieval returns the most relevant manual sections |
| **Mode 3 — Deep Offline** | No server at all | Mobile app searches its own local SQLite + vector DB on-device |

> Mode 3 is handled entirely by the **[rag-mobile](https://github.com/Amar5623/rag-mobile)** companion app. This repo covers Modes 1 and 2.

---

## 🗂️ Repository Structure

```
hybrid-rag-chatbot/
├── rag-backend/          ← Core FastAPI server (Modes 1 & 2)
├── rag-frontend/         ← React web UI for testing from PC/laptop
└── rag-admin/            ← Admin panel UI for PDF ingestion & KB management
```

---

## 🏗️ Architecture

```
                          ┌─────────────────────────────────┐
  User / Mobile App       │          FastAPI Backend         │
  ──────────────          │                                  │
  POST /chat/stream  ───► │  RAGChain                        │
  POST /chat/offline ───► │   ├── HybridRetriever            │
  POST /ingest       ───► │   │    ├── Qdrant (dense)        │
  GET  /kb/export    ───► │   │    └── BM25 (sparse)         │
                          │   │         └── RRF Fusion       │
                          │   ├── Cross-Encoder Reranker     │
                          │   └── Parent Expansion           │
                          │                                  │
                          │  LLM Layer                       │
                          │   ├── Groq API (online)          │
                          │   └── Ollama (local fallback)    │
                          │                                  │
                          │  Storage                         │
                          │   ├── Qdrant (local + cloud)     │
                          │   ├── BM25 index (pickle)        │
                          │   └── Supabase (PDF storage)     │
                          └─────────────────────────────────┘
                                         │
                          ┌──────────────┴──────────────┐
                     rag-frontend               rag-admin
                   (React web UI)         (ingestion panel)
```

### Retrieval Pipeline (per query)

```
Query
  │
  ├──► Qdrant semantic search  (top-20 child chunks, dense vector)
  └──► BM25 keyword search     (top-20 child chunks, sparse)
              │
              ▼
        RRF Fusion             (Reciprocal Rank Fusion, k=60)
              │
              ▼
        Cross-Encoder Rerank   (TinyBERT, children → top-5)
              │
              ▼
        Parent Expansion       (swap child 512-char → parent 1500-char)
              │
              ▼
        LLM (Groq / Ollama)    (Mode 1 only — streams answer via SSE)
```

---

## 🧩 Components

### `rag-backend` — FastAPI Server

The brain of the system. Handles ingestion, retrieval, generation, and sync.

**Key modules:**

| Module | Purpose |
|--------|---------|
| `ingestion/pdf_loader.py` | PDF → text + tables (markdown) + images (OCR via Tesseract) |
| `ingestion/chunker.py` | Hierarchical chunking: child chunks (512 chars) linked to parent chunks (1500 chars) |
| `embeddings/embedder.py` | `BAAI/bge-small-en-v1.5` local embeddings (384-dim, asymmetric retrieval) |
| `retrieval/hybrid_retriever.py` | Dense + sparse retrieval fused with RRF |
| `retrieval/bm25_store.py` | BM25 index with domain-aware tokenizer (keeps °C, /, -, %) |
| `retrieval/reranker.py` | `cross-encoder/ms-marco-TinyBERT-L-2-v2` cross-encoder |
| `vectorstore/` | Pluggable vector store: Qdrant ✦ LanceDB ✦ ChromaDB |
| `generation/groq_llm.py` | Groq streaming (Llama 3.1 8B Instant) with conversation history |
| `generation/ollama_llm.py` | Local Ollama fallback |
| `services/sync_service.py` | Cloud → Local Qdrant sync engine |
| `services/network_monitor.py` | Background 8.8.8.8 probe (15s interval) |
| `services/supabase_storage.py` | PDF upload to Supabase public bucket |
| `routers/chat.py` | `/chat/stream` (SSE) and `/chat/offline` (JSON) |
| `routers/ingest.py` | PDF upload, duplicate detection, deletion |
| `routers/kb.py` | `/health`, `/kb/export`, `/kb/diff`, `/stats`, `/documents` |
| `routers/admin.py` | Bearer-token protected admin routes |

### `rag-frontend` — Web Test Client

A lightweight React web app for testing the chatbot from a PC or laptop. Supports Mode 1 (streaming AI answers) and Mode 2 (offline chunk retrieval). Useful during development and for demos — ship workers use the mobile app instead.

### `rag-admin` — Admin Panel

A React admin dashboard for managing the knowledge base without touching the API directly. Supports:
- Uploading PDFs to the knowledge base
- Viewing indexed documents and vector counts
- Deleting documents
- Triggering sync

---

## ⚙️ Tech Stack

| Layer | Technology |
|-------|-----------|
| API framework | [FastAPI](https://fastapi.tiangolo.com) + Uvicorn |
| Embeddings | [BAAI/bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5) via `sentence-transformers` |
| Primary vector DB | [Qdrant](https://qdrant.tech) (local embedded or cloud) |
| Alt vector DBs | [LanceDB](https://lancedb.github.io/lancedb/), [ChromaDB](https://www.trychroma.com) |
| Sparse retrieval | [rank-bm25](https://github.com/dorianbrown/rank_bm25) |
| Reranker | [cross-encoder/ms-marco-TinyBERT-L-2-v2](https://huggingface.co/cross-encoder/ms-marco-TinyBERT-L-2-v2) |
| LLM (online) | [Groq API](https://groq.com) — `llama-3.1-8b-instant` |
| LLM (offline) | [Ollama](https://ollama.ai) — `llama3.2` |
| PDF parsing | [pdfplumber](https://github.com/jsvine/pdfplumber) |
| Image OCR | [Tesseract](https://github.com/tesseract-ocr/tesseract) via `pytesseract` |
| PDF cloud storage | [Supabase Storage](https://supabase.com/storage) |
| Configuration | [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) |
| Containerisation | Docker (multi-stage build) |

---

## 🚀 Setup Guide

### Prerequisites

- Python 3.12+
- [Tesseract OCR](https://tesseract-ocr.github.io/tessdoc/Installation.html) installed on the system
- A [Groq API key](https://console.groq.com) (free tier available)
- (Optional) A [Qdrant Cloud](https://cloud.qdrant.io) account for cloud vector storage
- (Optional) A [Supabase](https://supabase.com) project for PDF storage + cross-device sync

---

### Option A — Run Locally

#### 1. Clone the repo

```bash
git clone https://github.com/Amar5623/hybrid-rag-chatbot.git
cd hybrid-rag-chatbot/rag-backend
```

#### 2. Create a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate        # Linux / macOS
# .venv\Scripts\activate         # Windows
```

#### 3. Install dependencies

```bash
pip install -r requirements.txt
```

#### 4. Configure environment

```bash
cp .env.example .env
```

Open `.env` and fill in the required values:

```env
# ── LLM ──────────────────────────────────────────────────────
GROQ_API_KEY=gsk_...          # Required for Mode 1 (AI answers)
GROQ_MODEL=llama-3.1-8b-instant
LLM_PROVIDER=groq             # or "ollama" for local

# ── Embeddings ────────────────────────────────────────────────
EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
EMBEDDING_DIM=384

# ── Vector Store ──────────────────────────────────────────────
VECTOR_STORE_VENDOR=qdrant    # "qdrant" | "lancedb" | "chroma"

# ── Qdrant Cloud (optional — leave empty for local-only) ──────
QDRANT_CLOUD_URL=https://xxx.qdrant.io
QDRANT_CLOUD_API_KEY=your_key

# ── Supabase (optional — needed for mobile PDF sync) ──────────
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_SERVICE_KEY=your_service_role_key
SUPABASE_BUCKET=pdfs

# ── Admin Panel Auth ──────────────────────────────────────────
# Generate: python -c "import secrets; print(secrets.token_hex(32))"
ADMIN_TOKEN=your_secure_token
```

#### 5. Start the server

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

The API is now available at `http://localhost:8000`. Visit `http://localhost:8000/docs` for the interactive Swagger UI.

---

### Option B — Run with Docker

```bash
cd hybrid-rag-chatbot/rag-backend

# Build the image
docker build -t marinedoc-backend .

# Run with your env file
docker run -p 8000:8000 \
  --env-file .env \
  -v $(pwd)/data:/app/data \
  marinedoc-backend
```

The `data/` volume mount persists:
- `data/qdrant/` — local vector database
- `data/pdfs/` — uploaded PDF files
- `data/bm25.pkl` — BM25 sparse index
- `data/hf_cache/` — HuggingFace model cache
- `data/logs/rag.log` — rotating log file

---

### Running the Admin Panel

```bash
cd hybrid-rag-chatbot/rag-admin
npm install
npm start         # or: npm run dev
```

Make sure the backend is running first. Point the admin panel to `http://localhost:8000`.

---

### Running the Web Frontend (optional)

```bash
cd hybrid-rag-chatbot/rag-frontend
npm install
npm start
```

The web UI lets you chat with the backend from a browser — useful for testing Mode 1 and Mode 2 without the mobile app.

---

## 📡 API Reference

### Chat

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/chat/stream` | Mode 1 — SSE token stream with Groq LLM |
| `POST` | `/chat/offline` | Mode 2 — returns top chunks as JSON (no LLM) |
| `POST` | `/chat/clear` | Clear conversation history for a session |

**`POST /chat/stream`** body:
```json
{
  "question": "What is the oil pressure alarm threshold?",
  "session_id": "default",
  "pinned_file": "engine_manual.pdf"   // optional: restrict to one document
}
```

**SSE events received:**
```
data: {"token": "The "}
data: {"token": "alarm "}
...
data: {"done": true, "citations": [...], "usage": {...}}
data: [DONE]
```

### Knowledge Base

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/ingest` | Upload one or more PDF files |
| `DELETE` | `/ingest/{filename}` | Remove a file from the knowledge base |
| `GET` | `/health` | Server health + `is_online` status |
| `GET` | `/stats` | Vector count, BM25 count, indexed files |
| `GET` | `/documents` | List all indexed files |
| `DELETE` | `/collection` | Wipe the entire knowledge base |
| `GET` | `/kb/export` | Paginated chunk + embedding export (for mobile sync) |
| `POST` | `/kb/diff` | Delta sync: returns which sources changed |

### Admin (requires `Authorization: Bearer <ADMIN_TOKEN>`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/admin/ingest` | Upload PDFs (admin-protected) |
| `DELETE` | `/admin/collection` | Wipe KB (admin-protected) |

---

## 🔧 Configuration Reference

All settings live in `.env`. Every value has a sensible default — only `GROQ_API_KEY` is strictly required for Mode 1.

| Variable | Default | Description |
|----------|---------|-------------|
| `GROQ_API_KEY` | — | [Groq](https://console.groq.com) API key |
| `GROQ_MODEL` | `llama-3.1-8b-instant` | Groq model ID |
| `LLM_PROVIDER` | `groq` | `groq` or `ollama` |
| `OLLAMA_MODEL` | `llama3.2` | Ollama model name |
| `EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | HuggingFace embedding model |
| `VECTOR_STORE_VENDOR` | `qdrant` | `qdrant`, `lancedb`, or `chroma` |
| `CHUNKER` | `hierarchical` | `hierarchical` or `flat` |
| `CHILD_CHUNK_SIZE` | `512` | Size of retrieval chunks (chars) |
| `PARENT_CHUNK_SIZE` | `1500` | Size of context chunks fed to LLM (chars) |
| `TOP_K` | `20` | Candidates retrieved before reranking |
| `RERANKER_TOP_K` | `5` | Chunks kept after cross-encoder rerank |
| `ENABLE_OFFLINE_RERANKER` | `true` | Run reranker in Mode 2 as well |
| `NETWORK_POLL_INTERVAL` | `30` | Seconds between connectivity checks |
| `ADMIN_TOKEN` | — | Bearer token for `/admin/*` routes |
| `QDRANT_CLOUD_URL` | — | Qdrant Cloud URL (leave empty for local) |
| `SUPABASE_URL` | — | Supabase project URL (leave empty to skip) |

---

## 📱 Mobile App

Ship workers use the **MarineDoc mobile app** ([rag-mobile](https://github.com/Amar5623/rag-mobile)) on their phones. It connects to this backend and also runs a full on-device RAG pipeline for deep offline use.

The backend exposes `/kb/export` and `/kb/diff` specifically for the mobile sync engine — the app downloads the entire knowledge base (chunks + pre-computed embeddings) to its own SQLite database, so it can search without any server connection.

---

## 🔗 Related Links

- **Mobile App Repo:** [github.com/Amar5623/rag-mobile](https://github.com/Amar5623/rag-mobile)
- **Qdrant Docs:** [qdrant.tech/documentation](https://qdrant.tech/documentation)
- **Groq Console:** [console.groq.com](https://console.groq.com)
- **Supabase Storage Docs:** [supabase.com/docs/guides/storage](https://supabase.com/docs/guides/storage)
- **BAAI/bge-small-en-v1.5:** [huggingface.co/BAAI/bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5)
- **TinyBERT Reranker:** [huggingface.co/cross-encoder/ms-marco-TinyBERT-L-2-v2](https://huggingface.co/cross-encoder/ms-marco-TinyBERT-L-2-v2)
- **FastAPI Docs:** [fastapi.tiangolo.com](https://fastapi.tiangolo.com)
- **pdfplumber:** [github.com/jsvine/pdfplumber](https://github.com/jsvine/pdfplumber)

---

## 📂 Data Flow Summary

```
PDF Upload (POST /ingest)
    │
    ▼
PDF Parser       → text blocks + tables (markdown) + images (OCR)
    │
    ▼
Hierarchical Chunker
    ├── child chunks  (512 chars)  → stored in Qdrant + BM25
    └── parent chunks (1500 chars) → stored in child chunk metadata
    │
    ▼
BGE Embedder     → 384-dim vectors (BGE asymmetric: query prefix on queries only)
    │
    ▼
Qdrant / BM25    → persisted to disk (and optionally Qdrant Cloud)
    │
    ▼
Supabase Storage → PDF uploaded to public bucket (source_url in chunk metadata)
```

```
User Query (POST /chat/stream)
    │
    ├── Dense search  (Qdrant, top-20)
    └── Sparse search (BM25, top-20)
              │
              ▼
        RRF Fusion (k=60)
              │
              ▼
        TinyBERT Reranker  → top-5 children
              │
              ▼
        Parent Expansion   → 1500-char context passages
              │
              ▼
        Groq (Llama 3.1)   → streamed answer + citations
```

---

## 📋 Logging

The backend writes structured logs to both stderr (coloured in TTY) and a rotating file at `data/logs/rag.log` (10 MB per file, 5 backups).

Every HTTP request gets a unique 12-character request ID that appears on every log line for that request, making it easy to trace a single query end-to-end.

Set `LOG_LEVEL=DEBUG` in `.env` to see retrieval scores, token counts, and embedding details.

---

*Built and Designed for maritime deployment where connectivity is unreliable and documentation accuracy is critical.*
