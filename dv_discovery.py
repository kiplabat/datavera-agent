"""Pure, testable rules for search results, data links and reuse terms. No network, no model."""
import base64
import re
from urllib.parse import parse_qs, urljoin, urldefrag, urlsplit

SEARCH_HOSTS = ("bing.com", "duckduckgo.com", "google.com", "yahoo.com", "startpage.com")
TABULAR = {"csv", "tsv", "xlsx", "json"}
SUPPORTED = TABULAR | {"zip"}  # ZIP: opened and searched for one CSV/TSV table
UNSUPPORTED = {"xls", "nc", "nc4", "netcdf", "grib", "grb", "grb2", "hdf", "h5", "he5", "shp", "gz",
               "tar", "7z", "pdf", "doc", "docx", "ppt", "pptx", "parquet", "geojson", "kml", "kmz", "tif", "tiff"}
STOP = set("""a an and the of in on for to with from by at as or is are be this that these those site https http www com
gov org net csv json xlsx tsv download downloads data dataset datasets open public original publisher license licence
monthly annual yearly daily global world worldwide time series table tables file files api statistics stats""".split())


def host_of(url):
    try:
        return (urlsplit(str(url)).hostname or "").rstrip(".").lower()
    except ValueError:
        return ""


def site_of(url):
    """Approximate registrable domain: last two labels, or three for e.g. *.gov.uk / *.co.ke."""
    parts = host_of(url).split(".")
    if len(parts) >= 3 and parts[-2] in {"gov", "co", "ac", "org", "go", "or", "gob", "gouv", "com", "edu", "govt"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def is_datavera_url(url):
    host = host_of(url)
    return host == "datavera.io" or host.endswith(".datavera.io")


def is_search_url(url):
    host = host_of(url)
    return any(host == h or host.endswith("." + h) for h in SEARCH_HOSTS)


def page_key(url):
    """A page and its #fragments are the same document."""
    return urldefrag(str(url))[0].rstrip("/")


def unwrap_url(url, base=""):
    """Decode search-engine redirect links; return a clean http(s) URL or None."""
    try:
        url = urljoin(base, str(url).strip().strip('"'))
        parsed = urlsplit(url)
        params = parse_qs(parsed.query)
        host = (parsed.hostname or "").lower()
        if (host == "bing.com" or host.endswith(".bing.com")) and parsed.path.startswith("/ck/a"):
            encoded = params.get("u", [""])[0]
            if encoded.startswith("a1"):
                value = encoded[2:]
                url = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
        elif (host == "duckduckgo.com" or host.endswith(".duckduckgo.com")) and "uddg" in params:
            url = params["uddg"][0]
        elif (host == "google.com" or host.endswith(".google.com")) and parsed.path == "/url":
            url = params.get("q", params.get("url", [url]))[0]
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            return None
        return url
    except (ValueError, UnicodeError):
        return None


def topic_terms(text):
    words = re.findall(r"[a-z0-9]{3,}", re.sub(r"(?i)\bsite:\S+", "", str(text)).lower())
    return {w for w in words if w not in STOP}


def search_result_links(links, page_url, query, limit=12):
    """External, relevant, de-duplicated result links from a search results page."""
    terms = topic_terms(query)
    seen, scored = set(), []
    for row in links:
        url = unwrap_url(row.get("href", ""), page_url)
        if not url or is_search_url(url) or is_datavera_url(url):
            continue
        key = page_key(url)
        label = re.sub(r"\s+", " ", row.get("text", "")).strip()
        if key in seen or not label or re.match(r"(?i)^(privacy|terms|settings|feedback|sign in|log in|help|more|next|images|videos|news|maps)\b", label):
            continue
        seen.add(key)
        hay = (label + " " + url).lower()
        score = sum(t in hay for t in terms) if terms else 1
        if score:
            scored.append((score, {"url": url, "label": label[:160]}))
    scored.sort(key=lambda item: -item[0])
    return [row for _, row in scored[:limit]]


def data_format(url, label=""):
    """Return a tabular format name, an unsupported format name, or None for ordinary links."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    path = parsed.path.lower()
    query = parsed.query.lower()
    ext = path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""
    if ext in SUPPORTED:
        return ext
    if ext in UNSUPPORTED:
        return ext
    if re.search(r"(?:^|&)(?:format|output|filetype|download|type|f)=(csv|tsv|xlsx|json)\b", query):
        return re.search(r"(csv|tsv|xlsx|json)", query).group(1)
    if path.endswith("/csv") or path.endswith("rows.csv") or re.search(r"/export\.csv$|/download\.csv$", path):
        return "csv"
    if re.search(r"(?i)\b(csv)\b", label) and re.search(r"/(download|export|file)s?\b", path):
        return "csv"
    return None


LICENCE_RULES = [
    # (pattern, licence enum or None for rejection, label). Order matters: restrictive terms first.
    (r"cc[\s-]*by[\s-]*(?:sa[\s-]*)?nc\b|non-?commercial|creativecommons\.org/licenses/by-nc", None, "non-commercial terms"),
    (r"cc[\s-]*by[\s-]*(?:nc[\s-]*)?nd\b|no ?derivatives|creativecommons\.org/licenses/by-nd", None, "no-derivatives terms"),
    (r"cc[\s-]*by[\s-]*sa[\s-]*4\.0|attribution[\s-]+share[\s-]?alike (?:4\.0|international)|creativecommons\.org/licenses/by-sa/4\.0",
     "CC-BY-SA-4.0", "CC BY-SA 4.0"),
    (r"cc[\s-]*by[\s-]*4\.0|creative commons attribution (?:4\.0|international)|creativecommons\.org/licenses/by/4\.0",
     "CC-BY-4.0", "CC BY 4.0"),
    (r"\bcc0\b|cc[\s-]zero|public domain dedication|creativecommons\.org/publicdomain/zero", "CC0", "CC0"),
    (r"open database licen[cs]e|\bodbl\b|opendatacommons\.org/licenses/odbl", "ODbL", "ODbL"),
    (r"open data commons public domain|\bpddl\b", "Custom", "ODC Public Domain Dedication (PDDL)"),
    (r"cc[\s-]*by[\s-]*(?:sa[\s-]*)?[23]\.[05]|creative commons attribution [23]\.[05]|creativecommons\.org/licenses/by(?:-sa)?/[23]",
     "Custom", "CC BY 2.x/3.0 family"),
    (r"open government licen[cs]e", "Custom", "Open Government Licence"),
    # Only explicit statements about the work itself; a site saying it "aims to publish public-domain data" is not one.
    (r"not subject to (?:domestic )?copyright|works? of the u\.?s\.? government|u\.?s\.? government works?|"
     r"\b(?:this|these|the|our) (?:data(?:set)?s?|data set|works?|materials?|information|statistics) (?:is|are) "
     r"(?:in the )?public domain",
     "Custom", "public domain statement"),
    (r"creative commons attribution(?! ?-? ?(?:non|no|share))", "Custom", "Creative Commons Attribution (version not stated)"),
]
WEAK_TERMS = r"freely available|free to use|free of charge|openly available|open access|public domain"


def detect_licence(text, hrefs=()):
    """Return {licence, label, evidence, accepted, reason} from page text and link targets."""
    corpus = " ".join([str(text or "")] + [str(h) for h in hrefs]).lower()
    corpus = re.sub(r"\s+", " ", corpus)
    for pattern, licence, label in LICENCE_RULES:
        m = re.search(pattern, corpus)
        if m:
            start, end = max(0, m.start() - 140), min(len(corpus), m.end() + 140)
            evidence = corpus[start:end].strip()
            if licence is None:
                return {"licence": None, "label": label, "evidence": evidence, "accepted": False,
                        "reason": f"reuse terms are {label}"}
            return {"licence": licence, "label": label, "evidence": evidence, "accepted": True, "reason": ""}
    m = re.search(WEAK_TERMS, corpus)
    if m:
        evidence = corpus[max(0, m.start() - 140):m.end() + 140].strip()
        return {"licence": None, "label": "weak statement only", "evidence": evidence, "accepted": False,
                "reason": "only an informal availability statement; no reuse licence found"}
    return {"licence": None, "label": "", "evidence": "", "accepted": False, "reason": "no reuse terms found"}


def licence_links(links, page_url, limit=2):
    """Same-site links that probably state reuse terms."""
    site = site_of(page_url)
    out = []
    for row in links:
        url = unwrap_url(row.get("href", ""), page_url)
        label = row.get("text", "")
        if not url or site_of(url) != site or page_key(url) == page_key(page_url):
            continue
        if re.search(r"(?i)licen[cs]e|terms of use|terms and conditions|copyright|data policy|reuse|re-use|citation|disclaimer", label + " " + urlsplit(url).path):
            if page_key(url) not in {page_key(r) for r in out}:
                out.append(url)
    return out[:limit]


def followup_links(links, page_url, limit=3):
    """Same-site links that probably lead to a download/data page (depth one only)."""
    site = site_of(page_url)
    out = []
    for row in links:
        url = unwrap_url(row.get("href", ""), page_url)
        label = re.sub(r"\s+", " ", row.get("text", "")).strip()
        if not url or site_of(url) != site or page_key(url) == page_key(page_url) or data_format(url, label):
            continue
        if re.search(r"(?i)\b(download|data access|access (?:the )?data|get (?:the )?data|data files?|datasets?|csv|tabular)\b", label):
            if page_key(url) not in {page_key(r["url"]) for r in out}:
                out.append({"url": url, "label": label[:120]})
    return out[:limit]


def data_links(links, page_url, query=""):
    """Tabular and unsupported data links with a simple usefulness score."""
    terms = topic_terms(query)
    out, seen = [], set()
    for row in links:
        url = unwrap_url(row.get("href", ""), page_url)
        label = re.sub(r"\s+", " ", row.get("text", "")).strip()[:160]
        if not url or is_datavera_url(url) or url in seen:
            continue
        fmt = data_format(url, label)
        if not fmt:
            continue
        seen.add(url)
        score = {"csv": 5, "tsv": 4, "xlsx": 3, "json": 2, "zip": 2}.get(fmt, 0)
        if fmt in SUPPORTED:
            hay = (label + " " + url).lower()
            score += sum(t in hay for t in terms)
            score += 1 if site_of(url) == site_of(page_url) else 0
            score -= 2 if re.search(r"(?i)readme|metadata|schema|dictionary|codebook|sample|example|template", hay) else 0
        out.append({"url": url, "label": label, "format": fmt, "supported": fmt in SUPPORTED,
                    "hidden": not row.get("visible", True), "score": score})
    out.sort(key=lambda r: (-r["supported"], -r["score"]))
    return out
