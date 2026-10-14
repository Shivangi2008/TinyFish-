"""Raw-HTML fetching, HTML parsing, and the SEO rules that turn snapshots into issues."""
import json
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
AI_BOTS = ["GPTBot", "ClaudeBot", "PerplexityBot", "Google-Extended", "CCBot"]
SEVERITY_WEIGHT = {"high": 12, "medium": 6, "low": 2}


# ---------- fetching ----------

async def fetch_raw(http: httpx.AsyncClient, url: str) -> dict:
    """Plain HTTP GET, no JavaScript: what a basic crawler sees on first pass."""
    resp = await http.get(url, headers={"User-Agent": BROWSER_UA}, timeout=20)
    return {"status": resp.status_code, "final_url": str(resp.url), "html": resp.text}


async def fetch_text(http: httpx.AsyncClient, url: str) -> str | None:
    try:
        resp = await http.get(url, headers={"User-Agent": BROWSER_UA}, timeout=10)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200 or "<html" in resp.text[:500].lower():
        return None
    return resp.text


# ---------- parsing ----------

def parse_html(html: str, base_url: str) -> dict:
    soup = BeautifulSoup(html or "", "html.parser")
    host = urlparse(base_url).netloc

    def meta(name: str) -> str:
        tag = soup.find("meta", attrs={"name": re.compile(f"^{name}$", re.I)})
        return (tag.get("content") or "").strip() if tag else ""

    canonical_tag = soup.find("link", rel=lambda v: v and "canonical" in v)
    og = {
        m.get("property"): m.get("content", "")
        for m in soup.find_all("meta", attrs={"property": re.compile(r"^og:")})
    }

    jsonld_types: list[str] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data])
        for item in items:
            if isinstance(item, dict) and item.get("@type"):
                t = item["@type"]
                jsonld_types.extend(t if isinstance(t, list) else [t])

    headings = [
        (int(h.name[1]), h.get_text(" ", strip=True))
        for h in soup.find_all(re.compile(r"^h[1-6]$"))
    ]
    images = soup.find_all("img")
    missing_alt = [
        urljoin(base_url, img.get("src") or img.get("data-src") or "")[:150]
        for img in images
        if not (img.get("alt") or "").strip()
    ]
    internal = external = 0
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        if urlparse(urljoin(base_url, href)).netloc == host:
            internal += 1
        else:
            external += 1

    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)

    return {
        "title": soup.title.get_text(strip=True) if soup.title else "",
        "meta_description": meta("description"),
        "meta_robots": meta("robots"),
        "viewport": meta("viewport"),
        "canonical": canonical_tag.get("href", "") if canonical_tag else "",
        "lang": soup.html.get("lang", "") if soup.html else "",
        "og": og,
        "jsonld_types": jsonld_types,
        "h1": [t for lvl, t in headings if lvl == 1],
        "headings": headings[:40],
        "images": len(images),
        "images_missing_alt": missing_alt[:20],
        "images_missing_alt_count": len(missing_alt),
        "internal_links": internal,
        "external_links": external,
        "word_count": len(re.findall(r"\w+", text)),
        "text_excerpt": text[:4000],
    }


def merge_rendered(tf_result: dict, base_url: str) -> dict:
    """TinyFish Fetch returns cleaned body HTML plus head metadata separately; combine them."""
    snap = parse_html(tf_result.get("text", ""), base_url)
    meta = tf_result.get("page_metadata") or {}
    snap["title"] = tf_result.get("title") or snap["title"]
    snap["meta_description"] = tf_result.get("description") or snap["meta_description"]
    snap["canonical"] = meta.get("canonical") or snap["canonical"]
    snap["viewport"] = meta.get("viewport") or snap["viewport"]
    snap["meta_robots"] = meta.get("robots") or snap["meta_robots"]
    snap["og"] = {f"og:{k}": v for k, v in (meta.get("og") or {}).items()} or snap["og"]
    snap["lang"] = tf_result.get("language") or snap["lang"]
    snap["link_count"] = len(tf_result.get("links") or [])
    snap["image_link_count"] = len(tf_result.get("image_links") or [])
    return snap


