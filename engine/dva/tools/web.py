"""Web tools: search, reading pages (HTML and PDF) and downloading files.

Search engines often answer automated requests with generic pages (sign-in links, trending items) instead of
results. So every result is scored against the query's words and unrelated ones are dropped; several engines are
tried; if none gives relevant results, the browser helper (a real Edge window) is used; and if that fails too, the
agent is told plainly that nothing relevant was found, so it changes approach instead of searching again."""
import asyncio
import base64
import io
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from . import Tool, ToolError

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/141.0 Safari/537.36 Edg/141.0")
PAGE_LIMIT = 14_000
SEARCH_HOSTS = ("bing.com", "duckduckgo.com", "google.com", "yahoo.com", "startpage.com", "mojeek.com", "msn.com",
                "microsoft.com/en-us/bing", "brave.com")
STOP = set("""a an and the of in on for to with from by at as or is are be this that these those what which who how
when where why latest new recent current official data statistics stats report reports information info about
site https http www com org net html pdf page pages free online best top list vs""".split())
AUTHORITATIVE = re.compile(r"\.(gov|gouv|gob|go|govt|int|edu|ac)(\.[a-z]{2})?$|(^|\.)(un|who|worldbank|imf|oecd|irena|"
                           r"iea|ren21|eia|fao|unep|undp|unicef|wto|afdb|ourworldindata|data|statistics|stats|"
                           r"europa|esa|nasa|noaa|nature|science|wikipedia)\.", re.I)
DATA_EXT = re.compile(r"\.(csv|tsv|xlsx?|json|zip|parquet|xml|ods)(\?|#|$)", re.I)
DOC_EXT = re.compile(r"\.(pdf|docx?|pptx?)(\?|#|$)", re.I)


def _client(timeout=25):
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=12), follow_redirects=True,
                             headers={"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9",
                                      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})


# ---------------------------------------------------------------------- relevance
def terms_of(text):
    words = re.findall(r"[a-z0-9]{3,}", re.sub(r"(?i)\bsite:\S+", "", str(text or "")).lower())
    return [w for w in dict.fromkeys(words) if w not in STOP]


def host_of(url):
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def is_search_page(url):
    host = host_of(url)
    return any(host == h or host.endswith("." + h) for h in SEARCH_HOSTS if "/" not in h)


def unwrap(url, base=""):
    """Decode search-engine redirect links into the real address."""
    try:
        url = urljoin(base, (url or "").strip())
        parts = urlsplit(url)
        params = parse_qs(parts.query)
        host = (parts.hostname or "").lower()
        if host.endswith("bing.com") and parts.path.startswith("/ck/a"):
            enc = params.get("u", [""])[0]
            if enc.startswith("a1"):
                enc = enc[2:]
                url = base64.urlsafe_b64decode(enc + "=" * (-len(enc) % 4)).decode("utf-8", "replace")
        elif host.endswith("duckduckgo.com") and "uddg" in params:
            url = unquote(params["uddg"][0])
        elif host.endswith("google.com") and parts.path == "/url":
            url = params.get("q", params.get("url", [url]))[0]
        if not re.match(r"https?://", url) or not host_of(url):
            return None
        return url
    except (ValueError, UnicodeError):
        return None


def score(row, terms):
    hay = f"{row.get('title', '')} {row.get('snippet', '')} {row.get('url', '')}".lower()
    hits = sum(1 for t in terms if t in hay)
    if not terms:
        return 1.0
    need = 1 if len(terms) <= 2 else 2           # longer queries must match at least two words
    if hits < need:
        return 0.0
    bonus = 0.5 if AUTHORITATIVE.search(host_of(row.get("url", ""))) else 0.0
    bonus += 0.3 if DATA_EXT.search(row.get("url", "")) or DOC_EXT.search(row.get("url", "")) else 0.0
    return hits / len(terms) + bonus


# ---------------------------------------------------------------------- engines
def generic_links(html, base_url):
    """Fallback parser for any results page: external links with their text and nearby text."""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for a in soup.find_all("a", href=True):
        url = unwrap(a["href"], base_url)
        title = a.get_text(" ", strip=True)
        if not url or is_search_page(url) or len(title) < 8:
            continue
        parent = a.find_parent(["li", "div", "td", "article"])
        snippet = parent.get_text(" ", strip=True)[:300] if parent else ""
        rows.append({"title": title[:200], "url": url, "snippet": snippet})
    return rows


