"""Gemini turns audit findings into prioritized, paste-ready fixes (structured JSON output)."""
import asyncio
import json
import os

from google import genai
from google.genai import errors, types

# Tried in order; on overload (503) fall through to the next model.
MODELS = [os.environ.get("GEMINI_MODEL", "gemini-3.8-flash"), "gemini-flash-lite-latest", "gemini-3.5-flash-lite"]

SYSTEM = """You are a senior technical SEO consultant. You receive automated audit findings for one web page:
issues found by comparing the raw server HTML with the browser-rendered page (via TinyFish), live search
results, and a real-browser UX check. Write fixes a developer can apply today.

Rules:
- One fix per real issue, highest impact first. Merge issues that share one fix.
- `code` must be paste-ready (HTML tags, JSON-LD, robots.txt lines, or a short config/JS snippet). Use an
  empty string and code_language "none" when a fix is not code.
- Base the optimized title, meta description and JSON-LD on the page's actual content; never invent facts
  such as prices, ratings, addresses or reviews. Leave unknown JSON-LD fields out.
- Be concise and specific to this page, not generic SEO advice."""

FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "optimized_title": {"type": "string"},
        "optimized_meta_description": {"type": "string"},
        "json_ld": {"type": "string"},
        "fixes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "issue_ids": {"type": "array", "items": {"type": "string"}},
                    "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                    "title": {"type": "string"},
                    "why_it_matters": {"type": "string"},
                    "how_to_fix": {"type": "string"},
                    "code": {"type": "string"},
                    "code_language": {
                        "type": "string",
                        "enum": ["html", "json", "robots", "javascript", "text", "none"],
                    },
                },
                "required": ["issue_ids", "priority", "title", "why_it_matters",
                             "how_to_fix", "code", "code_language"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "optimized_title", "optimized_meta_description", "json_ld", "fixes"],
    "additionalProperties": False,
}

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    return _client


async def _call(payload: dict, model: str):
    return await _get_client().aio.models.generate_content(
        model=model,
        contents=json.dumps(payload, ensure_ascii=False),
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM,
            max_output_tokens=16000,
            response_mime_type="application/json",
            response_json_schema=FIX_SCHEMA,
        ),
    )


async def generate_fixes(url: str, issues: list[dict], page: dict, ux: dict | None) -> dict:
    payload = {
        "url": url,
        "issues": issues,
        "page": {
            "title": page.get("title"),
            "meta_description": page.get("meta_description"),
            "h1": page.get("h1"),
            "headings": [f"H{lvl}: {text}" for lvl, text in page.get("headings", [])[:25]],
            "existing_schema_types": page.get("jsonld_types"),
            "word_count": page.get("word_count"),
            "content_excerpt": page.get("text_excerpt"),
        },
        "browser_ux_check": ux,
    }
    for i, model in enumerate(MODELS):
        try:
            response = await asyncio.wait_for(_call(payload, model), timeout=60)
            break
        except (errors.ServerError, asyncio.TimeoutError):
            if i == len(MODELS) - 1:
                raise
    reason = response.candidates[0].finish_reason if response.candidates else None
    if reason == types.FinishReason.MAX_TOKENS:
        raise RuntimeError("Fix report was cut off (max_tokens)")
    if not response.text:
        raise RuntimeError(f"Gemini returned no fixes (finish_reason={reason})")
    return json.loads(response.text)
