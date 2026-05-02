# SearchX

A self-hosted search engine built with Python and Flask. Index your local files and websites, search them instantly, and get AI-generated answers — all running on your own machine.

## Features

- **Full-text search** across local files and crawled websites
- **Semantic / vector search** — reranks results using sentence embeddings (`all-MiniLM-L6-v2`)
- **AI answer box** — streaming answers powered by Ollama, OpenAI, Anthropic, or Gemini
- **Text-to-speech** — read answers aloud via Edge TTS (free) or ElevenLabs
- **Web crawler** — async crawler with configurable depth, page limits, and per-domain rate limiting
- **Cloudflare bypass** — Playwright headless Chromium fallback for 403-protected sites
- **Single URL indexing** — add one page without crawling
- **File watcher** — auto-reindex local folders when files change
- **Duplicate detection** — SimHash-based near-duplicate finder
- **Collections** — group and save search results
- **Search analytics** — dashboard with query trends, top searches, zero-result tracking
- **Related searches** — trigram similarity suggestions
- **Browser extension** — add any page to the index with one click
- **User accounts** — registration, login, search history, per-user settings
- **File viewer** — view PDFs, images, video, audio, and documents in-browser
- **Admin panel** — manage users, sources, embeddings, and crawler jobs

## Supported File Types

| Category | Formats |
|---|---|
| Documents | PDF, DOCX, DOC, XLSX, XLS, PPTX, PPT, TXT, CSV |
| Images | JPG, PNG, GIF, WEBP, BMP, TIFF, SVG (with OCR via Tesseract) |
| Audio | MP3, WAV, FLAC, OGG, AAC, M4A (reads metadata) |
| Video | MP4, MKV, AVI, MOV, WEBM, M4V (reads metadata) |
| Web | HTML pages crawled or added via URL |

## Requirements

- Python 3.11+
- PostgreSQL 13+
- (Optional) pgvector extension — for HNSW vector index
- (Optional) Tesseract OCR — for image text extraction
- (Optional) Ollama — for local AI answers

## Setup

**1. Clone and create a virtual environment**

```bash
git clone https://github.com/YOUR_USERNAME/searchx.git
cd searchx
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**2. Install Playwright (for Cloudflare bypass)**

```bash
pip install playwright
python -m playwright install chromium
```

**3. Create the database**

```bash
createdb searchx
python setup_db.py
```

**4. Configure**

Copy the default config and edit it:

```bash
cp config.py config.json  # or let the app create it on first run
```

Key settings in `config.json`:

```json
{
  "ai_provider": "ollama",
  "ollama_url": "http://localhost:11434",
  "ollama_model": "qwen2.5:0.5b",
  "ai_answer_mode": "results",
  "tts_provider": "edge"
}
```

**5. Run**

```bash
python app.py
```

Open [http://localhost:5000](http://localhost:5000) and register the first account (automatically becomes admin).

## AI Providers

| Provider | Key required | Notes |
|---|---|---|
| Ollama | No | Free, runs locally |
| OpenAI | Yes | `openai_api_key` in config |
| Anthropic | Yes | `anthropic_api_key` in config |
| Gemini | Yes | `gemini_api_key` in config |

Set `ai_answer_mode` to `"results"` to summarize indexed documents, or `"free"` to let the AI answer from its own knowledge.

## Browser Extension

Load the `extension/` folder as an unpacked extension in Chrome/Edge:

1. Go to `chrome://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked** → select the `extension/` folder
4. Click the SearchX icon on any page to add it to your index

## Production Deployment

The repo includes a `gunicorn.conf.py` and `searchx.service` (systemd) for production:

```bash
gunicorn -c gunicorn.conf.py wsgi:app
```

For reverse proxy, see `nginx.conf`.

## Tech Stack

- **Backend** — Flask, psycopg2, aiohttp, BeautifulSoup, Playwright
- **Search** — PostgreSQL FTS (`tsvector`), pgvector (HNSW), `pg_trgm`
- **Embeddings** — sentence-transformers (`all-MiniLM-L6-v2`)
- **AI** — Ollama / OpenAI / Anthropic / Gemini (streaming SSE)
- **TTS** — edge-tts (Microsoft neural voices), ElevenLabs
- **File parsing** — pdfplumber, python-docx, mutagen, Pillow, pytesseract, trafilatura
- **Frontend** — Vanilla JS, Chart.js (analytics), no framework
