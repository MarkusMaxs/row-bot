"""Gmail tool — search, read, draft, and send emails via the Gmail API.

Custom send/draft implementations replace the LangChain defaults to add
file attachment support.
"""

from __future__ import annotations

import base64
import email
import email.encoders
import email.mime.base
import email.mime.multipart
import email.mime.text
import logging
import mimetypes
import os
from enum import Enum
from pathlib import Path
from typing import Any, List, Optional, Union

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, field_validator

from row_bot import account_tokens
from row_bot.data_paths import get_row_bot_data_dir
from row_bot.tools.base import BaseTool
from row_bot.tools import registry

logger = logging.getLogger(__name__)

# The Google sign-in and client live in the system keychain (account_tokens); this folder holds
# Gmail's own working files.
_DATA_DIR = get_row_bot_data_dir()
_GMAIL_DIR = _DATA_DIR / "gmail"
_GMAIL_DIR.mkdir(parents=True, exist_ok=True)

# Gmail operations — grouped by risk level
_READ_OPS = ["search_gmail", "get_gmail_message", "get_gmail_thread"]
_COMPOSE_OPS = ["create_gmail_draft"]
_SEND_OPS = ["send_gmail_message"]
ALL_OPERATIONS = _READ_OPS + _COMPOSE_OPS + _SEND_OPS
DEFAULT_OPERATIONS = _READ_OPS + _COMPOSE_OPS  # send disabled by default

# Full access scope
GMAIL_SCOPES = ["https://mail.google.com/"]


# ── Path resolution (shared with Telegram tool) ─────────────────────────

def _resolve_file_path(file_path: str) -> str:
    """Resolve relative paths against workspace root, tracker exports, cwd."""
    p = Path(file_path)
    if p.is_absolute() and p.is_file():
        return str(p)
    try:
        from row_bot.tools import registry as _reg
        fs_tool = _reg.get_tool("filesystem")
        if fs_tool:
            ws_root = fs_tool.get_config("workspace_root", "")
            if ws_root:
                candidate = Path(ws_root) / p
                if candidate.is_file():
                    return str(candidate.resolve())
    except Exception:
        pass
    try:
        tracker_exports = _DATA_DIR / "tracker" / "exports"
        candidate = tracker_exports / p
        if candidate.is_file():
            return str(candidate.resolve())
    except Exception:
        pass
    candidate = Path.cwd() / p
    if candidate.is_file():
        return str(candidate.resolve())
    return file_path


# ── Custom send / draft with attachment support ──────────────────────────

class _EmailRecipientsInput(BaseModel):
    to: list[str] = Field(description="Recipient email address(es).")
    cc: list[str] | None = Field(
        default=None, description="CC recipients.",
    )
    bcc: list[str] | None = Field(
        default=None, description="BCC recipients.",
    )

    @field_validator("to", "cc", "bcc", mode="before")
    @classmethod
    def _normalize_recipient_lists(cls, value):
        if isinstance(value, str):
            return [value]
        return value


class _SendMessageInput(_EmailRecipientsInput):
    message: str = Field(description="The email body text.")
    subject: str = Field(description="The email subject line.")
    attachments: Optional[List[str]] = Field(
        default=None,
        description=(
            "Optional list of file paths to attach. Accepts workspace-relative "
            "paths (e.g. 'report.pdf') or absolute paths."
        ),
    )


class _CreateDraftInput(_EmailRecipientsInput):
    message: str = Field(description="The draft email body text.")
    subject: str = Field(description="The email subject line.")
    attachments: Optional[List[str]] = Field(
        default=None,
        description=(
            "Optional list of file paths to attach. Accepts workspace-relative "
            "paths (e.g. 'report.pdf') or absolute paths."
        ),
    )


def _build_mime_message(
    body: str,
    to: Union[str, List[str]],
    subject: str,
    cc: Optional[Union[str, List[str]]] = None,
    bcc: Optional[Union[str, List[str]]] = None,
    attachments: Optional[List[str]] = None,
) -> email.mime.multipart.MIMEMultipart:
    """Build a MIME message with optional file attachments."""
    mime = email.mime.multipart.MIMEMultipart()
    mime.attach(email.mime.text.MIMEText(body, "plain", "utf-8"))
    mime["To"] = ", ".join(to) if isinstance(to, list) else to
    mime["Subject"] = subject
    if cc:
        mime["Cc"] = ", ".join(cc) if isinstance(cc, list) else cc
    if bcc:
        mime["Bcc"] = ", ".join(bcc) if isinstance(bcc, list) else bcc

    for fp in (attachments or []):
        resolved = _resolve_file_path(fp)
        if not os.path.isfile(resolved):
            logger.warning("Attachment not found, skipping: %s", fp)
            continue
        content_type, _ = mimetypes.guess_type(resolved)
        if content_type is None:
            content_type = "application/octet-stream"
        main_type, sub_type = content_type.split("/", 1)
        with open(resolved, "rb") as f:
            part = email.mime.base.MIMEBase(main_type, sub_type)
            part.set_payload(f.read())
        email.encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition", "attachment",
            filename=os.path.basename(resolved),
        )
        mime.attach(part)

    return mime


