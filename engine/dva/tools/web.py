"""Web tools: search (public results pages, no paid API) and reading pages as clean text."""
import re
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from . import Tool, ToolError

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Safari/537.36 DataveraAgent/2")
PAGE_LIMIT = 14_000


def _client():
    return httpx.AsyncClient(timeout=httpx.Timeout(25, connect=10), follow_redirects=True,
                             headers={"User-Agent": UA, "Accept-Language": "en"})


def _ddg(html):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for res in soup.select(".result"):
        a = res.select_one("a.result__a")
        if not a:
            continue
        href = a.get("href", "")
        if "uddg=" in href:
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
        if "duckduckgo.com/y.js" in href:  # ads
            continue
        snip = res.select_one(".result__snippet")
        out.append({"title": a.get_text(" ", strip=True), "url": href,
                    "snippet": snip.get_text(" ", strip=True) if snip else ""})
    return out


def _bing(html):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for li in soup.select("li.b_algo"):
        a = li.select_one("h2 a")
        if not a:
            continue
        p = li.select_one("p")
        out.append({"title": a.get_text(" ", strip=True), "url": a.get("href", ""),
                    "snippet": p.get_text(" ", strip=True) if p else ""})
    return out


async def search(query, limit=8):
    async with _client() as http:
        for url, parser in ((f"https://html.duckduckgo.com/html/?q={quote_plus(query)}", _ddg),
                            (f"https://www.bing.com/search?q={quote_plus(query)}", _bing)):
            try:
                r = await http.get(url)
                if r.status_code == 200:
                    rows = parser(r.text)
                    if rows:
                        return rows[:limit]
            except httpx.HTTPError:
                continue
    return []


async def web_search(ctx, query, limit=8):
    rows = await search(query, int(limit or 8))
    if not rows:
        return "No results (the search page may have asked for a check). Try different words."
    return "\n\n".join(f"[{i}] {r['title']}\n{r['url']}\n{r['snippet']}" for i, r in enumerate(rows, 1))


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
    links = []
    for a in main.find_all("a", href=True)[:400]:
        href = a["href"]
        if re.search(r"\.(csv|tsv|xlsx?|json|zip|pdf|parquet)(\?|$)", href, re.I):
            links.append(f"- {a.get_text(' ', strip=True)[:80]} → {httpx.URL(base_url).join(href) if base_url else href}")
    if links:
        body += "\n\n## File links on this page\n" + "\n".join(links[:40])
    return title, body


async def web_fetch(ctx, url, start=0):
    if not re.match(r"https?://", url or ""):
        raise ToolError("Give a full http(s) URL")
    async with _client() as http:
        try:
            r = await http.get(url)
        except httpx.HTTPError as exc:
            raise ToolError(f"Could not open the page: {type(exc).__name__}") from exc
    ctype = r.headers.get("content-type", "")
    if r.status_code >= 400:
        raise ToolError(f"The page answered HTTP {r.status_code}")
    if "html" in ctype or r.text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        title, text = html_to_text(r.text, str(r.url))
        text = f"# {title}\nURL: {r.url}\n\n{text}"
    elif "json" in ctype or "text" in ctype or "csv" in ctype:
        text = r.text
    else:
        return (f"{r.url} is a {ctype or 'binary'} file ({len(r.content):,} bytes). "
                "Use download_file to save it, then read it with read_file or run_python.")
    start = int(start or 0)
    piece = text[start:start + PAGE_LIMIT]
    if start + PAGE_LIMIT < len(text):
        piece += f"\n…(page continues; call web_fetch with start={start + PAGE_LIMIT})"
    return piece


async def download_file(ctx, url, filename=""):
    name = re.sub(r"[^\w.\-]+", "_", filename or Path(urlparse(url).path).name or "download")[:120]
    dest = ctx.workspace / "downloads" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    async with _client() as http:
        async with http.stream("GET", url) as r:
            if r.status_code >= 400:
                raise ToolError(f"HTTP {r.status_code}")
            with dest.open("wb") as f:
                async for chunk in r.aiter_bytes(1 << 16):
                    size += len(chunk)
                    if size > 200 * 2**20:
                        raise ToolError("File is larger than 200 MB; stopped")
                    f.write(chunk)
    ctx.emit("file_changed", path=str(dest), action="downloaded")
    return f"Saved {size:,} bytes to {dest}"


S = lambda props, req: {"type": "object", "properties": props, "required": req}  # noqa: E731
TOOLS = [
    Tool("web_search", "Search the web. Returns titles, links and snippets.",
         S({"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]), web_search, "web",
         label="Search the web", summary=lambda a: f"Search: {a.get('query')}"),
    Tool("web_fetch", "Read a web page as text (also lists data-file links on it).",
         S({"url": {"type": "string"}, "start": {"type": "integer"}}, ["url"]), web_fetch, "web",
         label="Read page", summary=lambda a: f"Read {a.get('url')}"),
    Tool("download_file", "Download a file from the web into this chat's workspace (downloads folder).",
         S({"url": {"type": "string"}, "filename": {"type": "string"}}, ["url"]), download_file, "web",
         label="Download", summary=lambda a: f"Download {a.get('url')}"),
]
