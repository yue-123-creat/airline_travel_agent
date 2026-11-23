"""Lightweight Markdown → plain text (no extra deps; mini-program friendly)."""

from __future__ import annotations

import re
from collections.abc import Iterator

_CODE_FENCE = re.compile(r"```[\w+-]*\n?([\s\S]*?)```", re.MULTILINE)
_INLINE_CODE = re.compile(r"`([^`]+)`")
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_LINK = re.compile(r"(?<!!)\[([^\]]+)\]\([^)]+\)")
_HEADING = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_BOLD_ITALIC = re.compile(r"(\*\*\*|___|\*\*|__|\*|_)(.+?)\1")
_STRIKE = re.compile(r"~~(.+?)~~")
_BLOCKQUOTE = re.compile(r"^>\s?", re.MULTILINE)
_HR = re.compile(r"^(\s*[-*_]\s*){3,}$", re.MULTILINE)
_UL = re.compile(r"^(\s*)([-*+])\s+", re.MULTILINE)
_OL = re.compile(r"^(\s*)\d+\.\s+", re.MULTILINE)
_MULTI_NL = re.compile(r"\n{3,}")


def extract_markdown_images(text: str) -> list[dict[str, str]]:
    """抽取正文 ![alt](url)，供 images 字段对齐。"""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for m in _IMAGE.finditer(text or ""):
        alt = (m.group(1) or "").strip()
        url = (m.group(2) or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({"name": alt or "景点", "url": url, "role": "inline_md", "placement": "inline"})
    return out


def strip_markdown_images(text: str) -> str:
    """去掉 ![alt](url)，用于本轮不出图时清洗正文。"""
    s = _IMAGE.sub("", text or "")
    s = _MULTI_NL.sub("\n\n", s)
    return s.strip()


def markdown_to_plain(text: str) -> str:
    """Markdown → 可读纯文本；保留 ![alt](url) 以便图文混排。"""
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not raw.strip():
        return ""

    def _fence(m: re.Match[str]) -> str:
        body = (m.group(1) or "").strip("\n")
        return f"\n{body}\n"

    s = _CODE_FENCE.sub(_fence, raw)

    # 先抽出图片 MD，避免斜体 `_…_` / `__…__` 误伤 URL 中的下划线
    img_stash: list[str] = []

    def _stash_img(m: re.Match[str]) -> str:
        img_stash.append(m.group(0))
        return f"\x00IMG{len(img_stash) - 1}\x00"

    s = _IMAGE.sub(_stash_img, s)

    s = _LINK.sub(lambda m: (m.group(1) or "").strip(), s)
    s = _INLINE_CODE.sub(r"\1", s)
    s = _HEADING.sub("", s)
    s = _BLOCKQUOTE.sub("", s)
    s = _HR.sub("", s)
    s = _STRIKE.sub(r"\1", s)
    for _ in range(3):
        s2 = _BOLD_ITALIC.sub(r"\2", s)
        if s2 == s:
            break
        s = s2
    s = _UL.sub(r"\1", s)
    s = _OL.sub(r"\1", s)
    s = s.replace("**", "").replace("__", "")
    s = _MULTI_NL.sub("\n\n", s)

    for i, raw_img in enumerate(img_stash):
        s = s.replace(f"\x00IMG{i}\x00", raw_img)
    return s.strip()


def iter_plain_chunks(text: str, *, size: int = 24) -> Iterator[str]:
    """按字符块切分，供 SSE 流式推送（体感更快）。"""
    body = text or ""
    n = max(8, int(size))
    i = 0
    while i < len(body):
        yield body[i : i + n]
        i += n