def parse_ddg(html, base):
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for res in soup.select(".result, .web-result"):
        a = res.select_one("a.result__a, a.result-link")
        if not a:
            continue
        url = unwrap(a.get("href", ""), base)
        if not url or "duckduckgo.com/y.js" in a.get("href", ""):
            continue
        snip = res.select_one(".result__snippet, .result-snippet")
        rows.append({"title": a.get_text(" ", strip=True), "url": url,
                     "snippet": snip.get_text(" ", strip=True) if snip else ""})
    if not rows:  # lite layout: table rows
        for a in soup.select("a.result-link"):
            url = unwrap(a.get("href", ""), base)
            if url:
                tr = a.find_parent("tr")
                nxt = tr.find_next_sibling("tr") if tr else None
                rows.append({"title": a.get_text(" ", strip=True), "url": url,
                             "snippet": nxt.get_text(" ", strip=True)[:300] if nxt else ""})
    return rows


def parse_mojeek(html, base):
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for li in soup.select("ul.results-standard li, .results li"):
        a = li.select_one("h2 a, a.title, a.ob")
        if not a:
            continue
        url = unwrap(a.get("href", ""), base)
        snip = li.select_one("p.s, p")
        if url:
            rows.append({"title": a.get_text(" ", strip=True), "url": url,
                         "snippet": snip.get_text(" ", strip=True) if snip else ""})
    return rows


def parse_bing(html, base):
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for li in soup.select("li.b_algo"):
        a = li.select_one("h2 a")
        if not a:
            continue
        url = unwrap(a.get("href", ""), base)
        p = li.select_one("p, .b_caption p, .b_lineclamp2")
        if url:
            rows.append({"title": a.get_text(" ", strip=True), "url": url, "snippet": p.get_text(" ", strip=True) if p else ""})
    return rows


ENGINES = [
    ("DuckDuckGo", "POST", "https://html.duckduckgo.com/html/", parse_ddg),
    ("DuckDuckGo lite", "POST", "https://lite.duckduckgo.com/lite/", parse_ddg),
    ("Mojeek", "GET", "https://www.mojeek.com/search?q={q}", parse_mojeek),
    ("Bing", "GET", "https://www.bing.com/search?q={q}&setlang=en&cc=gb", parse_bing),
]


async def _engine(http, name, method, url, parser, query):
    try:
        if method == "POST":
            origin = f"{urlsplit(url).scheme}://{urlsplit(url).hostname}"
            r = await http.post(url, data={"q": query, "kl": ""}, headers={"Referer": url, "Origin": origin})
        else:
            r = await http.get(url.format(q=quote_plus(query)))
        if r.status_code != 200:
            return [], f"HTTP {r.status_code}"
        rows = parser(r.text, str(r.url)) or generic_links(r.text, str(r.url))
        return rows, None
    except httpx.HTTPError as exc:
        return [], type(exc).__name__


LINKS_JS = r"""() => Array.from(document.querySelectorAll('a[href]')).slice(0, 800).map(a => {
  const box = a.closest('li, article, div');
  return {url: a.href, title: (a.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 200),
          snippet: box ? (box.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 300) : ''};
})"""


def _evaluate_json(text):
    m = re.search(r"### Result\s*\n(.*?)(?:\n### |\Z)", text, re.S)
    body = (m.group(1) if m else text).strip()
    body = re.sub(r"^```\w*\n|\n```$", "", body)
    start = min([i for i in (body.find("["), body.find("{")) if i >= 0] or [0])
    return json.loads(body[start:])


async def browser_search(app, query):
    """Search in the real browser (Playwright MCP + Edge): far fewer bot-check pages."""
    from fastmcp import Client
    url = await app.services.browser()
    rows = []
    async with Client(url, timeout=60) as client:
        for engine in (f"https://duckduckgo.com/?q={quote_plus(query)}&ia=web",
                       f"https://www.bing.com/search?q={quote_plus(query)}&setlang=en"):
            await client.call_tool("browser_navigate", {"url": engine}, raise_on_error=False)
            await asyncio.sleep(2)
            res = await client.call_tool("browser_evaluate", {"function": LINKS_JS}, raise_on_error=False)
            text = "\n".join(getattr(c, "text", "") for c in (res.content or []))
            try:
                found = _evaluate_json(text)
            except (ValueError, json.JSONDecodeError):
                continue
            for row in found:
                u = unwrap(row.get("url", ""))
                if u and not is_search_page(u):
                    rows.append({**row, "url": u})
            if rows:
                break
    return rows


