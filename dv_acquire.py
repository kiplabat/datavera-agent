"""Download one data file, parse it into a table, clean it and profile its columns. No model."""
import csv
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

import httpx
import pandas as pd

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DataveraLocalAgent/1.0 (+https://www.datavera.io)"


class AcquireError(RuntimeError):
    pass


async def download(url, folder, max_bytes=25_000_000, timeout=90):
    """Stream to disk with a hard size cap; reject HTML pages masquerading as data."""
    folder.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    chunks, size = [], 0
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout,
                                 headers={"User-Agent": USER_AGENT, "Accept": "*/*"}) as http:
        async with http.stream("GET", url) as response:
            if response.status_code != 200:
                raise AcquireError(f"HTTP {response.status_code} downloading the data file")
            ctype = response.headers.get("content-type", "").lower()
            declared = int(response.headers.get("content-length") or 0)
            if declared > max_bytes:
                raise AcquireError(f"file is {declared/1e6:.1f} MB; limit {max_bytes/1e6:.0f} MB")
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise AcquireError(f"file exceeds {max_bytes/1e6:.0f} MB")
                digest.update(chunk)
                chunks.append(chunk)
            final_url = str(response.url)
    body = b"".join(chunks)
    head = body[:600].lstrip().lower()
    if "text/html" in ctype or head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        raise AcquireError("the link returned an HTML page, not a data file")
    sha = digest.hexdigest()
    path = folder / f"source_{sha[:16]}.bin"
    path.write_bytes(body)
    return {"path": str(path), "bytes": size, "sha256": sha, "content_type": ctype, "final_url": final_url}


def unpack_zip(path, folder, max_member_bytes=60_000_000):
    """Pick the largest CSV/TSV in an archive (skipping metadata files) and extract it with a size cap.
    Returns (member_path, format, member_names, citation_from_metadata_or_None)."""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise AcquireError("the ZIP archive is damaged or not a ZIP") from exc
    with zf:
        names = [i.filename for i in zf.infolist() if not i.is_dir()]
        tables = [i for i in zf.infolist() if not i.is_dir() and re.search(r"(?i)\.(csv|tsv)$", i.filename)
                  and not re.search(r"(?i)metadata|readme|dictionary|codebook|schema", Path(i.filename).name)]
        if not tables:
            raise AcquireError(f"the archive has no CSV/TSV table (contains: {names[:6]})")
        best = max(tables, key=lambda i: i.file_size)
        if best.file_size > max_member_bytes:
            raise AcquireError(f"{best.filename} is {best.file_size/1e6:.0f} MB uncompressed; limit {max_member_bytes/1e6:.0f} MB")
        out = Path(folder) / ("unzipped_" + re.sub(r"[^\w.-]", "_", Path(best.filename).name))
        size = 0
        with zf.open(best) as src, out.open("wb") as dst:
            while chunk := src.read(1 << 20):
                size += len(chunk)
                if size > max_member_bytes:  # do not trust the declared size
                    raise AcquireError("archive member exceeds the size limit")
                dst.write(chunk)
        citation = None
        for info in zf.infolist():
            if re.search(r"(?i)metadata.*\.json$|\.json$", info.filename) and info.file_size < 2_000_000:
                try:
                    citation = _find_citation(json.loads(zf.read(info).decode("utf-8", "replace")))
                except (ValueError, UnicodeError):
                    citation = None
                if citation:
                    break
    fmt = "tsv" if best.filename.lower().endswith(".tsv") else "csv"
    return str(out), fmt, names, citation


def _find_citation(value):
    """First long citation string in a metadata document (e.g. Our World in Data's citationLong)."""
    if isinstance(value, dict):
        for key in ("citationLong", "citation_full", "citation", "citationShort"):
            v = value.get(key)
            if isinstance(v, str) and len(v) > 15:
                return v.strip()[:500]
        for v in value.values():
            found = _find_citation(v)
            if found:
                return found
    elif isinstance(value, list):
        for v in value:
            found = _find_citation(v)
            if found:
                return found
    return None


def _decode(raw):
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    raise AcquireError("could not decode text file")


