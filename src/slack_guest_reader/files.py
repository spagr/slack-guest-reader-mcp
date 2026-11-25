"""Opt-in file content (ENABLE_FILE_CONTENT): images and PDFs attached to messages.

Off by default. When on, `get_file` returns the file itself to the assistant so it can
read a screenshot or a PDF. Nothing is stored or cached, nothing is logged except the
file ID, kind, size and timing. The file is located through the message it is attached
to, so the channel allowlist applies exactly as for every other tool.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from fastmcp.utilities.types import File, Image
from mcp.types import TextContent

from .slack_client import FileDownloadError

if TYPE_CHECKING:  # pragma: no cover
    from .tools import Services

logger = logging.getLogger(__name__)

FileKind = Literal["image", "pdf"]

IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
IMAGE_FILETYPES = frozenset({"png", "jpg", "jpeg", "gif", "webp"})
# Claude accepts images up to 5 MB after base64 encoding; keep originals well below and
# fall back to Slack's own thumbnails for anything bigger.
MAX_IMAGE_BYTES = 3_750_000
THUMBNAIL_KEYS = ("thumb_1024", "thumb_960", "thumb_720", "thumb_480", "thumb_360")


def is_remote(file: Mapping[str, Any]) -> bool:
    """Remote files point at a URL an app chose; it must never get the bot token."""
    return bool(
        file.get("mode") == "external"
        or file.get("is_external")
        or file.get("filetype") == "remote"
    )


def file_kind(file: Mapping[str, Any]) -> FileKind | None:
    """'image' or 'pdf' when get_file can return this file, else None."""
    if is_remote(file) or file.get("mode") in {"tombstone", "hidden_by_limit"}:
        return None
    mimetype = str(file.get("mimetype") or "").lower()
    filetype = str(file.get("filetype") or "").lower()
    if mimetype in IMAGE_MIME_TYPES or filetype in IMAGE_FILETYPES:
        return "image"
    if mimetype == "application/pdf" or filetype == "pdf":
        return "pdf"
    return None


def pick_source(file: Mapping[str, Any], kind: FileKind, max_bytes: int) -> tuple[str, str]:
    """(url, variant) to download, or ToolError when nothing fits the size limit."""
    size = int(file.get("size") or 0)
    original = str(file.get("url_private") or "")
    if kind == "image":
        if original and size <= min(max_bytes, MAX_IMAGE_BYTES):
            return original, "original"
        for key in THUMBNAIL_KEYS:
            if file.get(key):
                return str(file[key]), key
        raise ToolError("The image is too large and Slack offers no thumbnail; use its permalink.")
    if original and size <= max_bytes:
        return original, "original"
    raise ToolError(
        f"The PDF is larger than this server's limit ({max_bytes // 1_000_000} MB); "
        "use its permalink."
    )


def _accepts(kind: FileKind) -> Any:
    if kind == "image":
        return lambda content_type: content_type in IMAGE_MIME_TYPES
    return lambda content_type: content_type == "application/pdf"


DOWNLOAD_ERRORS = {
    "unexpected_content_type": (
        "Slack did not return the file. The Slack app most likely lacks the files:read "
        "scope (the workspace admin adds it and reinstalls the app), or the file is no "
        "longer available. Use the permalink meanwhile."
    ),
    "too_large": "The file is larger than this server's limit; use its permalink.",
    "untrusted_host": "The file is not hosted by Slack, so it is not fetched; use its permalink.",
    "http_403": "Slack refused the download (files:read scope missing?); use its permalink.",
    "http_404": "Slack no longer has this file; use its permalink.",
}


async def read_file(services: Services, file: Mapping[str, Any]) -> ToolResult:
    """Download one attached file and return it as MCP image or embedded-resource content."""
    kind = file_kind(file)
    if kind is None:
        raise ToolError(
            "Only images (PNG, JPEG, GIF, WebP) and PDFs can be read; use the permalink "
            "for other files."
        )
    url, variant = pick_source(file, kind, services.settings.max_file_bytes)
    started = time.monotonic()
    try:
        data, content_type = await services.slack.download(
            url, max_bytes=services.settings.max_file_bytes, accept=_accepts(kind)
        )
    except FileDownloadError as exc:
        logger.warning("file read failed id=%s kind=%s error=%s", file.get("id"), kind, exc.code)
        raise ToolError(
            DOWNLOAD_ERRORS.get(exc.code, f"Could not download the file ({exc.code}).")
        ) from None
    logger.info(
        "file read id=%s kind=%s variant=%s bytes=%d ms=%d",
        file.get("id"),
        kind,
        variant,
        len(data),
        int((time.monotonic() - started) * 1000),
    )
    meta = {
        "id": file.get("id"),
        "name": file.get("name") or file.get("title"),
        "kind": kind,
        "variant": variant,
        "bytes": len(data),
        "permalink": file.get("permalink"),
    }
    if kind == "image":
        content: Any = Image(data=data).to_image_content(mime_type=content_type)
    else:
        name = str(file.get("name") or f"{file.get('id')}.pdf")
        content = File(data=data, format="pdf", name=name).to_resource_content()
    return ToolResult(content=[TextContent(type="text", text=json.dumps(meta)), content])