async def search(query, limit=8, app=None, use_browser=True):
    """Relevant results only. Returns (rows, report) — report says which engines ran and what was dropped."""
    terms = terms_of(query)
    seen, kept, report = set(), [], {"engines": [], "dropped": 0}
    async with _client() as http:
        for name, method, url, parser in ENGINES:
            rows, err = await _engine(http, name, method, url, parser, query)
            relevant = 0
            for row in rows:
                key = row["url"].split("#")[0].rstrip("/")
                if key in seen:
                    continue
                seen.add(key)
                s = score(row, terms)
                if s <= 0:
                    report["dropped"] += 1
                    continue
                relevant += 1
                kept.append({**row, "score": round(s, 2), "engine": name})
            report["engines"].append(f"{name}: {err or f'{relevant} relevant of {len(rows)}'}")
            if len(kept) >= max(3, limit // 2):
                break
    if not kept and use_browser and app is not None and app.db.setting("browser_search", True):
        try:
            rows = await asyncio.wait_for(browser_search(app, query), 120)
            for row in rows:
                s = score(row, terms)
                if s > 0 and row["url"] not in seen:
                    seen.add(row["url"])
                    kept.append({**row, "score": round(s, 2), "engine": "browser"})
                elif s <= 0:
                    report["dropped"] += 1
            report["engines"].append(f"browser: {len(kept)} relevant")
        except Exception as exc:
            report["engines"].append(f"browser: unavailable ({type(exc).__name__})")
    kept.sort(key=lambda r: -r["score"])
    return kept[:limit], report


async def web_search(ctx, query, limit=8):
    rows, report = await search(query, int(limit or 8), ctx.app)
    ctx.notes.setdefault("searches", []).append({"query": query, "found": len(rows)})
    if not rows:
        return (f"NO RELEVANT RESULTS for {query!r}. Engines: {'; '.join(report['engines'])}. "
                f"{report['dropped']} unrelated results were discarded. Do not run this search again. Instead: use "
                "2-4 different words, open a known official source directly with web_fetch (e.g. the website of the "
                "statistics office or organisation that publishes this), or answer with what you already have.")
    lines = [f"[{i}] {r['title']}\n{r['url']}\n{r['snippet'][:240]}" for i, r in enumerate(rows, 1)]
    note = f"\n({report['dropped']} unrelated results discarded)" if report["dropped"] else ""
    return "\n\n".join(lines) + note


# ---------------------------------------------------------------------- reading pages
def html_to_text(html, base_url=""):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe"]):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    main = soup.find("main") or soup.find("article") or soup.body or soup
    lines = []
    for el in main.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "th", "pre", "blockquote"]):
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if el.name in ("h1", "h2", "h3", "h4"):
            text = "#" * int(el.name[1]) + " " + text
        elif el.name == "li":
            text = "- " + text
        lines.append(text)
    body = "\n".join(dict.fromkeys(lines)) or main.get_text("\n", strip=True)
    files = []
    for a in (soup.body or soup).find_all("a", href=True)[:1500]:
        href = urljoin(base_url, a["href"]) if base_url else a["href"]
        if DATA_EXT.search(href) or DOC_EXT.search(href) or re.search(r"(?i)download|export|format=(csv|xlsx|json)", href):
            label = a.get_text(" ", strip=True)[:90]
            if re.match(r"https?://", href):
                files.append({"label": label, "url": href})
    uniq = list({f["url"]: f for f in files}.values())[:60]
    return title, body, uniq


def pdf_to_text(data, max_pages=40):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for i, page in enumerate(reader.pages[:max_pages]):
        try:
            pages.append(f"--- page {i + 1} ---\n{page.extract_text() or ''}")
        except Exception:
            pages.append(f"--- page {i + 1} --- (could not read)")
    more = len(reader.pages) - max_pages
    return "\n".join(pages) + (f"\n…({more} more pages)" if more > 0 else ""), len(reader.pages)