def _make_custom_send(build_api_resource):
    """Return a send function that builds its own API client for each call."""
    def _send_gmail_message(
        message: str,
        to: Union[str, List[str]],
        subject: str,
        cc: Optional[Union[str, List[str]]] = None,
        bcc: Optional[Union[str, List[str]]] = None,
        attachments: Optional[List[str]] = None,
    ) -> str:
        try:
            mime = _build_mime_message(message, to, subject, cc, bcc, attachments)
            raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
            sent = (
                build_api_resource().users()
                .messages()
                .send(userId="me", body={"raw": raw})
                .execute()
            )
            att_note = ""
            if attachments:
                resolved = [_resolve_file_path(a) for a in attachments if os.path.isfile(_resolve_file_path(a))]
                if resolved:
                    att_note = f" with {len(resolved)} attachment(s)"
            return f"Message sent{att_note}. Message Id: {sent['id']}"
        except Exception as exc:
            return f"Error sending email: {exc}"
    return _send_gmail_message


def _make_custom_draft(build_api_resource):
    """Return a draft-creation function that builds its own API client for each call."""
    def _create_gmail_draft(
        message: str,
        to: Union[str, List[str]],
        subject: str,
        cc: Optional[Union[str, List[str]]] = None,
        bcc: Optional[Union[str, List[str]]] = None,
        attachments: Optional[List[str]] = None,
    ) -> str:
        try:
            mime = _build_mime_message(message, to, subject, cc, bcc, attachments)
            raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
            draft = (
                build_api_resource().users()
                .drafts()
                .create(userId="me", body={"message": {"raw": raw}})
                .execute()
            )
            att_note = ""
            if attachments:
                resolved = [_resolve_file_path(a) for a in attachments if os.path.isfile(_resolve_file_path(a))]
                if resolved:
                    att_note = f" with {len(resolved)} attachment(s)"
            return f"Draft created{att_note}. Draft Id: {draft['id']}"
        except Exception as exc:
            return f"Error creating draft: {exc}"
    return _create_gmail_draft


class GmailTool(BaseTool):

    @property
    def name(self) -> str:
        return "gmail"

    @property
    def display_name(self) -> str:
        return "📧 Gmail"

    @property
    def description(self) -> str:
        return (
            "Search, read, draft, and send emails via Gmail. "
            "Use this when the user asks about emails, wants to search "
            "their inbox, read messages, draft, or send emails."
        )

    @property
    def destructive_tool_names(self) -> set[str]:
        return {"send_gmail_message"}

    @property
    def untrusted_tool_names(self) -> set[str]:
        return set(_READ_OPS)

    @property
    def enabled_by_default(self) -> bool:
        return False  # Must set up OAuth credentials first

    @property
    def required_api_keys(self) -> dict[str, str]:
        return {}

    @property
    def config_schema(self) -> dict[str, dict]:
        return {
            "credentials_path": {
                "label": "Your own client file (kept outside Row-Bot)",
                "type": "text",
                "default": "",
            },
            "selected_operations": {
                "label": "Allowed operations",
                "type": "multicheck",
                "default": DEFAULT_OPERATIONS,
                "options": ALL_OPERATIONS,
            },
        }

    # ── Auth helpers ─────────────────────────────────────────────────────────
    def has_credentials_file(self) -> bool:
        """Whether Google's Desktop client is saved (or the person keeps their own client file)."""
        return account_tokens.google_client() is not None

    def is_authenticated(self) -> bool:
        return account_tokens.google_covers(GMAIL_SCOPES)

    def check_token_health(self) -> tuple[str, str]:
        """Probe the OAuth token and attempt silent refresh if needed.

        Returns
        -------
        (status, detail) where status is one of:
        - ``"valid"``   — token is fresh, no action needed
        - ``"refreshed"`` — access token was expired, silently refreshed
        - ``"expired"`` — refresh token is revoked; user must re-authenticate
        - ``"missing"`` — no token.json found
        - ``"error"``   — unexpected error during check
        """
        return account_tokens.check_google()

    def _build_api_resource(self):
        """Build a fresh service and HTTP transport for one call only.

        A Gmail ``Resource`` and its ``httplib2`` connection are not thread-safe, and
        parallel tool calls run on worker threads (B347). Built with googleapiclient
        itself: importing langchain_google_community loads ~450 MB of Google Cloud
        libraries Gmail never uses."""
        from googleapiclient.discovery import build

        return build("gmail", "v1", credentials=account_tokens.google_credentials())

    # ── Build toolkit tools ──────────────────────────────────────────────────
    def _get_selected_operations(self) -> list[str]:
        ops = self.get_config("selected_operations", DEFAULT_OPERATIONS)
        return [op for op in ops if op in ALL_OPERATIONS]

    def as_langchain_tools(self) -> list:
        """Return the selected Gmail tools using stored OAuth credentials."""
        if not self.has_credentials_file():
            return []
        if not self.is_authenticated():
            return []

        try:
            self._build_api_resource()
        except Exception as exc:
            logger.warning("Gmail tools unavailable — %s", exc)
            return []

        selected = self._get_selected_operations()
        tools = []

        for name in _READ_OPS:
            if name in selected:
                tools.append(_wrap_gmail_tool_empty_guard(_gmail_read_tool(name, self._build_api_resource)))

        # Custom draft with attachments
        if "create_gmail_draft" in selected:
            tools.append(StructuredTool.from_function(
                func=_make_custom_draft(self._build_api_resource),
                name="create_gmail_draft",
                description=(
                    "Create a Gmail draft email. Supports file attachments — "
                    "pass workspace-relative or absolute file paths in the "
                    "attachments list. Use this when the user wants to draft "
                    "an email, optionally with files attached."
                ),
                args_schema=_CreateDraftInput,
            ))

        # Custom send with attachments
        if "send_gmail_message" in selected:
            tools.append(StructuredTool.from_function(
                func=_make_custom_send(self._build_api_resource),
                name="send_gmail_message",
                description=(
                    "Send an email via Gmail. Supports file attachments — "
                    "pass workspace-relative or absolute file paths in the "
                    "attachments list. Use this when the user wants to send "
                    "an email, optionally with files attached."
                ),
                args_schema=_SendMessageInput,
            ))

        return tools

    def execute(self, query: str) -> str:
        return "Use the individual Gmail operations instead."


