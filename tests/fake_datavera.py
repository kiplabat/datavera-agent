"""In-process stand-in for the Datavera account MCP, mirroring response shapes read from the live server on
2026-11-12 (validate: valid/errors/warnings/quality_score_preview/cover_missing; create: dataset_id/edit_url;
idempotent client_request_id). NOT the real server."""
import base64, io, json, sys, uuid
import pandas as pd
from fastmcp import FastMCP
mcp = FastMCP("fake-datavera")
UP, DS, REQ = {}, {}, {}
@mcp.tool
def whoami() -> dict:
    return {"username": "labatt", "display_name": "Test User", "key": {"kind": "api_key", "scopes": ["read", "publish"]},
            "publication_mode": "moderated", "publishes_directly": False,
            "on_submit": "sent to review; a moderator approves it on datavera.io",
            "companies": [{"slug": "datavera", "name": "Datavera.io", "role": "owner"}]}
@mcp.tool
def upload_file(filename: str, content_base64: str, sheet: str | None = None) -> dict:
    raw = base64.b64decode(content_base64); uid = "upl_" + uuid.uuid4().hex[:10]
    cols = []
    if filename.endswith(".csv"):
        cols = [{"name": c, "type": str(t)} for c, t in pd.read_csv(io.BytesIO(raw)).dtypes.items()]
    elif not raw.startswith(b"\x89PNG"):
        raise ValueError("unsupported image")
    UP[uid] = {"filename": filename, "columns": cols, "bytes": len(raw)}
    return {"upload_id": uid, "filename": filename, "columns": cols}
@mcp.tool
def inspect_upload(upload_id: str) -> dict:
    return {"upload_id": upload_id, **UP[upload_id]}
def _check(p):
    errors, warnings = [], []
    for k in ("title", "tag", "summary", "source", "source_url", "charts"):
        if not p.get(k): errors.append({"field": k, "code": "REQUIRED", "message": k + " required"})
    for i, ch in enumerate(p.get("charts", [])):
        u = UP.get(ch.get("upload_id"))
        if not u: errors.append({"field": f"charts[{i}].upload_id", "code": "UPLOAD_NOT_FOUND", "message": "no upload"}); continue
        names = {c["name"] for c in u["columns"]}
        for chan in ("x", "y", "color"):
            v = ch.get(chan); f = v if isinstance(v, str) else (v or {}).get("field")
            if f and f not in names: errors.append({"field": f"charts[{i}].{chan}", "code": "UNKNOWN_COLUMN", "message": f})
    if not p.get("cover_upload_id"): warnings.append("No cover image."); 
    elif p["cover_upload_id"] not in UP: errors.append({"field": "cover_upload_id", "code": "UPLOAD_NOT_FOUND", "message": "x"})
    if len(p.get("summary", "")) > 400: errors.append({"field": "summary", "code": "TOO_LONG", "message": "x"})
    if p.get("license") not in ("CC-BY-4.0", "CC-BY-SA-4.0", "CC0", "ODbL", "Custom"): errors.append({"field": "license", "code": "ENUM", "message": "x"})
    return {"valid": not errors, "errors": errors, "warnings": warnings, "quality_score_preview": 80, "cover_missing": not p.get("cover_upload_id")}
@mcp.tool
def validate_dataset(title: str, tag: str, summary: str, source: str, source_url: str, charts: list, subtitle: str | None = None, tags: list | None = None, key_highlights: list | None = None, analysis: str | None = None, methodology: str | None = None, citation: str | None = None, geo_scope: str | None = None, time_period: str | None = None, license: str = 'CC-BY-4.0', downloads: list | None = None, cover_upload_id: str | None = None, company: str | None = None, model: str | None = None, visibility: str = 'public', access_tier: str = 'free') -> dict:
    kw = dict(locals()); return _check(kw)
@mcp.tool
def create_dataset(title: str, tag: str, summary: str, source: str, source_url: str, charts: list, client_request_id: str | None = None, subtitle: str | None = None, tags: list | None = None, key_highlights: list | None = None, analysis: str | None = None, methodology: str | None = None, citation: str | None = None, geo_scope: str | None = None, time_period: str | None = None, license: str = 'CC-BY-4.0', downloads: list | None = None, cover_upload_id: str | None = None, company: str | None = None, model: str | None = None, visibility: str = 'public', access_tier: str = 'free') -> dict:
    kw = dict(locals())
    if client_request_id in REQ: return DS[REQ[client_request_id]]["_create"]
    v = _check(kw)
    if not v["valid"]: raise ValueError(json.dumps(v["errors"]))
    did = str(uuid.uuid4()); res = {"dataset_id": did, "edit_url": f"https://www.datavera.io/publish?edit={did}", "status": "draft"}
    DS[did] = {"dataset_id": did, "title": title, "status": "draft", "company": company, "source_url": source_url,
               "source": source, "geography": kw.get("geo_scope"), "time_period": kw.get("time_period"), "_create": res}
    if client_request_id: REQ[client_request_id] = did
    return res
@mcp.tool
def submit_dataset(id: str) -> dict:
    DS[id]["status"] = "pending_review"; return {"dataset_id": id, "status": "pending_review"}
@mcp.tool
def get_my_dataset(id: str) -> dict:
    return {k: v for k, v in DS[id].items() if not k.startswith("_")}
@mcp.tool
def list_my_datasets(page: int = 1, per_page: int = 20, status: str | None = None) -> dict:
    rows = [{k: v for k, v in d.items() if not k.startswith("_")} for d in DS.values()]
    chunk = rows[(page - 1) * per_page: page * per_page]
    return {"total": len(rows), "page": page, "per_page": per_page, "has_more": page * per_page < len(rows), "datasets": chunk}
@mcp.tool
def stats() -> dict:
    return {"datasets": len(DS), "uploads": len(UP), "requests": len(REQ)}
if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=int(sys.argv[1]), show_banner=False)