async def fetch_page(url, max_bytes=40 * 2**20):
    """Download a page and turn it into text. Returns dict(kind, url, title, text, files, error)."""
    async with _client(40) as http:
        try:
            r = await http.get(url)
        except httpx.HTTPError as exc:
            return {"kind": "error", "url": url, "error": f"could not open ({type(exc).__name__})"}
    if r.status_code >= 400:
        return {"kind": "error", "url": str(r.url), "error": f"HTTP {r.status_code}"}
    ctype = r.headers.get("content-type", "").lower()
    if "pdf" in ctype or str(r.url).lower().split("?")[0].endswith(".pdf"):
        if len(r.content) > max_bytes:
            return {"kind": "error", "url": str(r.url), "error": "PDF larger than 40 MB"}
        try:
            text, pages = await asyncio.to_thread(pdf_to_text, r.content)
        except Exception as exc:
            return {"kind": "error", "url": str(r.url), "error": f"unreadable PDF ({type(exc).__name__})"}
        return {"kind": "pdf", "url": str(r.url), "title": Path(urlsplit(str(r.url)).path).name, "text": text,
                "files": [], "pages": pages}
    if "html" in ctype or r.text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        title, text, files = html_to_text(r.text, str(r.url))
        return {"kind": "html", "url": str(r.url), "title": title, "text": text, "files": files}
    if any(t in ctype for t in ("json", "text", "csv")):
        return {"kind": "text", "url": str(r.url), "title": "", "text": r.text, "files": []}
    return {"kind": "binary", "url": str(r.url), "title": "", "text": "", "files": [], "content_type": ctype,
            "size": len(r.content)}


async def web_fetch(ctx, url, start=0):
    if not re.match(r"https?://", url or ""):
        raise ToolError("Give a full http(s) URL")
    page = await fetch_page(url)
    if page["kind"] == "error":
        raise ToolError(f"{page['url']}: {page['error']}")
    if page["kind"] == "binary":
        return (f"{page['url']} is a {page['content_type'] or 'binary'} file ({page['size']:,} bytes). Use "
                "download_file to save it, then read it with read_file or run_python.")
    text = f"# {page['title']}\nURL: {page['url']}\n\n{page['text']}"
    if page["files"]:
        text += "\n\n## File links on this page\n" + "\n".join(f"- {f['label'] or 'file'} → {f['url']}" for f in page["files"][:40])
    start = int(start or 0)
    piece = text[start:start + PAGE_LIMIT]
    if start + PAGE_LIMIT < len(text):
        piece += f"\n…(page continues; call web_fetch with start={start + PAGE_LIMIT})"
    return piece


async def save_url(url, folder, filename="", max_bytes=200 * 2**20):
    name = re.sub(r"[^\w.\-]+", "_", filename or Path(urlsplit(url).path).name or "download")[:120] or "download"
    dest = Path(folder) / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = 1
    while dest.exists():
        dest = dest.with_name(f"{Path(name).stem}-{n}{Path(name).suffix}")
        n += 1
    size = 0
    async with _client(60) as http:
        async with http.stream("GET", url) as r:
            if r.status_code >= 400:
                raise ToolError(f"HTTP {r.status_code}")
            with dest.open("wb") as f:
                async for chunk in r.aiter_bytes(1 << 16):
                    size += len(chunk)
                    if size > max_bytes:
                        f.close()
                        dest.unlink(missing_ok=True)
                        raise ToolError(f"File is larger than {max_bytes // 2**20} MB; stopped")
                    f.write(chunk)
    return dest, size


async def download_file(ctx, url, filename=""):
    dest, size = await save_url(url, ctx.workspace / "downloads", filename)
    ctx.emit("file_changed", path=str(dest), action="downloaded")
    return f"Saved {size:,} bytes to {dest}"


S = lambda props, req: {"type": "object", "properties": props, "required": req}  # noqa: E731
TOOLS = [
    Tool("web_search", "Search the web. Returns only results relevant to the query (unrelated ones are dropped). "
         "If it says NO RELEVANT RESULTS, change approach instead of searching again.",
         S({"query": {"type": "string", "description": "3-8 plain words"}, "limit": {"type": "integer"}}, ["query"]),
         web_search, "web", label="Search the web", summary=lambda a: f"Search: {a.get('query')}"),
    Tool("web_fetch", "Read a web page or PDF as text (also lists data/document file links on the page).",
         S({"url": {"type": "string"}, "start": {"type": "integer"}}, ["url"]), web_fetch, "web",
         label="Read page", summary=lambda a: f"Read {a.get('url')}"),
    Tool("download_file", "Download a file (data, PDF…) into this chat's workspace (downloads folder).",
         S({"url": {"type": "string"}, "filename": {"type": "string"}}, ["url"]), download_file, "web",
         label="Download", summary=lambda a: f"Download {a.get('url')}"),
]
