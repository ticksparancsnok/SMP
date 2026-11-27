# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

"""Uploaded files (raw request bodies: .xlsx user imports, hosts files)."""

from fastapi import Request, status

from smp.core.errors import AppError


def _too_large(limit: int) -> AppError:
    mb = limit // (1024 * 1024)
    return AppError(status.HTTP_413_CONTENT_TOO_LARGE, "upload.too_large", f"file too large (max {mb} MB)", max_mb=mb)


async def read_limited(request: Request, limit: int) -> bytes:
    """Read the body but stop as soon as it exceeds `limit`, also for chunked uploads
    that have no Content-Length (otherwise the whole stream is buffered first)."""
    if int(request.headers.get("content-length") or 0) > limit:
        raise _too_large(limit)
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            raise _too_large(limit)
    return bytes(buf)
