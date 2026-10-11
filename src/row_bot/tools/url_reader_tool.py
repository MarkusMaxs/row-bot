"""URL / Webpage Reader tool — fetch and extract text from any URL."""

from __future__ import annotations

import io
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urljoin, urlsplit

import httpx
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from row_bot.integrations.safe import _Pinned, _public_addresses
from row_bot.tools.base import BaseTool
from row_bot.tools import registry


class _ReadURLInput(BaseModel):
    url: str = Field(description="The full URL to read (must start with http:// or https://)")


_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
_PDF_MAX_BYTES = 25 * 1024 * 1024
_PDF_MAX_PAGES = 200
_TIMEOUT = 15
_MAX_REDIRECTS = 10
_REDIRECTS = {301, 302, 303, 307, 308}
_NOT_PUBLIC = "not_public"
NOT_PUBLIC_MESSAGE = (
    "This link can't be read: read_url opens public web pages only, never an address on this computer, "
    "the local network or a cloud metadata service (this includes links that redirect there)."
)


class _NotPublic(Exception):
    pass


@contextmanager
def _public_get(url: str) -> Iterator[tuple[str, httpx.Response]]:
    """GET a public web page, yielding the final URL and its response.

    A page the agent reads can tell it what to read next, so each hop (the link and every redirect) is
    checked on its own: http(s) without a user name or password, a name that resolves only to public
    addresses, and a connection to one of those checked addresses with no second lookup, so a rebinding name
    can't swap in a private one.
    """
    deadline = time.monotonic() + _TIMEOUT * 4
    for _hop in range(_MAX_REDIRECTS + 1):
        try:
            target = httpx.URL(url)
        except (httpx.InvalidURL, TypeError) as exc:
            raise ValueError(f"not a valid link: {exc}") from None
        if target.scheme not in {"http", "https"} or not target.host or target.userinfo:
            raise ValueError("only http:// and https:// links without a user name or password can be read")
        try:
            addresses = _public_addresses(target.raw_host.decode("ascii").rstrip("."), _NOT_PUBLIC)
        except ValueError as exc:
            raise _NotPublic() from exc
        except ConnectionError:
            raise ConnectionError(f"could not resolve {target.host}") from None
        transport = _Pinned(target.host, addresses, deadline)
        with httpx.Client(transport=transport, timeout=_TIMEOUT, follow_redirects=False, trust_env=False) as client:
            with client.stream("GET", target, headers={"User-Agent": _USER_AGENT}) as response:
                location = response.headers.get("Location", "")
                if response.status_code in _REDIRECTS and location:
                    url = urljoin(str(target), location)
                    continue
                yield str(target), response
                return
    raise ValueError(f"the link redirected more than {_MAX_REDIRECTS} times")


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


def _read_url(url: str) -> str:
    """Fetch a webpage or PDF and return its text content."""
    from bs4 import BeautifulSoup

    try:
        with _public_get(str(url or "").strip()) as (source, response):
            chunks, size = [], 0
            for chunk in response.iter_bytes(64 * 1024):
                size += len(chunk)
                if size > _PDF_MAX_BYTES:
                    break
                chunks.append(chunk)
            data = b"".join(chunks)
            if _is_pdf(response, source, data[:5]):
                if size > _PDF_MAX_BYTES:
                    return f"The PDF is larger than {_PDF_MAX_BYTES // (1024 * 1024)} MB, too large to read."
                text = _pdf_text(data)
            else:
                # BeautifulSoup reads the page's own charset when the server names none.
                charset = response.headers.get("Content-Type", "").partition("charset=")[2].strip(" \"';") or None
                text = BeautifulSoup(data, "html.parser", from_encoding=charset).get_text()
    except _NotPublic:
        return NOT_PUBLIC_MESSAGE
    except Exception as exc:
        return f"Failed to fetch URL: {exc}"

    # Collapse excessive whitespace / blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = text.strip()

    if not text:
        return "The page was fetched but no readable text content was found."

    # Truncate very long pages to avoid overwhelming the LLM context
    from row_bot.models import get_tool_budget
    max_chars = get_tool_budget(0.20, floor=15_000, ceiling=150_000)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n… [content truncated]"

    return f"SOURCE_URL: {source}\n\n{text}"


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
                    "Fetch a webpage or PDF and extract its text content. "
                    "Input must be a full URL starting with http:// or https://. "
                    "Returns the page's readable text. Use this whenever a user "
                    "shares a link or asks about a specific webpage."
                ),
                args_schema=_ReadURLInput,
            )
        ]

    def execute(self, query: str) -> str:
        return _read_url(query)


registry.register(URLReaderTool())
