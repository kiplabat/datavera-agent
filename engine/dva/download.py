"""Resumable HTTP downloads with progress, cancellation and optional SHA-256 check."""
import asyncio
import hashlib
import os
import time
from pathlib import Path

import httpx

USER_AGENT = "DataveraAgent/2 (+https://www.datavera.io/publish/agent)"


class DownloadCancelled(Exception):
    pass


async def fetch_json(url, headers=None, timeout=30):
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                 headers={"User-Agent": USER_AGENT, **(headers or {})}) as http:
        response = await http.get(url)
        response.raise_for_status()
        return response.json()


class DownloadStalled(Exception):
    pass


def describe_error(exc):
    """A sentence a person can act on."""
    if isinstance(exc, DownloadStalled):
        return str(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return (f"The server refused the download (HTTP {code}). The model may be gated: accept its licence on "
                    "huggingface.co and add a Hugging Face token in Settings.")
        if code == 404:
            return "The file is no longer on the server (HTTP 404). The publisher may have renamed it; search again."
        if code == 429:
            return "Hugging Face is rate-limiting downloads (HTTP 429). It will retry; a token in Settings helps."
        return f"The server answered HTTP {code}."
    if isinstance(exc, httpx.ConnectTimeout):
        return "Could not connect to the download server (timed out). Check the internet connection or a firewall/VPN."
    if isinstance(exc, httpx.ConnectError):
        return f"Could not connect to the download server ({str(exc)[:120] or 'connection refused'})."
    if isinstance(exc, httpx.ReadTimeout):
        return "The download server stopped sending data."
    if isinstance(exc, (httpx.RemoteProtocolError, httpx.ReadError)):
        return "The connection was dropped by the server or the network."
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return "The disk is full."
    return f"{type(exc).__name__}: {str(exc)[:200]}"


async def download(url, dest, *, headers=None, expected_size=None, sha256=None, progress=None, cancel=None,
                   on_phase=None, stall_seconds=45):
    """Download url to dest (via dest.part, resuming it). progress(done, total, rate) is called ~2x/second;
    on_phase(phase, detail) reports connecting / downloading. Raises DownloadStalled if no data arrives for
    stall_seconds (the caller retries; the .part file is kept and resumed)."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    digest = hashlib.sha256() if sha256 else None
    if digest and have:
        with part.open("rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                digest.update(block)
    req_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    if have:
        req_headers["Range"] = f"bytes={have}-"
    timeout = httpx.Timeout(30, connect=20, read=stall_seconds)
    if on_phase:
        on_phase("connecting", f"resuming at {have / 2**20:,.0f} MB" if have else "")
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http:
        async with http.stream("GET", url, headers=req_headers) as response:
            if on_phase:
                host = response.url.host or ""
                on_phase("downloading", f"from {host}" if host else "")
            if response.status_code == 416 and expected_size and have >= expected_size:
                pass  # already complete
            elif response.status_code not in (200, 206):
                raise httpx.HTTPStatusError(f"HTTP {response.status_code} for download", request=response.request,
                                            response=response)
            else:
                if response.status_code == 200 and have:
                    have = 0  # server ignored Range: start again
                    digest = hashlib.sha256() if sha256 else None
                length = response.headers.get("content-length")
                total = expected_size or ((int(length) + have) if length else None)
                mode = "ab" if have else "wb"
                done, last, last_done, rate = have, 0.0, have, 0.0
                if progress:
                    progress(have, total, 0.0)
                with part.open(mode) as f:
                    try:
                        chunks = response.aiter_bytes(1 << 18)
                        async for chunk in chunks:
                            if cancel and cancel.is_set():
                                raise DownloadCancelled()
                            f.write(chunk)
                            if digest:
                                digest.update(chunk)
                            done += len(chunk)
                            now = time.monotonic()
                            if now - last >= 0.5:
                                if last:
                                    rate = (done - last_done) / (now - last)
                                last, last_done = now, done
                                if progress:
                                    progress(done, total, rate)
                                await asyncio.sleep(0)
                    except httpx.ReadTimeout as exc:
                        raise DownloadStalled(f"No data arrived for {stall_seconds} seconds") from exc
                if progress:
                    progress(done, total, rate)
    if expected_size and part.stat().st_size != expected_size:
        raise ValueError(f"Downloaded size {part.stat().st_size} does not match expected {expected_size}")
    if digest and digest.hexdigest().lower() != sha256.lower():
        part.unlink(missing_ok=True)
        raise ValueError("The downloaded file's SHA-256 does not match the publisher's; it was deleted. Try again.")
    os.replace(part, dest)
    return dest
