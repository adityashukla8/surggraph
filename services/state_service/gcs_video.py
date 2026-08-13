"""Streams the prepared demo video files straight from Cloud Storage — no
local-disk dependency for the running service. `scripts/prepare_demo_videos.py`
uploads its output to the same GCS bucket already used for the raw source
video (tools/video_utils.py's `find_video_gcs_uri`, same `videos/{video_id}/`
prefix convention), so this route works identically whether the service is
running on a laptop or a fresh, stateless Cloud Run instance that never
touched `data/video/` at all.

Range requests are forwarded as real GCS byte-range reads (`Blob.download_as_bytes(start=, end=)`,
a partial GET against GCS, not "download the whole object then slice it in
memory") — this is what lets the browser's <video> tag seek/scrub without
pulling the full ~200MB file first.
"""

from __future__ import annotations

import asyncio
import os
import re

from fastapi import HTTPException, Request
from fastapi.responses import Response
from google.cloud import storage as gcs_storage
from google.cloud.exceptions import NotFound

from tools.video_utils import video_mime_type

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")

# Real bug this guards against: an open-ended Range request (`bytes=0-`,
# with no end — exactly what Chrome/Firefox send to start playback) used
# to default `end` to the object's last byte, so `download_as_bytes`
# synchronously pulled the ENTIRE ~200MB file from GCS into memory before
# a single byte reached the browser (confirmed: video stuck on its loading
# spinner indefinitely, zero further log lines while the fetch hung).
# Capping every response to one bounded chunk — regardless of what the
# client's range asked for — is standard range-server behavior; the
# browser sees a `Content-Range` with more bytes remaining and simply
# issues further range requests as it needs them.
_MAX_CHUNK_BYTES = 8 * 1024 * 1024

_client: gcs_storage.Client | None = None
_size_cache: dict[str, int] = {}


def _bucket_name() -> str | None:
    return os.environ.get("SURGGRAPH_GCS_BUCKET")


def _get_client() -> gcs_storage.Client:
    global _client
    if _client is None:
        _client = gcs_storage.Client()
    return _client


def _blob_size(object_path: str) -> int:
    if object_path in _size_cache:
        return _size_cache[object_path]
    bucket_name = _bucket_name()
    blob = _get_client().bucket(bucket_name).blob(object_path)
    if not blob.exists():
        raise NotFound(object_path)
    blob.reload()
    _size_cache[object_path] = blob.size
    return blob.size


def _download_range(bucket_name: str, object_path: str, start: int, end: int) -> bytes:
    blob = _get_client().bucket(bucket_name).blob(object_path)
    return blob.download_as_bytes(start=start, end=end)


async def stream_video(video_id: str, filename: str, request: Request) -> Response:
    bucket_name = _bucket_name()
    if not bucket_name:
        raise HTTPException(status_code=503, detail="SURGGRAPH_GCS_BUCKET not configured")

    object_path = f"videos/{video_id}/{filename}"
    try:
        # google-cloud-storage's HTTP calls are blocking (requests-based,
        # not asyncio) — running them directly in this async route would
        # stall the event loop for every byte-range fetch, including other
        # clients' concurrent SSE streams. asyncio.to_thread offloads the
        # real network I/O to a worker thread instead.
        size = await asyncio.to_thread(_blob_size, object_path)
    except NotFound:
        raise HTTPException(status_code=404, detail=f"gs://{bucket_name}/{object_path} not found") from None

    media_type = video_mime_type(filename)
    range_header = request.headers.get("range")

    if range_header:
        match = _RANGE_RE.match(range_header)
        if not match:
            raise HTTPException(status_code=416, detail=f"malformed Range header: {range_header!r}")
        start = int(match.group(1)) if match.group(1) else 0
        requested_end = int(match.group(2)) if match.group(2) else size - 1
        end = min(requested_end, size - 1, start + _MAX_CHUNK_BYTES - 1)
        if start > end or start >= size:
            raise HTTPException(status_code=416, detail=f"Range not satisfiable for {size}-byte object")
    else:
        # No Range header at all — still cap the chunk (see _MAX_CHUNK_BYTES
        # above); <video> tags always send Range, so this path is mainly a
        # plain GET/curl, and a 206 with more available is a valid response
        # to those too (Accept-Ranges tells the client it can ask for more).
        start, end = 0, min(size - 1, _MAX_CHUNK_BYTES - 1)

    status_code = 206
    headers = {"Content-Range": f"bytes {start}-{end}/{size}"}

    chunk = await asyncio.to_thread(_download_range, bucket_name, object_path, start, end)
    headers.update({"Accept-Ranges": "bytes", "Content-Length": str(len(chunk))})
    return Response(content=chunk, status_code=status_code, media_type=media_type, headers=headers)