# ── Bounded read results ─────────────────────────────────────────────────

# A search returns each message's whole body; a newsletter or a long thread could fill the
# context. Search results carry the start of each body, and one message reads in full up to
# a cap, beyond which the rest stays readable by handle.
_SEARCH_BODY_CHARS = 1_500
_MESSAGE_BODY_CHARS = 30_000


def _bounded_gmail_result(tool_name: str, result):
    """Compact JSON for a Gmail read result, with long bodies shortened (nothing unreachable)."""
    from row_bot.tool_outputs import bounded, compact_json

    def shorten_search_body(message: dict) -> dict:
        body = message.get("body")
        if not isinstance(body, str) or len(body) <= _SEARCH_BODY_CHARS:
            return message
        return {
            **message,
            "body": body[:_SEARCH_BODY_CHARS],
            "body_note": (
                f"First {_SEARCH_BODY_CHARS:,} of {len(body):,} characters; "
                f"get_gmail_message(message_id='{message.get('id', '')}') reads all of it."
            ),
        }

    if tool_name == "search_gmail" and isinstance(result, list):
        result = [shorten_search_body(item) if isinstance(item, dict) else item for item in result]
    elif tool_name == "get_gmail_message" and isinstance(result, dict) and isinstance(result.get("body"), str):
        result = {
            **result,
            "body": bounded(result["body"], tool_name=tool_name, max_chars=_MESSAGE_BODY_CHARS),
        }
    if isinstance(result, (dict, list)):
        return compact_json(result)
    return result


# ── Read tools ───────────────────────────────────────────────────────────
# The search, message and thread reads of langchain_google_community's Gmail toolkit, with
# the same names, descriptions, arguments and results.

class _SearchResource(str, Enum):
    THREADS = "threads"
    MESSAGES = "messages"


class _SearchInput(BaseModel):
    # From https://support.google.com/mail/answer/7190?hl=en
    query: str = Field(
        ...,
        description="The Gmail query. Example filters include from:sender,"
        " to:recipient, subject:subject, -filtered_term,"
        " in:folder, is:important|read|starred, after:year/mo/date, "
        "before:year/mo/date, label:label_name"
        ' "exact phrase".'
        " Search newer/older than using d (day), m (month), and y (year): "
        "newer_than:2d, older_than:1y."
        " Attachments with extension example: filename:pdf. Multiple term"
        " matching example: from:amy OR from:david.",
    )
    resource: _SearchResource = Field(
        default=_SearchResource.MESSAGES,
        description="Whether to search for threads or messages.",
    )
    max_results: int = Field(default=10, description="The maximum number of results to return.")


class _GetMessageInput(BaseModel):
    message_id: str = Field(
        ...,
        description="The unique ID of the email message, retrieved from a search.",
    )


