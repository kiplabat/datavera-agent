"""File tools, limited to the folders granted to the chat or project (plus the chat's own workspace)."""
import asyncio
import fnmatch
import os
import shutil
from pathlib import Path

from . import Tool, ToolError

TEXT_LIMIT = 60_000
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".next", "dist", "build"}


def _read_text(path, offset=0, limit=400):
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        import pandas as pd
        sheets = pd.read_excel(path, sheet_name=None, nrows=200)
        return "\n\n".join(f"## Sheet {name} (first {len(df)} rows)\n{df.to_csv(index=False)}" for name, df in sheets.items())
    if suffix == ".docx":
        import zipfile
        import re
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
        return re.sub(r"<[^>]+>", " ", xml.replace("</w:p>", "\n"))
    if suffix == ".pdf":
        raise ToolError("PDF text extraction is not built in; use run_python with a PDF library if installed.")
    raw = path.read_bytes()
    if b"\x00" in raw[:4096]:
        raise ToolError(f"{path.name} looks like a binary file ({len(raw)} bytes)")
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    chunk = lines[offset:offset + limit]
    more = len(lines) - offset - len(chunk)
    body = "\n".join(f"{i + offset + 1:>5}  {line}" for i, line in enumerate(chunk))
    if len(body) > TEXT_LIMIT:
        body = body[:TEXT_LIMIT] + "\n…(truncated)"
    if more > 0:
        body += f"\n…{more} more lines (use offset={offset + len(chunk)})"
    return body


async def read_file(ctx, path, offset=0, limit=400):
    p = ctx.resolve(path)
    if not p.is_file():
        raise ToolError(f"No file at {p}")
    return await asyncio.to_thread(_read_text, p, int(offset or 0), min(int(limit or 400), 2000))


async def list_files(ctx, path=".", pattern="*", recursive=False):
    p = ctx.resolve(path)
    if not p.is_dir():
        raise ToolError(f"No folder at {p}")

    def walk():
        rows = []
        it = p.rglob(pattern) if recursive else p.glob(pattern)
        for item in it:
            if any(part in SKIP_DIRS for part in item.relative_to(p).parts):
                continue
            try:
                size = item.stat().st_size if item.is_file() else None
            except OSError:
                continue
            rows.append(f"{'[dir] ' if item.is_dir() else ''}{item.relative_to(p)}" + (f"  ({size:,} B)" if size is not None else ""))
            if len(rows) >= 300:
                rows.append("…(more; narrow the pattern)")
                break
        return "\n".join(sorted(rows)) or "(empty)"
    return f"{p}\n" + await asyncio.to_thread(walk)


async def search_files(ctx, query, path=".", pattern="*"):
    root = ctx.resolve(path)
    needle = query.lower()

    def scan():
        hits = []
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                if not fnmatch.fnmatch(name, pattern):
                    continue
                fp = Path(dirpath) / name
                try:
                    if fp.stat().st_size > 5_000_000:
                        continue
                    with fp.open("r", encoding="utf-8", errors="ignore") as f:
                        for n, line in enumerate(f, 1):
                            if needle in line.lower():
                                hits.append(f"{fp.relative_to(root)}:{n}: {line.strip()[:200]}")
                                if len(hits) >= 80:
                                    return hits
                except OSError:
                    continue
        return hits
    hits = await asyncio.to_thread(scan)
    return "\n".join(hits) if hits else f"No matches for {query!r}"


async def write_file(ctx, path, content):
    p = ctx.resolve(path, write=True)
    if p.exists():
        backup = ctx.workspace / ".backups" / p.name
        backup.parent.mkdir(exist_ok=True)
        shutil.copy2(p, backup)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    ctx.emit("file_changed", path=str(p), action="written")
    return f"Wrote {len(content):,} characters to {p}"


async def edit_file(ctx, path, old_text, new_text):
    p = ctx.resolve(path, write=True)
    text = p.read_text(encoding="utf-8")
    count = text.count(old_text)
    if count != 1:
        raise ToolError(f"old_text must appear exactly once in {p.name} (found {count}). Include more context.")
    backup = ctx.workspace / ".backups" / p.name
    backup.parent.mkdir(exist_ok=True)
    shutil.copy2(p, backup)
    p.write_text(text.replace(old_text, new_text), encoding="utf-8")
    ctx.emit("file_changed", path=str(p), action="edited")
    return f"Edited {p}"


async def make_folder(ctx, path):
    p = ctx.resolve(path, write=True)
    p.mkdir(parents=True, exist_ok=True)
    return f"Folder ready: {p}"


async def move_file(ctx, source, destination):
    src, dst = ctx.resolve(source, write=True), ctx.resolve(destination, write=True)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    ctx.emit("file_changed", path=str(dst), action="moved")
    return f"Moved {src} → {dst}"


async def delete_file(ctx, path):
    """Deletes are reversible: files go to the workspace's .trash folder."""
    p = ctx.resolve(path, write=True)
    if p in ctx.roots:
        raise ToolError("Refusing to delete a whole granted folder")
    trash = ctx.workspace / ".trash"
    trash.mkdir(exist_ok=True)
    shutil.move(str(p), str(trash / p.name))
    ctx.emit("file_changed", path=str(p), action="deleted")
    return f"Moved {p} to the chat's trash ({trash})"


S = lambda props, req: {"type": "object", "properties": props, "required": req}  # noqa: E731
PATH = {"type": "string", "description": "File or folder path, relative to the chat's main folder or absolute"}

TOOLS = [
    Tool("read_file", "Read a text file (also .xlsx sheets and .docx text). Lines are numbered.",
         S({"path": PATH, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, ["path"]),
         read_file, "files", label="Read file", summary=lambda a: f"Read {a.get('path')}"),
    Tool("list_files", "List files in a folder. Use pattern like '*.csv' and recursive=true to search deeper.",
         S({"path": PATH, "pattern": {"type": "string"}, "recursive": {"type": "boolean"}}, []),
         list_files, "files", label="List folder", summary=lambda a: f"List {a.get('path') or '.'}"),
    Tool("search_files", "Find lines containing text in files under a folder.",
         S({"query": {"type": "string"}, "path": PATH, "pattern": {"type": "string"}}, ["query"]),
         search_files, "files", label="Search files", summary=lambda a: f"Search for {a.get('query')!r}"),
    Tool("write_file", "Create or overwrite a text file with the full content.",
         S({"path": PATH, "content": {"type": "string"}}, ["path", "content"]),
         write_file, "files", changes=True, label="Write file",
         summary=lambda a: f"Write {a.get('path')} ({len(a.get('content') or ''):,} characters)"),
    Tool("edit_file", "Replace one exact piece of text in a file.",
         S({"path": PATH, "old_text": {"type": "string"}, "new_text": {"type": "string"}},
           ["path", "old_text", "new_text"]),
         edit_file, "files", changes=True, label="Edit file", summary=lambda a: f"Edit {a.get('path')}"),
    Tool("make_folder", "Create a folder.", S({"path": PATH}, ["path"]), make_folder, "files", changes=True,
         label="Create folder", summary=lambda a: f"Create folder {a.get('path')}"),
    Tool("move_file", "Move or rename a file or folder.",
         S({"source": PATH, "destination": PATH}, ["source", "destination"]), move_file, "files", changes=True,
         label="Move", summary=lambda a: f"Move {a.get('source')} → {a.get('destination')}"),
    Tool("delete_file", "Delete a file or folder (it goes to the chat's trash, so it can be recovered).",
         S({"path": PATH}, ["path"]), delete_file, "files", changes=True, label="Delete",
         summary=lambda a: f"Delete {a.get('path')}"),
]