def robots_blocks(robots_txt: str, agent: str) -> bool:
    """True if robots.txt has a group naming `agent` (or `*` when agent == '*') with `Disallow: /`."""
    groups: list[dict] = []
    for raw_line in robots_txt.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (p.strip() for p in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            # Consecutive User-agent lines share one group; a User-agent after rules starts a new one.
            if not groups or groups[-1]["rules"]:
                groups.append({"agents": [], "rules": []})
            groups[-1]["agents"].append(value.lower())
        elif key in ("allow", "disallow") and groups:
            groups[-1]["rules"].append((key, value))
    for group in groups:
        if agent.lower() in group["agents"]:
            return ("disallow", "/") in group["rules"] and ("allow", "/") not in group["rules"]
    return False


# ---------- rules ----------

def _issue(issues, id_, severity, category, title, detail, evidence=""):
    issues.append({
        "id": id_, "severity": severity, "category": category,
        "title": title, "detail": detail, "evidence": evidence,
    })


def build_issues(url, raw, rendered, robots_txt, llms_txt, serp, serp_query, ux) -> list[dict]:
    issues: list[dict] = []
    raw_ok = raw is not None and raw["status"] < 400
    r = raw["snapshot"] if raw_ok else None
    g = rendered  # what the browser (and Google's renderer) ends up with

    # --- Rendering gap: raw HTML vs TinyFish browser render ---
    if raw is not None and not raw_ok:
        _issue(issues, "raw-status", "high", "Rendering",
               f"Server returns HTTP {raw['status']} to a plain crawler",
               "Crawlers that don't run a full browser get an error instead of the page.")
    if r and g:
        if g["word_count"] > 100 and r["word_count"] < 0.3 * g["word_count"]:
            share = 100 - round(100 * r["word_count"] / g["word_count"])
            _issue(issues, "js-content", "high", "Rendering",
                   f"~{share}% of the content only exists after JavaScript runs",
                   "Google renders JS on a delayed second pass; other crawlers and AI bots often never do. "
                   "Server-side render or pre-render the main content.",
                   f"raw HTML: {r['word_count']} words, rendered: {g['word_count']} words")
        if g["h1"] and not r["h1"]:
            _issue(issues, "js-h1", "high", "Rendering", "The H1 only appears after JavaScript",
                   "The main heading is missing from the HTML the server sends.",
                   f"rendered H1: {g['h1'][0][:100]}")
        if g["title"] and not r["title"]:
            _issue(issues, "js-title", "high", "Rendering", "The <title> is injected by JavaScript",
                   "Put the title in the server HTML so every crawler sees it.")
        if g.get("meta_description") and not r["meta_description"]:
            _issue(issues, "js-meta", "medium", "Rendering",
                   "Meta description is injected by JavaScript", "Render it server-side.")
        if g["internal_links"] - r["internal_links"] > 10 and g["internal_links"] > 2 * r["internal_links"]:
            _issue(issues, "js-links", "medium", "Rendering", "Most internal links are added by JavaScript",
                   "Crawlers discover pages through links; JS-only links slow down or block discovery.",
                   f"raw: {r['internal_links']} internal links, rendered: {g['internal_links']}")

    head = r or g  # prefer what the server sends for head tags
    body = g or r  # prefer what visitors see for content
    if not head or not body:
        return issues

    # --- On-page ---
    title = head["title"] or (g or {}).get("title", "")
    if not title:
        _issue(issues, "title-missing", "high", "On-page", "Missing <title>", "Every page needs a unique title.")
    elif not 30 <= len(title) <= 60:
        _issue(issues, "title-length", "medium", "On-page",
               f"Title is {len(title)} characters (aim for 30-60)",
               "Long titles get cut off in results; short ones waste ranking space.", title)
    desc = head["meta_description"] or (g or {}).get("meta_description", "")
    if not desc:
        _issue(issues, "desc-missing", "medium", "On-page", "Missing meta description",
               "Google will invent a snippet; a written one usually gets more clicks.")
    elif not 70 <= len(desc) <= 160:
        _issue(issues, "desc-length", "low", "On-page",
               f"Meta description is {len(desc)} characters (aim for 70-160)", "", desc)
    if not body["h1"]:
        _issue(issues, "h1-missing", "high", "On-page", "No H1 heading",
               "The H1 tells search engines and readers what the page is about.")
    elif len(body["h1"]) > 1:
        _issue(issues, "h1-multiple", "medium", "On-page", f"{len(body['h1'])} H1 headings",
               "Use one H1 for the main topic and H2/H3 for sections.", " | ".join(body["h1"][:3]))
    levels = [lvl for lvl, _ in body["headings"]]
    skips = [(a, b) for a, b in zip(levels, levels[1:]) if b > a + 1]
    if skips:
        _issue(issues, "heading-skip", "low", "On-page", "Heading levels skip",
               "Keep a logical outline (H2 then H3, not H2 then H4).",
               ", ".join(f"H{a}->H{b}" for a, b in skips[:5]))
    alt_src = body if body["images_missing_alt_count"] >= head["images_missing_alt_count"] else head
    if alt_src["images_missing_alt_count"]:
        _issue(issues, "img-alt", "medium", "On-page",
               f"{alt_src['images_missing_alt_count']} of {alt_src['images']} images have no alt text",
               "Alt text helps image search and accessibility.",
               "\n".join(alt_src["images_missing_alt"][:5]))
    if body["word_count"] < 300:
        _issue(issues, "thin-content", "medium", "On-page", f"Thin content ({body['word_count']} words)",
               "Pages under ~300 words rarely rank for anything competitive.")

    # --- Technical ---
    robots_meta = (head["meta_robots"] + " " + (g or {}).get("meta_robots", "")).lower()
    if "noindex" in robots_meta:
        _issue(issues, "noindex", "high", "Technical", "Page is set to noindex",
               "Search engines are told not to index this page.", robots_meta.strip())
    if not head["canonical"] and not (g or {}).get("canonical"):
        _issue(issues, "canonical", "medium", "Technical", "No canonical URL",
               "Add <link rel=\"canonical\"> to avoid duplicate-content splits.")
    if not head["viewport"] and not (g or {}).get("viewport"):
        _issue(issues, "viewport", "high", "Technical", "No mobile viewport tag",
               "Google indexes the mobile version first; without a viewport the page isn't mobile-friendly.")
    if not head["lang"] and not (g or {}).get("lang"):
        _issue(issues, "lang", "low", "Technical", "No lang attribute on <html>", "")
    og = head["og"] or (g or {}).get("og", {})
    if not og.get("og:title") or not og.get("og:image"):
        _issue(issues, "og", "low", "Technical", "Incomplete Open Graph tags",
               "Shared links look bare on social and chat apps without og:title and og:image.")
    if not head["jsonld_types"]:
        _issue(issues, "schema", "medium", "Technical", "No structured data (JSON-LD)",
               "Schema markup enables rich results and helps AI answers understand the page.")
    if robots_txt is None:
        _issue(issues, "robots-missing", "low", "Technical", "No robots.txt", "")
    elif robots_blocks(robots_txt, "*"):
        _issue(issues, "robots-all", "high", "Technical", "robots.txt blocks all crawlers", "", "User-agent: * / Disallow: /")

    # --- AI search readiness ---
    if robots_txt:
        blocked = [b for b in AI_BOTS if robots_blocks(robots_txt, b)]
        if blocked:
            _issue(issues, "ai-bots", "medium", "AI readiness", "robots.txt blocks AI crawlers",
                   "These bots can't read the page, so AI answers won't cite it.", ", ".join(blocked))
    if llms_txt is None:
        _issue(issues, "llms-txt", "low", "AI readiness", "No /llms.txt",
               "An llms.txt file gives AI assistants a clean summary of your site.")

    # --- Search visibility (TinyFish Search) ---
    if serp is not None:
        host = urlparse(url).netloc.removeprefix("www.")
        results = serp.get("results", [])
        ours = [x for x in results if host in urlparse(x.get("url", "")).netloc]
        if not ours:
            _issue(issues, "serp", "medium", "Search visibility",
                   f"Not in the top {len(results)} results for \"{serp_query}\"",
                   "Competitors currently own this query.",
                   "\n".join(f"#{x.get('position')} {x.get('url')}" for x in results[:5]))

    # --- Real-visitor UX (TinyFish Agent) ---
    if ux:
        if ux.get("blocking_overlay"):
            _issue(issues, "interstitial", "high", "UX", "A popup covers the content on load",
                   "Google demotes pages with intrusive interstitials, especially on mobile.",
                   ux.get("overlay_description", ""))
        if ux.get("content_visible_without_interaction") is False:
            _issue(issues, "hidden-content", "medium", "UX",
                   "Main content isn't visible without interaction", "")
        for i, item in enumerate((ux.get("broken_or_empty_elements") or [])[:5]):
            _issue(issues, f"broken-{i}", "medium", "UX", "Broken or empty element", item)
        for i, item in enumerate((ux.get("ux_issues") or [])[:5]):
            _issue(issues, f"ux-{i}", "low", "UX", "UX issue spotted by the browser agent", item)

    order = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda x: order[x["severity"]])
    return issues


def score(issues: list[dict]) -> int:
    return max(0, 100 - sum(SEVERITY_WEIGHT[i["severity"]] for i in issues))
