"""Real-browser access through the local Playwright MCP server.

Pages are read with one DOM extraction (`browser_evaluate`) instead of accessibility snapshots
and click references. The DOM includes links and text inside collapsed tabs/accordions, which is
what defeated the previous runs (the NOAA "#data" tab). Nothing here asks the model anything.
"""
import asyncio
import json
import re
from urllib.parse import quote_plus

from fastmcp import Client

from dv_discovery import host_of, is_datavera_url, search_result_links

EXTRACT_JS = r"""() => {
  const meta = (sel) => { const m = document.querySelector(sel); return m ? (m.getAttribute('content') || '').trim() : ''; };
  const clone = document.body ? document.body.cloneNode(true) : null;
  if (clone) clone.querySelectorAll('script,style,noscript,svg,template').forEach(n => n.remove());
  const text = clone ? (clone.textContent || '').replace(/\s+/g, ' ').trim() : '';
  const links = Array.from(document.querySelectorAll('a[href]')).slice(0, 1500).map(a => ({
    href: a.href, text: (a.textContent || a.getAttribute('title') || a.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim().slice(0, 160),
    visible: !!(a.offsetParent || a.getClientRects().length)}));
  return {url: location.href, title: document.title || '',
          site_name: meta('meta[property="og:site_name"]') || meta('meta[name="publisher"]') || meta('meta[name="DC.publisher"]') || meta('meta[name="dc.publisher"]') || meta('meta[name="author"]'),
          description: meta('meta[name="description"]') || meta('meta[property="og:description"]'),
          links: links, text: text.slice(0, 40000)};
}"""

ENGINES = {
    "duckduckgo": "https://html.duckduckgo.com/html/?q={q}",
    "bing": "https://www.bing.com/search?q={q}",
}


class BrowserError(RuntimeError):
    pass


def result_text(result):
    if isinstance(result, str):
        return result
    parts = getattr(result, "content", None) or []
    return "\n".join(str(getattr(p, "text", "") or (p.get("text", "") if isinstance(p, dict) else "")) for p in parts)


def parse_evaluate(text):
    """Pull the JSON value out of Playwright MCP's markdown-style response."""
    m = re.search(r"### Result\s*\n(.*?)(?:\n### |\Z)", text, re.S)
    body = m.group(1).strip() if m else text
    if body.startswith("```"):
        body = re.sub(r"^```\w*\n|\n```$", "", body)
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        start, end = body.find("{"), body.rfind("}")
        if start >= 0 and end > start:
            return json.loads(body[start:end + 1])
        raise


def error_in(text):
    if re.search(r"net::ERR_NAME_NOT_RESOLVED|ENOTFOUND|EAI_AGAIN", text):
        return "dns_error"
    if re.search(r"net::ERR_[A-Z_]+", text):
        return re.search(r"net::(ERR_[A-Z_]+)", text).group(1).lower()
    if re.search(r"(?m)^### Error|^Error:", text):
        return "browser_error"
    return None


