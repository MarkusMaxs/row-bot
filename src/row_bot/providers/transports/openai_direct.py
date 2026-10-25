"""``ChatOpenAI`` for the direct OpenAI provider: cache key and stateless reasoning replay.

- Every request carries the ``prompt_cache_key`` of ``cache_affinity``, on
  Chat Completions and Responses alike: one shared by interactive chats, and a
  conversation's own for workflows, goals and helpers. Calls outside a
  conversation send none. ``prompt_cache_retention`` is left at the server
  default (24 hours for organizations without Zero Data Retention).
- Responses requests are stateless: the runtime sets ``store=False`` and asks
  for ``reasoning.encrypted_content``, so nothing is kept at OpenAI and the
  reasoning items travel in the conversation itself. langchain-openai keeps
  them in the AIMessage content and replays the ones that carry their
  ciphertext (``_construct_responses_api_input(store=False)``).

Two gaps in the pinned langchain-openai are closed here:

1. When streaming it takes a reasoning item from ``response.output_item.added``,
   where OpenAI says the ciphertext may be incomplete, and ignores the matching
   ``response.output_item.done``. The Responses stream is filtered so the
   ciphertext comes from the ``done`` event (newer langchain-openai does the
   same; when it does, the filter is not installed).
2. A reasoning item it can't replay is dropped, but the ``fc_`` id of the
   function call it produced is kept. A stored call id whose reasoning item is
   missing can be refused, so such call ids are left out. That covers
   conversations whose reasoning was stored server-side before ``store=False``.

A request whose replayed reasoning can no longer be read (HTTP 400 naming the
encrypted content) is retried once without it.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, AsyncIterator, Iterator

import openai
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI
from pydantic import model_validator
from typing_extensions import Self

from row_bot.providers.cache_affinity import shared_prefix_cache_key
from row_bot.providers.transports.cancellable_http import RequestStopped
from row_bot.providers.transports.responses_reasoning import (
    ENCRYPTED_REASONING_INCLUDE,
    is_encrypted_reasoning_rejection,
)

logger = logging.getLogger(__name__)

OPENAI_DIRECT_SCOPE = "openai"


class OpenAIRequestStopped(RequestStopped, openai.OpenAIError):
    """Stop ended the request. The OpenAI SDK (also behind xAI and OpenCode) passes its own errors on without
    retrying them."""


def stateless_responses_kwargs(model_name: str) -> dict[str, Any]:
    """Constructor arguments for a direct OpenAI Responses model: nothing stored at OpenAI.

    Encrypted reasoning is requested only from reasoning models; the
    ``-chat`` variants don't reason and may refuse the include.
    """
    kwargs: dict[str, Any] = {"store": False}
    bare = str(model_name or "").split("/")[-1].strip().lower()
    if "-chat" not in bare:
        kwargs["include"] = [ENCRYPTED_REASONING_INCLUDE]
    return kwargs


class DirectChatOpenAI(ChatOpenAI):
    """``ChatOpenAI`` with a per-conversation cache key and stateless reasoning replay."""

    @model_validator(mode="after")
    def _complete_streamed_reasoning(self) -> Self:
        if _langchain_streams_partial_reasoning():
            if self.root_client is not None and not isinstance(self.root_client, _CompletedReasoningClient):
                self.root_client = _CompletedReasoningClient(self.root_client)
            if self.root_async_client is not None and not isinstance(
                self.root_async_client, _CompletedReasoningAsyncClient
            ):
                self.root_async_client = _CompletedReasoningAsyncClient(self.root_async_client)
        return self

    def _get_request_payload(self, input_: Any, *, stop: list[str] | None = None, **kwargs: Any) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if "prompt_cache_key" not in payload:
            cache_key = shared_prefix_cache_key(OPENAI_DIRECT_SCOPE)
            if cache_key:
                payload["prompt_cache_key"] = cache_key
        if payload.get("store") is False and isinstance(payload.get("input"), list):
            _drop_unpaired_function_call_ids(payload["input"])
        return payload

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        try:
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        except Exception as exc:
            stripped = _recoverable_without_reasoning(exc, messages, self.model_name)
            if stripped is None:
                raise
        return super()._generate(stripped, stop=stop, run_manager=run_manager, **kwargs)

    def _stream(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> Iterator[ChatGenerationChunk]:
        emitted = False
        try:
            for chunk in super()._stream(messages, *args, **kwargs):
                emitted = True
                yield chunk
            return
        except Exception as exc:
            stripped = None if emitted else _recoverable_without_reasoning(exc, messages, self.model_name)
            if stripped is None:
                raise
        yield from super()._stream(stripped, *args, **kwargs)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        try:
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        except Exception as exc:
            stripped = _recoverable_without_reasoning(exc, messages, self.model_name)
            if stripped is None:
                raise
        return await super()._agenerate(stripped, stop=stop, run_manager=run_manager, **kwargs)

    async def _astream(
        self, messages: list[BaseMessage], *args: Any, **kwargs: Any
    ) -> AsyncIterator[ChatGenerationChunk]:
        emitted = False
        try:
            async for chunk in super()._astream(messages, *args, **kwargs):
                emitted = True
                yield chunk
            return
        except Exception as exc:
            stripped = None if emitted else _recoverable_without_reasoning(exc, messages, self.model_name)
            if stripped is None:
                raise
        async for chunk in super()._astream(stripped, *args, **kwargs):
            yield chunk


def _recoverable_without_reasoning(
    exc: BaseException, messages: list[BaseMessage], model_name: str
) -> list[BaseMessage] | None:
    """The messages without replayed reasoning, when that is what the server refused."""
    if not is_encrypted_reasoning_rejection(exc):
        return None
    stripped: list[BaseMessage] = []
    removed = 0
    for message in messages:
        content = getattr(message, "content", None)
        if isinstance(message, AIMessage) and isinstance(content, list):
            kept = [block for block in content if not (isinstance(block, dict) and block.get("type") == "reasoning")]
            if len(kept) != len(content):
                removed += len(content) - len(kept)
                message = message.model_copy(update={"content": kept})
        stripped.append(message)
    if not removed:
        return None
    logger.warning(
        "openai_responses: replayed reasoning was rejected; retrying once without %d reasoning item(s) model=%s",
        removed,
        model_name,
    )
    return stripped


def _drop_unpaired_function_call_ids(items: list[Any]) -> None:
    """Leave out the ``fc_`` id of a function call whose reasoning item isn't replayed.

    The call itself, with its ``call_id``, stays: a call without an id is an
    ordinary client-supplied item.
    """
    reasoning_replayed = False
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "reasoning":
            reasoning_replayed = True
        elif item_type == "function_call":
            if not reasoning_replayed:
                item.pop("id", None)
        elif item_type == "function_call_output" or (item_type == "message" and item.get("role") != "assistant"):
            reasoning_replayed = False


@functools.lru_cache(maxsize=1)
def _langchain_streams_partial_reasoning() -> bool:
    """True when langchain-openai ignores a reasoning item's ``output_item.done`` event."""
    try:
        from langchain_openai.chat_models import base
        from openai.types.responses import ResponseOutputItemDoneEvent, ResponseReasoningItem

        event = ResponseOutputItemDoneEvent.model_construct(
            type="response.output_item.done",
            output_index=0,
            sequence_number=0,
            item=ResponseReasoningItem.model_construct(
                type="reasoning", id="rs_probe", summary=[], encrypted_content="probe"
            ),
        )
        *_, chunk = base._convert_responses_chunk_to_generation_chunk(event, 0, 0, 0)
    except Exception:
        logger.debug("openai_responses: could not inspect langchain-openai streaming", exc_info=True)
        return False
    return chunk is None


