from __future__ import annotations

import re

from flask import url_for

_INVALID_SLUG_RE = re.compile(r"[^0-9A-Za-z\u0E00-\u0E7F\s-]+")
_MULTI_SPACE_RE = re.compile(r"\s+")
_MULTI_DASH_RE = re.compile(r"[-\s]+")


def slugify_title(title: str, max_len: int = 120) -> str:
    text = (title or "").strip().lower()
    if not text:
        return "novel"

    text = text.replace("/", " ").replace("\\", " ")
    text = _MULTI_SPACE_RE.sub(" ", text)
    text = _INVALID_SLUG_RE.sub("", text)
    text = text.replace("_", " ")
    text = _MULTI_DASH_RE.sub("-", text).strip("-")

    if max_len and len(text) > max_len:
        text = text[:max_len].rstrip("-")

    return text or "novel"


def novel_detail_url(novels_id: int, title: str | None = None) -> str:
    slug = slugify_title(title or "")
    try:
        return url_for("novel.detail", novels_id=novels_id, slug=slug)
    except Exception:
        return f"/novel/{novels_id}"