def parse(path, fmt):
    raw = Path(path).read_bytes()
    notes = []
    if fmt in ("csv", "tsv"):
        text, enc = _decode(raw)
        lines = text.splitlines()
        skip = 0
        while skip < len(lines) and (lines[skip].startswith("#") or not lines[skip].strip()):
            skip += 1
        if skip:
            notes.append(f"skipped {skip} leading comment/blank lines")
        sample = "\n".join(lines[skip:skip + 50])
        try:
            sep = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            sep = "\t" if fmt == "tsv" else ","
        try:
            df = pd.read_csv(io.StringIO("\n".join(lines[skip:])), sep=sep, low_memory=False, on_bad_lines="skip")
        except (pd.errors.ParserError, ValueError) as exc:
            raise AcquireError(f"CSV could not be parsed: {str(exc)[:160]}") from exc
        notes.append(f"decoded as {enc}; delimiter {sep!r}")
    elif fmt == "xlsx":
        try:
            import openpyxl  # noqa: F401
        except ImportError as exc:
            raise AcquireError("XLSX needs openpyxl: .venv\\Scripts\\python.exe -m pip install openpyxl") from exc
        df = pd.read_excel(io.BytesIO(raw), sheet_name=0)
        notes.append("first worksheet read")
    elif fmt == "json":
        text, _ = _decode(raw)
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AcquireError("JSON could not be parsed") from exc
        rows = None
        if isinstance(value, list) and value and isinstance(value[0], dict):
            rows = value
        elif isinstance(value, dict):
            lists = [v for v in value.values() if isinstance(v, list) and v and isinstance(v[0], dict)]
            rows = max(lists, key=len) if lists else None
        if rows is None:
            raise AcquireError("JSON is not a list of records")
        df = pd.json_normalize(rows, max_level=1)
        notes.append("JSON records flattened")
    else:
        raise AcquireError(f"unsupported format {fmt}")
    return clean(df, notes)


def _numeric(series):
    if pd.api.types.is_numeric_dtype(series):
        return series
    text = series.astype("string").str.strip()
    text = text.str.replace(r"(?<=\d)[,\s](?=\d{3}\b)", "", regex=True).str.replace("%", "", regex=False)
    text = text.replace({"": None, "-": None, "..": None, "NA": None, "N/A": None, "n/a": None, "null": None})
    return pd.to_numeric(text, errors="coerce")


def clean(df, notes):
    df = df.copy()
    df.columns = [re.sub(r"\s+", " ", str(c)).strip() or f"column_{i+1}" for i, c in enumerate(df.columns)]
    if sum(c.lower().startswith("unnamed") for c in df.columns) > len(df.columns) / 2:
        raise AcquireError("the table has no usable header row")
    seen = {}
    names = []
    for c in df.columns:
        seen[c] = seen.get(c, 0) + 1
        names.append(c if seen[c] == 1 else f"{c}_{seen[c]}")
    df.columns = names
    before = df.shape
    df = df.dropna(axis=1, how="all").dropna(axis=0, how="all")
    if df.shape != before:
        notes.append(f"dropped empty rows/columns: {before} -> {df.shape}")
    for col in df.columns:
        if df[col].dtype == object or pd.api.types.is_string_dtype(df[col]):
            converted = _numeric(df[col])
            present = df[col].notna().sum()
            if present and converted.notna().sum() >= 0.9 * present:
                df[col] = converted
    return df, notes


def is_year_series(s):
    num = pd.to_numeric(s, errors="coerce").dropna()
    return len(num) > 0 and (num == num.round()).all() and num.between(1500, 2100).all()


def profile(df, max_columns=60):
    cols = []
    for col in list(df.columns)[:max_columns]:
        s = df[col]
        non_null = int(s.notna().sum())
        kind = "text"
        info = {}
        if pd.api.types.is_numeric_dtype(s):
            kind = "year" if is_year_series(s) and s.nunique() > 1 else "number"
            if non_null:
                info = {"min": float(s.min()), "max": float(s.max())}
        else:
            parsed = pd.to_datetime(s.dropna().astype(str).head(200), errors="coerce", format="mixed")
            if len(parsed) and parsed.notna().mean() > 0.9 and re.search(r"\d{4}", str(s.dropna().iloc[0])):
                kind = "date"
        examples = [str(v)[:40] for v in s.dropna().unique()[:3]]
        cols.append({"name": col, "kind": kind, "non_null": non_null, "distinct": int(s.nunique()),
                     "examples": examples, **info})
    return {"rows": int(len(df)), "columns": cols}