def _completed_reasoning_event(event: Any) -> Any:
    """Move a streamed reasoning item's ciphertext from its ``added`` to its ``done`` event.

    langchain-openai turns ``output_item.added`` into the reasoning block and
    merges later chunks with the same index into it (strings concatenate). So
    the ``added`` event loses its possibly partial ciphertext, and the ``done``
    event is passed on in the ``added`` form with only the final ciphertext.
    """
    item = getattr(event, "item", None)
    if getattr(item, "type", None) != "reasoning" or not getattr(item, "encrypted_content", None):
        return event
    event_type = getattr(event, "type", "")
    if event_type == "response.output_item.added":
        return event.model_copy(update={"item": item.model_copy(update={"encrypted_content": None})})
    if event_type == "response.output_item.done":
        from openai.types.responses import ResponseOutputItemAddedEvent

        return ResponseOutputItemAddedEvent.model_construct(
            type="response.output_item.added",
            output_index=getattr(event, "output_index", 0),
            sequence_number=getattr(event, "sequence_number", 0),
            item=item.model_copy(update={"summary": [], "content": None, "status": None}),
        )
    return event


class _CompletedReasoningStream:
    def __init__(self, stream: Any) -> None:
        self._stream = stream

    def __enter__(self) -> _CompletedReasoningStream:
        self._stream.__enter__()
        return self

    def __exit__(self, *exc_info: Any) -> Any:
        return self._stream.__exit__(*exc_info)

    def __iter__(self) -> Iterator[Any]:
        for event in self._stream:
            yield _completed_reasoning_event(event)

    def close(self) -> None:
        self._stream.close()


class _AsyncCompletedReasoningStream:
    def __init__(self, stream: Any) -> None:
        self._stream = stream

    async def __aenter__(self) -> _AsyncCompletedReasoningStream:
        await self._stream.__aenter__()
        return self

    async def __aexit__(self, *exc_info: Any) -> Any:
        return await self._stream.__aexit__(*exc_info)

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._events()

    async def _events(self) -> AsyncIterator[Any]:
        async for event in self._stream:
            yield _completed_reasoning_event(event)

    async def close(self) -> None:
        await self._stream.close()


class _Delegate:
    """Forwards everything it doesn't define to the wrapped SDK object."""

    def __init__(self, wrapped: Any) -> None:
        self._wrapped = wrapped

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._wrapped, name)


class _CompletedReasoningResponses(_Delegate):
    def create(self, **kwargs: Any) -> Any:
        result = self._wrapped.create(**kwargs)
        return _CompletedReasoningStream(result) if kwargs.get("stream") else result


class _AsyncCompletedReasoningResponses(_Delegate):
    async def create(self, **kwargs: Any) -> Any:
        result = await self._wrapped.create(**kwargs)
        return _AsyncCompletedReasoningStream(result) if kwargs.get("stream") else result


class _CompletedReasoningClient(_Delegate):
    """An ``openai.OpenAI`` whose streamed Responses carry complete reasoning items."""

    def __init__(self, wrapped: Any) -> None:
        super().__init__(wrapped)
        self.responses = _CompletedReasoningResponses(wrapped.responses)


class _CompletedReasoningAsyncClient(_Delegate):
    """An ``openai.AsyncOpenAI`` whose streamed Responses carry complete reasoning items."""

    def __init__(self, wrapped: Any) -> None:
        super().__init__(wrapped)
        self.responses = _AsyncCompletedReasoningResponses(wrapped.responses)
