"""URL / Webpage Reader tool — fetch and extract text from any URL."""

from __future__ import annotations

import io
import re
from urllib.parse import urlsplit

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from row_bot.tools.base import BaseTool
from row_bot.tools import registry


# One page of a fetched page's text: fixed, not scaled to the model's context window.
PAGE_CHARS = 40_000
PAGE_CHARS_MAX = 100_000


class _ReadURLInput(BaseModel):
    url: str = Field(description="The full URL to read (must start with http:// or https://)")
    offset: int = Field(default=0, description="Character offset to start from, to read on in a long page.")
    limit: int = Field(default=PAGE_CHARS, description=f"Characters to return, at most {PAGE_CHARS_MAX}.")


_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
_PDF_MAX_BYTES = 25 * 1024 * 1024
_PDF_MAX_PAGES = 200


def _is_pdf(response, url: str, head: bytes) -> bool:
    content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
    return (content_type == "application/pdf" or head.startswith(b"%PDF-")
            or (content_type in {"", "application/octet-stream"} and urlsplit(url).path.lower().endswith(".pdf")))


def _pdf_text(data: bytes) -> str:
    """Text of a PDF's first pages; a PDF link used to come back as binary noise (B297)."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = []
    for number, page in enumerate(reader.pages[:_PDF_MAX_PAGES], start=1):
        text = (page.extract_text() or "").strip()
        if text:
            pages.append(f"[Page {number}]\n{text}")
    if len(reader.pages) > _PDF_MAX_PAGES:
        pages.append(f"[Only the first {_PDF_MAX_PAGES} of {len(reader.pages)} pages were read.]")
    return "\n\n".join(pages)


def _read_url(url: str, offset: int = 0, limit: int = PAGE_CHARS) -> str:
    """Fetch a webpage or PDF and return a page of its text content."""
    import requests
    from bs4 import BeautifulSoup

    try:
        with requests.get(url, timeout=15, stream=True, headers={"User-Agent": _USER_AGENT}) as response:
            chunks, size = [], 0
            for chunk in response.iter_content(64 * 1024):
                size += len(chunk)
                if size > _PDF_MAX_BYTES:
                    break
                chunks.append(chunk)
            data = b"".join(chunks)
            if _is_pdf(response, url, data[:5]):
                if size > _PDF_MAX_BYTES:
                    return f"The PDF is larger than {_PDF_MAX_BYTES // (1024 * 1024)} MB, too large to read."
                text = _pdf_text(data)
            else:
                # BeautifulSoup reads the page's own charset when the server names none.
                charset = response.headers.get("Content-Type", "").partition("charset=")[2].strip(" \"';") or None
                text = BeautifulSoup(data, "html.parser", from_encoding=charset).get_text()
            source = response.url or url
    except Exception as exc:
        return f"Failed to fetch URL: {exc}"

    # Collapse excessive whitespace / blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = text.strip()

    if not text:
        return "The page was fetched but no readable text content was found."

    # A long page is read a window at a time; the footer says where to continue.
    from row_bot.tool_outputs import page_chars

    limit = min(max(1_000, int(limit or PAGE_CHARS)), PAGE_CHARS_MAX)
    window, start, end = page_chars(text, offset=offset, limit=limit)
    total = len(text)
    if start == 0 and end >= total:
        return f"SOURCE_URL: {source}\n\n{text}"
    if start >= total:
        return f"SOURCE_URL: {source}\n\n[Offset {start:,} is past the end: the page has {total:,} characters.]"
    more = f"Continue with offset={end}." if end < total else "End of page."
    return f"SOURCE_URL: {source}\n\n{window}\n\n[Characters {start:,}–{end:,} of {total:,}. {more}]"


class URLReaderTool(BaseTool):

    @property
    def name(self) -> str:
        return "url_reader"

    @property
    def display_name(self) -> str:
        return "🌐 URL Reader"

    @property
    def description(self) -> str:
        return (
            "Fetch and read the text content of any webpage or URL. "
            "Use this when the user provides a link or asks about the "
            "content of a specific webpage."
        )

    @property
    def enabled_by_default(self) -> bool:
        return True

    @property
    def required_api_keys(self) -> dict[str, str]:
        return {}

    @property
    def untrusted_tool_names(self) -> set[str]:
        return {"read_url"}

    def as_langchain_tools(self) -> list:
        return [
            StructuredTool.from_function(
                func=_read_url,
                name="read_url",
                description=(
                    "Fetch a webpage or PDF and return its readable text. Use this whenever the person "
                    "shares a link or asks about a specific page. A long page comes in parts: the result "
                    "ends with the offset to continue from."
                ),
                args_schema=_ReadURLInput,
            )
        ]

    def execute(self, query: str) -> str:
        return _read_url(query)


registry.register(URLReaderTool())
