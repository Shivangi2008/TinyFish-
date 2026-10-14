"""SEO Page Auditor: raw HTML vs TinyFish-rendered page, live SERP check, browser UX check, Gemini fixes."""
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

load_dotenv()

from . import checks, fixer, tinyfish  # noqa: E402  (after load_dotenv so keys are set)

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache"
CACHE_DIR.mkdir(exist_ok=True)

app = FastAPI(title="SEO Page Auditor")


class AuditRequest(BaseModel):
    url: str
    keyword: str | None = None   # query for the SERP check; defaults to the page title
    deep_ux: bool = True         # run the TinyFish browser agent (metered)
    use_cache: bool = False      # return the last saved result for this input (demo safety net)


def _normalize(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if not urlparse(url).netloc:
        raise HTTPException(400, "Invalid URL")
    return url


def _cache_path(req: AuditRequest, url: str) -> Path:
    key = hashlib.sha1(f"{url}|{req.keyword or ''}|{req.deep_ux}".encode()).hexdigest()[:16]
    return CACHE_DIR / f"{key}.json"


async def _safe(coro):
    """Run a step; return (result, error_message) so one failing source doesn't sink the audit."""
    try:
        return await coro, None
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
async def health():
    return {
        "tinyfish_key": bool(os.environ.get("TINYFISH_API_KEY")),
        "gemini_key": bool(os.environ.get("GEMINI_API_KEY")),
    }


@app.post("/api/audit")
async def audit(req: AuditRequest):
    url = _normalize(req.url)
    cache_file = _cache_path(req, url)
    if req.use_cache and cache_file.exists():
        return {**json.loads(cache_file.read_text(encoding="utf-8")), "cached": True}

    started = time.monotonic()
    timings: dict[str, float] = {}
    errors: dict[str, str] = {}
    origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"

    async def timed(name, coro):
        t = time.monotonic()
        result, err = await _safe(coro)
        timings[name] = round(time.monotonic() - t, 1)
        if err:
            errors[name] = err
        return result

    async with httpx.AsyncClient(follow_redirects=True) as http:
        # The browser agent is the slowest step, so start it first and let it run alongside everything else.
        ux_task = asyncio.create_task(timed("tinyfish_agent", tinyfish.ux_audit(http, url))) if req.deep_ux else None

        raw, tf_page, robots_txt, llms_txt = await asyncio.gather(
            timed("raw_html", checks.fetch_raw(http, url)),
            timed("tinyfish_fetch", tinyfish.fetch_rendered(http, url)),
            timed("robots_txt", checks.fetch_text(http, origin + "/robots.txt")),
            timed("llms_txt", checks.fetch_text(http, origin + "/llms.txt")),
        )

        if raw:
            raw["snapshot"] = checks.parse_html(raw.pop("html"), raw["final_url"])
        rendered = checks.merge_rendered(tf_page, url) if tf_page else None
        if raw is None and rendered is None:
            raise HTTPException(502, f"Could not load the page: {errors}")

        page = rendered or raw["snapshot"]
        serp_query = (req.keyword or page.get("title") or "").strip()[:120]
        serp = await timed("tinyfish_search", tinyfish.search(http, serp_query)) if serp_query else None

        ux = await ux_task if ux_task else None

    issues = checks.build_issues(url, raw, rendered, robots_txt, llms_txt, serp, serp_query, ux)

    t = time.monotonic()
    fixes, fix_err = await _safe(fixer.generate_fixes(url, issues, page, ux))
    timings["claude_fixes"] = round(time.monotonic() - t, 1)
    if fix_err:
        errors["claude_fixes"] = fix_err

    def view(snap):
        if not snap:
            return None
        return {k: v for k, v in snap.items() if k != "text_excerpt"}

    result = {
        "url": url,
        "score": checks.score(issues),
        "issues": issues,
        "raw": {"status": raw["status"], **view(raw["snapshot"])} if raw else None,
        "rendered": view(rendered),
        "serp": {"query": serp_query, "results": (serp or {}).get("results", [])[:10]} if serp else None,
        "ux": ux,
        "fixes": fixes,
        "robots_txt_found": robots_txt is not None,
        "llms_txt_found": llms_txt is not None,
        "timings": timings,
        "errors": errors,
        "total_seconds": round(time.monotonic() - started, 1),
        "cached": False,
    }
    cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
