# RenderScope: SEO Page Auditor

Most SEO auditors read your HTML. RenderScope reads your page the way crawlers, Google's renderer and real
visitors see it, then hands you paste-ready fixes.

## How it works

| Step | Tool | What it does |
|---|---|---|
| Raw HTML | `httpx` | Plain GET with no JavaScript: what a basic crawler or AI bot sees |
| Rendered page | **TinyFish Fetch** (free) | Real-browser render + head metadata, links and images |
| Render gap | Python | Compares the two: content, H1, title or links that exist only after JS |
| Live SERP | **TinyFish Search** (free) | Is the page in the top results for its keyword? Who outranks it? |
| Visitor check | **TinyFish Agent** (metered) | Opens the page like a user: popups covering content, lazy-load failures, broken sections |
| AI readiness | Python | robots.txt blocking GPTBot/ClaudeBot/etc., `/llms.txt` |
| Fixes | **Claude** (`claude-opus-5-5`) | Prioritized fixes with code, rewritten title/meta, JSON-LD |

Every step runs in parallel where possible, and a failing step is reported without breaking the audit.

## Run it

```powershell
cd seo-auditor
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env      # then paste your TINYFISH_API_KEY and ANTHROPIC_API_KEY
.venv\Scripts\python -m uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000.

## API

`POST /api/audit`

```json
{ "url": "https://example.com", "keyword": "optional target keyword", "deep_ux": true, "use_cache": false }
```

Every result is saved in `cache/`. Tick **Use saved result** (`use_cache: true`) during the demo to replay it
instantly if the network is slow.

## Cost

Fetch and Search are free. The Agent check costs about $0.016 per step, capped at 15 steps
(about $0.24 at most per audit). Untick the real-browser UX check to skip it.
