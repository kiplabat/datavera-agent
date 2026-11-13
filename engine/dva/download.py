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


async def download(url, dest, *, headers=None, expected_size=None, sha256=None, progress=None, cancel=None):
    """Download url to dest (via dest.part, resuming it). progress(done, total, rate) is called ~2x/second."""
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
    timeout = httpx.Timeout(30, read=120)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http:
        async with http.stream("GET", url, headers=req_headers) as response:
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
                with part.open(mode) as f:
                    async for chunk in response.aiter_bytes(1 << 20):
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
                if progress:
                    progress(done, total, rate)
    if expected_size and part.stat().st_size != expected_size:
        raise ValueError(f"Downloaded size {part.stat().st_size} does not match expected {expected_size}")
    if digest and digest.hexdigest().lower() != sha256.lower():
        part.unlink(missing_ok=True)
        raise ValueError("The downloaded file's SHA-256 does not match the publisher's; it was deleted. Try again.")
    os.replace(part, dest)
    return dest