class _GetThreadInput(BaseModel):
    thread_id: str = Field(..., description="The thread ID.")


def _clean_email_body(body: str) -> str:
    from bs4 import BeautifulSoup

    try:
        return str(BeautifulSoup(str(body), "html.parser").get_text())
    except Exception as exc:
        logger.error(exc)
        return str(body)


def _plain_text(email_msg) -> str:
    """The first plain-text part that isn't an attachment, or the whole body."""
    parts = [
        part for part in email_msg.walk()
        if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition"))
    ] if email_msg.is_multipart() else [email_msg]
    if not parts:
        return ""
    payload = parts[0].get_payload(decode=True)
    try:
        return payload.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        return payload.decode("latin-1", errors="replace")


def _read_message(service, message_id: str) -> dict:
    data = service.users().messages().get(userId="me", format="raw", id=message_id).execute()
    email_msg = email.message_from_bytes(base64.urlsafe_b64decode(data["raw"]))
    return {
        "id": message_id,
        "threadId": data["threadId"],
        "snippet": data["snippet"],
        "body": _clean_email_body(_plain_text(email_msg)),
        "subject": email_msg.get("Subject", ""),
        "sender": email_msg.get("From", ""),
    }


def _search_gmail(service, query: str, resource: _SearchResource = _SearchResource.MESSAGES,
                  max_results: int = 10) -> list[dict[str, Any]]:
    resource = _SearchResource(resource)
    listing = getattr(service.users(), resource.value)()
    found = listing.list(userId="me", q=query, maxResults=max_results).execute().get(resource.value, [])
    if resource is _SearchResource.MESSAGES:
        return [_read_message(service, message["id"]) for message in found]
    for thread in found:
        messages = service.users().threads().get(userId="me", id=thread["id"]).execute()["messages"]
        thread["messages"] = [{"snippet": message["snippet"], "id": message["id"]} for message in messages]
    return found


def _get_gmail_message(service, message_id: str) -> dict:
    return _read_message(service, message_id)


def _get_gmail_thread(service, thread_id: str) -> dict:
    thread = service.users().threads().get(userId="me", id=thread_id).execute()
    if not isinstance(thread, dict):
        raise ValueError("The output of the query must be a list.")
    thread["messages"] = [{key: message[key] for key in ("id", "snippet") if key in message}
                          for message in thread["messages"]]
    return thread


_READ_TOOLS = {
    "search_gmail": (_search_gmail, _SearchInput, (
        "Use this tool to search for email messages or threads."
        " The input must be a valid Gmail query."
        " The output is a JSON list of the requested resource."
    )),
    "get_gmail_message": (_get_gmail_message, _GetMessageInput, (
        "Use this tool to fetch an email by message ID."
        " Returns the thread ID, snippet, body, subject, and sender."
    )),
    "get_gmail_thread": (_get_gmail_thread, _GetThreadInput, (
        "Use this tool to search for email messages."
        " The input must be a valid Gmail query."
        " The output is a JSON list of messages."
    )),
}


def _gmail_read_tool(name: str, build_api_resource):
    """A Gmail read tool whose every call has its own API client and a bounded, compact result."""
    read, schema, description = _READ_TOOLS[name]

    def _read(**kwargs):
        return _bounded_gmail_result(name, read(build_api_resource(), **kwargs))

    return StructuredTool.from_function(func=_read, name=name, description=description, args_schema=schema)


# ── Empty-result guard ───────────────────────────────────────────────────

_EMPTY_MESSAGES: dict[str, str] = {
    "search_gmail": (
        "No emails were found matching that query. "
        "The inbox search returned zero results."
    ),
    "get_gmail_message": "No message content was returned.",
    "get_gmail_thread": "No thread content was returned.",
}
_DEFAULT_EMPTY_MSG = "The Gmail tool returned no results."


def _wrap_gmail_tool_empty_guard(tool):
    """Wrap a LangChain Gmail tool so that empty / blank results are
    replaced with an explicit 'no results' message.  This prevents the
    LLM from hallucinating fake emails when the API returns nothing."""
    from langchain_core.tools import StructuredTool

    original_func = tool.func if hasattr(tool, "func") else None
    if original_func is None:
        return tool

    empty_msg = _EMPTY_MESSAGES.get(tool.name, _DEFAULT_EMPTY_MSG)

    def _guarded(*args, **kwargs):
        result = original_func(*args, **kwargs)
        # Treat None, empty string, empty list, or whitespace-only as empty
        if not result or (isinstance(result, str) and not result.strip()):
            return empty_msg
        # Also catch list-like results that stringified to '[]'
        if isinstance(result, str) and result.strip() in ("[]", "[]\n", ""):
            return empty_msg
        return result

    return StructuredTool.from_function(
        func=_guarded,
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
    )


registry.register(GmailTool())