class Browser:
    REQUIRED = {"browser_navigate", "browser_evaluate"}

    def __init__(self, url, progress, evidence_dir, engines=("duckduckgo", "bing"), action_timeout=60, settle_seconds=2,
                 templates=None):
        self.url = url
        self.progress = progress
        self.evidence_dir = evidence_dir
        self.templates = {**ENGINES, **(templates or {})}
        self.engines = [e for e in engines if e in self.templates]
        self.action_timeout = action_timeout
        self.settle_seconds = settle_seconds
        self.failed_hosts = set()
        self.client = None
        self.tools = set()
        self.pages_opened = 0

    async def __aenter__(self):
        self.client = Client(self.url)
        try:
            await asyncio.wait_for(self.client.__aenter__(), timeout=20)
        except Exception as exc:
            raise BrowserError(f"Playwright MCP is not reachable at {self.url}: {type(exc).__name__}. "
                               "Start it (see README) and retry.") from exc
        self.tools = {t.name for t in await self.client.list_tools()}
        missing = self.REQUIRED - self.tools
        if missing:
            await self.client.__aexit__(None, None, None)
            raise BrowserError(f"Playwright MCP lacks required tools: {sorted(missing)}")
        return self

    async def __aexit__(self, *exc):
        if self.client:
            await self.client.__aexit__(*exc)

    async def _call(self, name, args):
        self.progress.busy("browser", f"{name} {json.dumps(args)[:160] if name != 'browser_evaluate' else '(page extraction)'}")
        try:
            result = await asyncio.wait_for(self.client.call_tool(name, args, raise_on_error=False),
                                            timeout=self.action_timeout)
            text = result_text(result)
            if getattr(result, "is_error", False) and not error_in(text):
                text = "### Error\n" + text
        except asyncio.TimeoutError:
            text = f"### Error\nError: {name} timed out after {self.action_timeout}s"
        except Exception as exc:  # transport failures are reported, not raised mid-research
            text = f"### Error\nError: {type(exc).__name__}: {str(exc)[:300]}"
        finally:
            self.progress.idle()
        return text

    async def navigate(self, url):
        """Navigate only to http(s) URLs observed in pages/search results or given by the operator."""
        host = host_of(url)
        if is_datavera_url(url):
            return {"ok": False, "error": "policy_block", "detail": "Datavera is not browsed during research"}
        if not re.match(r"(?i)^https?://", url) or not host:
            return {"ok": False, "error": "policy_block", "detail": "only http(s) URLs"}
        if host in self.failed_hosts:
            return {"ok": False, "error": "host_failed_before", "detail": host}
        started = asyncio.get_running_loop().time()
        text = await self._call("browser_navigate", {"url": url})
        err = error_in(text)
        took = asyncio.get_running_loop().time() - started
        if err:
            if err in ("dns_error", "err_connection_refused", "err_connection_timed_out", "err_address_unreachable"):
                self.failed_hosts.add(host)
            self.progress.event("browser_error", f"{err} opening {url} ({took:.1f}s)")
            return {"ok": False, "error": err, "detail": text[:300]}
        self.progress.event("browser", f"Opened {url} ({took:.1f}s)")
        return {"ok": True}

    async def extract(self):
        text = await self._call("browser_evaluate", {"function": EXTRACT_JS})
        if error_in(text):
            return None
        try:
            page = parse_evaluate(text)
        except (json.JSONDecodeError, ValueError):
            return None
        if is_datavera_url(page.get("url", "")):
            await self._call("browser_navigate", {"url": "about:blank"})
            self.progress.event("policy", "A redirect reached Datavera; content discarded")
            return None
        return page

    async def open(self, url, label=""):
        """Navigate and return the extracted page, saving it as evidence."""
        nav = await self.navigate(url)
        if not nav["ok"]:
            return None, nav["error"]
        page = await self.extract()
        if page is not None and len(page.get("links", [])) < 3 and len(page.get("text", "")) < 400:
            await asyncio.sleep(self.settle_seconds)  # JavaScript-rendered page still loading
            page = await self.extract() or page
        if page is None:
            return None, "extraction_failed"
        self.pages_opened += 1
        page["requested_url"] = url
        page["label"] = label
        (self.evidence_dir / f"page_{self.pages_opened:02d}.json").write_text(
            json.dumps(page, ensure_ascii=False, indent=1), encoding="utf-8")
        hidden = sum(not l.get("visible", True) for l in page.get("links", []))
        self.progress.event("page", f"{page.get('title', '')[:90]!r}: {len(page.get('links', []))} links "
                            f"({hidden} in hidden panels), {len(page.get('text', ''))} text characters")
        return page, None

    async def search(self, query):
        """Visible search pages, first engine that yields relevant external links wins."""
        for engine in self.engines:
            url = self.templates[engine].format(q=quote_plus(query))
            self.progress.event("search", f"{engine}: {query}")
            page, err = await self.open(url, label=f"search:{engine}")
            if page is None:
                continue
            results = search_result_links(page.get("links", []), page.get("url", url), query)
            if not results:
                await asyncio.sleep(self.settle_seconds)
                again = await self.extract()
                if again:
                    results = search_result_links(again.get("links", []), again.get("url", url), query)
            if results:
                self.progress.event("search", f"{engine}: {len(results)} relevant result links")
                return results, engine
            self.progress.event("search", f"{engine}: no relevant result links (CAPTCHA, consent page or empty)")
        return [], None
