"""The spending cap of a live eval run: the run stops before it would go past it.

Two caps, both from the environment:

- ``ROW_BOT_LIVE_EVAL_MAX_USD`` (default 5): US dollars of *priced* calls, that
  is calls with a provider-reported cost or a rate in the rates file.
- ``ROW_BOT_LIVE_EVAL_MAX_TOKENS`` (default 2,000,000): tokens processed (input,
  including cache reads and writes, plus output) by every call, priced or not.

``off`` (or 0) turns one cap off; at least one must stay on. Every model call
is checked *before* it is sent, against an upper-bound estimate of that call,
and every task is checked before it starts, against the task's estimate. A
provider without prices is held by the token cap only, and the report says so.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .pricing import CallTokens, Price

MAX_USD_ENV = "ROW_BOT_LIVE_EVAL_MAX_USD"
MAX_TOKENS_ENV = "ROW_BOT_LIVE_EVAL_MAX_TOKENS"
DEFAULT_MAX_USD = 5.0
DEFAULT_MAX_TOKENS = 2_000_000
# Characters per token for estimates. Deliberately low (more tokens per
# character than English text averages) so estimates err on the high side.
CHARS_PER_TOKEN = 3.5
# Output a call may produce when the request names no limit.
DEFAULT_OUTPUT_ALLOWANCE = 4_096
MAX_OUTPUT_ALLOWANCE = 16_384
_OFF = {"off", "none", "0", "0.0", "no", "false"}


class BudgetExceeded(RuntimeError):
    """Raised before a call or task that would take the run past a cap."""


class BudgetConfigError(ValueError):
    """The caps in the environment cannot be used."""


@dataclass(frozen=True)
class Caps:
    max_usd: float | None = DEFAULT_MAX_USD
    max_tokens: int | None = DEFAULT_MAX_TOKENS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Caps:
        source = os.environ if env is None else env
        max_usd = _parse_cap(source.get(MAX_USD_ENV), DEFAULT_MAX_USD, MAX_USD_ENV, float)
        max_tokens = _parse_cap(source.get(MAX_TOKENS_ENV), DEFAULT_MAX_TOKENS, MAX_TOKENS_ENV, int)
        if max_usd is None and max_tokens is None:
            raise BudgetConfigError(f"Set {MAX_USD_ENV} or {MAX_TOKENS_ENV}; a live run never runs without a cap.")
        return cls(max_usd=max_usd, max_tokens=max_tokens)

    def describe(self) -> str:
        usd = f"${self.max_usd:.2f} priced" if self.max_usd is not None else "no USD cap"
        tokens = f"{self.max_tokens:,} tokens" if self.max_tokens is not None else "no token cap"
        return f"{usd}, {tokens}"


def _parse_cap(raw: str | None, default: Any, name: str, kind: type) -> Any:
    text = str(raw or "").strip().lower()
    if not text:
        return default
    if text in _OFF:
        return None
    try:
        value = kind(float(text)) if kind is int else kind(text)
    except ValueError as exc:
        raise BudgetConfigError(f"{name}={raw!r} is not a number or 'off'") from exc
    if not math.isfinite(float(value)) or value <= 0:
        raise BudgetConfigError(f"{name} must be positive or 'off'")
    return value


@dataclass
class Spend:
    """What a run has spent so far."""

    tokens: int = 0
    usd: float = 0.0
    unpriced_tokens: int = 0
    calls: int = 0

    def add(self, other: Spend) -> None:
        self.tokens += other.tokens
        self.usd += other.usd
        self.unpriced_tokens += other.unpriced_tokens
        self.calls += other.calls


@dataclass
class BudgetGuard:
    """Caps plus what has been spent; answers "may this call or task run?"."""

    caps: Caps
    spent: Spend = field(default_factory=Spend)

    def over_by(self, tokens: int, usd: float | None = None) -> str:
        """Why spending *tokens* (and *usd*, when priced) now would pass a cap, or ''."""
        if self.caps.max_tokens is not None and self.spent.tokens + max(0, tokens) > self.caps.max_tokens:
            return (
                f"token cap: {self.spent.tokens:,} spent + {max(0, tokens):,} estimated "
                f"> {self.caps.max_tokens:,}"
            )
        if self.caps.max_usd is not None:
            if self.spent.usd >= self.caps.max_usd:
                return f"USD cap: ${self.spent.usd:.4f} spent >= ${self.caps.max_usd:.2f}"
            # A call with no estimate (no rates) is held by the token cap and by the spend so far.
            if usd is not None and self.spent.usd + max(0.0, usd) > self.caps.max_usd:
                return f"USD cap: ${self.spent.usd:.4f} spent + ${max(0.0, usd):.4f} estimated > ${self.caps.max_usd:.2f}"
        return ""

    def require(self, tokens: int, usd: float | None = None, *, what: str = "call") -> None:
        reason = self.over_by(tokens, usd)
        if reason:
            raise BudgetExceeded(f"live eval budget: not starting this {what} ({reason})")

    def charge(self, tokens: CallTokens, price: Price) -> None:
        self.spent.calls += 1
        self.spent.tokens += tokens.processed
        if price.usd is None:
            self.spent.unpriced_tokens += tokens.processed
        else:
            self.spent.usd += price.usd

    def charge_spend(self, spend: Spend) -> None:
        self.spent.add(spend)

    def remaining(self) -> Caps:
        """The caps left for a part of the run (a worker process, a later task)."""
        return Caps(
            max_usd=None if self.caps.max_usd is None else max(0.0, self.caps.max_usd - self.spent.usd),
            max_tokens=None if self.caps.max_tokens is None else max(0, self.caps.max_tokens - self.spent.tokens),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"caps": asdict(self.caps), "spent": asdict(self.spent)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BudgetGuard:
        caps = data.get("caps") or {}
        spent = data.get("spent") or {}
        return cls(Caps(caps.get("max_usd"), caps.get("max_tokens")), Spend(**spent))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path, default_caps: Caps) -> BudgetGuard:
        if path.is_file():
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        return cls(default_caps)


def text_tokens(chars: int) -> int:
    """An upper-leaning token estimate for *chars* characters of prompt text."""
    return math.ceil(max(0, chars) / CHARS_PER_TOKEN)


def _content_chars(content: Any) -> int:
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, Mapping):
        if content.get("type") in {"image", "image_url", "input_image"}:
            return 6_000  # about 1.6K tokens: a large image, on the high side
        return sum(_content_chars(value) for key, value in content.items() if key not in {"signature", "id"})
    if isinstance(content, (list, tuple)):
        return sum(_content_chars(item) for item in content)
    return len(str(content))


def estimate_call(messages: Iterable[Any], *, tools: Any = None, max_output: int | None = None) -> tuple[int, int]:
    """Upper-leaning (input, output) tokens for one model call.

    Input counts every message and the tool schemas; output is the request's
    own limit (capped) or a default allowance. *messages* may be LangChain
    messages or plain dicts; their ``content`` and tool calls are counted.
    Signatures and ids are skipped.
    """
    chars = 0
    for message in messages:
        content = message.get("content") if isinstance(message, Mapping) else getattr(message, "content", None)
        chars += _content_chars(content) + 16
        tool_calls = message.get("tool_calls") if isinstance(message, Mapping) else getattr(message, "tool_calls", None)
        if tool_calls:
            chars += len(json.dumps(tool_calls, default=str))
    if tools:
        chars += len(json.dumps(tools, default=str))
    output = DEFAULT_OUTPUT_ALLOWANCE if not max_output else min(int(max_output), MAX_OUTPUT_ALLOWANCE)
    return text_tokens(chars), output
