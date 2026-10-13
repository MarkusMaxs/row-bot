"""Opt-in live verification of the cost plan's provider work (A2b, A3, B1, B2, C1, A4b).

These call real providers and spend money. They run only with
``ROW_BOT_LIVE_EVAL=1`` and a provider's key in the environment (never a
Row-Bot data folder or the OS keychain), under the caps in
``ROW_BOT_LIVE_EVAL_MAX_USD`` / ``ROW_BOT_LIVE_EVAL_MAX_TOKENS``. Each check
records what it saw in ``<run folder>/probes.jsonl``; ``probe`` checks record a
provider's answer for a decision and do not fail on it. The check list, with
the work package behind each, is ``tests/e2e/live_eval/checks.py``; the manual
checks that cannot be automated are ``tests/e2e/live_eval/manual.py``
(``uv run python scripts/live_eval.py manual``).

    ROW_BOT_LIVE_EVAL=1 ANTHROPIC_API_KEY=... uv run python -m pytest tests/e2e/test_live_verification.py -m live_provider
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

import pytest

from tests.e2e.live_eval import orchestrate, providers as provider_registry
from tests.e2e.live_eval.capture import HttpCapture
from tests.e2e.live_eval.checks import by_id as check_by_id
from tests.e2e.live_eval.report import CallRecord, ProbeRecord
from tests.e2e.live_eval.session import LiveSession
from tests.e2e.live_eval.tasks import chapter_word, filler

pytestmark = pytest.mark.live_provider

A2B_MODELS_ENV = "ROW_BOT_LIVE_EVAL_A2B_MODELS"
A2B_MODELS_DEFAULT = "claude-opus-5-5,claude-sonnet-5-5,claude-haiku-5-5,claude-fable-5-1"
A2B_EDIT_MODEL_ENV = "ROW_BOT_LIVE_EVAL_A2B_EDIT_MODEL"
A3_MODEL_ENV = "ROW_BOT_LIVE_EVAL_A3_MODEL"
OPENAI_CHAT_MODEL_ENV = "ROW_BOT_LIVE_EVAL_OPENAI_CHAT_COMPLETIONS_MODEL"
OPENCODE_CLAUDE_ENV = "ROW_BOT_LIVE_EVAL_OPENCODE_CLAUDE"
A4B_ENV = "ROW_BOT_LIVE_EVAL_A4B"
A4B_MODEL_ENV = "ROW_BOT_LIVE_EVAL_A4B_MODEL"
A4B_WINDOW_ENV = "ROW_BOT_LIVE_EVAL_A4B_WINDOW"
# About 5.7K tokens: above every Claude model's minimum cacheable prefix (4,096 at most).
CACHE_PREFIX_CHARS = 20_000


# ── Session, budget and helpers ─────────────────────────────────────────────


@pytest.fixture(scope="module")
def live() -> Iterator[LiveSession]:
    if not orchestrate.enabled():
        pytest.skip(f"set {orchestrate.ENABLE_ENV}=1 to run real provider calls (they cost money)")
    session = LiveSession.start()
    session.install_gate()
    yield session
    session.finish()


@pytest.fixture(autouse=True)
def _within_budget(request: pytest.FixtureRequest) -> None:
    check = check_by_id().get(request.node.originalname.removeprefix("test_"))
    if check is None or not orchestrate.enabled():
        return
    session: LiveSession = request.getfixturevalue("live")
    reason = session.over(check.est_tokens)
    if reason:
        pytest.skip(f"budget: {reason}")


def _key(provider_id: str) -> str:
    if provider_id == "deepseek":
        return os.environ.get(provider_registry.DEEPSEEK_KEY_ENV, "")
    target = provider_registry.target(provider_id)
    return os.environ.get(target.key_env, "") if target.key_env else ""


def _need(provider_id: str) -> None:
    if provider_id == "ollama":
        state = provider_registry.availability(provider_registry.target("ollama"))
        if not state.available:
            pytest.skip(state.reason)
        why = orchestrate._ollama_ready(state.model)
        if why:
            pytest.skip(why)
        return
    if not _key(provider_id):
        name = provider_registry.DEEPSEEK_KEY_ENV if provider_id == "deepseek" else provider_registry.target(provider_id).key_env
        pytest.skip(f"set {name}")


def _available(provider_ids: tuple[str, ...] | None = None) -> list[provider_registry.Availability]:
    found = []
    for item in provider_registry.TARGETS:
        if provider_ids and item.provider_id not in provider_ids:
            continue
        state = provider_registry.availability(item)
        if state.available and (item.provider_id != "ollama" or not orchestrate._ollama_ready(state.model)):
            found.append(state)
    return found


def _model(provider_id: str, model_id: str) -> Any:
    if provider_id == "ollama":
        from row_bot import models

        return models.get_llm_for(f"model:ollama:{model_id}")
    from row_bot.providers.runtime import create_chat_model

    return create_chat_model(model_id, provider_id)


def _usage(message: Any) -> Any:
    from row_bot.prompt_cache import normalize_model_usage

    return normalize_model_usage(dict(getattr(message, "response_metadata", None) or {}),
                                 dict(getattr(message, "usage_metadata", None) or {}))


def _stream(llm: Any, messages: list) -> Any:
    full = None
    for chunk in llm.stream(messages):
        full = chunk if full is None else full + chunk
    return full


@contextmanager
def _conversation(thread_id: str) -> Iterator[None]:
    """Make Row-Bot's per-conversation cache keys see *thread_id* (B2)."""
    import row_bot.agent as agent

    token = agent._current_thread_id_var.set(thread_id)
    try:
        yield
    finally:
        agent._current_thread_id_var.reset(token)


def _prefix(label: str) -> str:
    # A fresh nonce per run keeps the first call cold even when an earlier run warmed the cache.
    return f"[{label} {uuid.uuid4().hex}] " + filler(CACHE_PREFIX_CHARS, seed=label)


def _ledger_rows_since(session: LiveSession, baseline: int) -> list[CallRecord]:
    from tests.e2e.live_eval import ledger as ledger_reader

    return ledger_reader.rows_after(session.gate.ledger_path(), baseline)


def _baseline(session: LiveSession) -> int:
    from tests.e2e.live_eval import ledger as ledger_reader

    return ledger_reader.max_id(session.gate.ledger_path())


def _anthropic_body(exchange: Any) -> dict[str, Any]:
    try:
        return exchange.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _message_start(exchange: Any) -> dict[str, Any]:
    return next((event.get("message") or {} for event in exchange.sse_events() if event.get("type") == "message_start"), {})


def _thinking_blocks(message: Any) -> int:
    content = getattr(message, "content", None)
    return sum(1 for block in content or [] if isinstance(block, dict) and block.get("type") in {"thinking", "redacted_thinking"}) if isinstance(content, list) else 0


# ── A2b: preserved-thinking controls (direct Anthropic) ─────────────────────


def test_a2b_controls_accepted(live: LiveSession, caplog: pytest.LogCaptureFixture) -> None:
    """Spec: wp-A2b-handoff 'Manual checks' 1."""
    from langchain_core.messages import HumanMessage

    from row_bot.providers.transports.thinking_binding import THINKING_BINDING_BETA

    _need("anthropic")
    caplog.set_level(logging.INFO, logger="row_bot")
    models = [name.strip() for name in os.environ.get(A2B_MODELS_ENV, A2B_MODELS_DEFAULT).split(",") if name.strip()]
    cases = [(name, None) for name in models] + [("claude-opus-4-8", {"type": "adaptive"})]
    problems: list[str] = []
    for model_id, thinking in cases:
        llm = _model("anthropic", model_id)
        runnable = llm.bind(thinking=thinking) if thinking else llm
        observed: dict[str, Any] = {}
        with HttpCapture(lambda request: "/v1/messages" in str(request.url)) as capture:
            try:
                runnable.invoke([HumanMessage(content="Reply with exactly: ok")])
                _stream(runnable, [HumanMessage(content="Reply with exactly: ok")])
            except Exception as exc:
                observed["error"] = f"{type(exc).__name__}: {exc}"[:300]
        for mode, exchange in zip(("invoke", "stream"), capture.exchanges):
            body = exchange.request_json or {}
            sent = (body.get("thinking") or {}).get("block_binding") or {}
            response = _message_start(exchange) if mode == "stream" else _anthropic_body(exchange)
            observed[mode] = {
                "status": exchange.status,
                "beta_header": THINKING_BINDING_BETA in exchange.request_headers.get("anthropic-beta", ""),
                "prefix_mismatch_behavior": sent.get("prefix_mismatch_behavior"),
                "input_transformations": response.get("input_transformations", "absent"),
            }
            if exchange.status == 400 and ("block_binding" in exchange.text() or "thinking-binding" in exchange.text()):
                problems.append(f"{model_id} {mode}: the controls were rejected")
            elif exchange.status == 200:
                if not observed[mode]["beta_header"] or observed[mode]["prefix_mismatch_behavior"] != "drop_block":
                    problems.append(f"{model_id} {mode}: the controls were not sent")
                if observed[mode]["input_transformations"] != []:
                    problems.append(f"{model_id} {mode}: input_transformations={observed[mode]['input_transformations']!r}")
        status = "pass" if not any(p.startswith(model_id) for p in problems) and "error" not in observed else (
            "skipped" if "error" in observed and not any(p.startswith(model_id) for p in problems) else "fail")
        live.record(ProbeRecord("a2b_controls_accepted", "anthropic", model_id, status, observed))
    if "endpoint rejected the binding controls" in caplog.text:
        problems.append("a 'rejected controls' retry happened on the direct API")
    assert not problems, problems


@pytest.fixture(scope="module")
def opus_history(live: LiveSession) -> dict[str, Any]:
    """Turn 1 on Opus 5.5 with a tool round, so signed thinking blocks sit in the history."""
    from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
    from langchain_core.tools import tool

    _need("anthropic")

    @tool
    def lookup_code(name: str) -> str:
        """Look up the numeric code registered for a name."""
        return {"alpha": "7731"}.get(name.lower(), "unknown")

    model_id = os.environ.get(A2B_EDIT_MODEL_ENV, "claude-opus-5-5")
    base = _model("anthropic", model_id)
    llm = base.bind_tools([lookup_code])
    system = "You are a careful assistant. Think before answering. " + filler(1_500, seed="a2b-system")
    history: list = [SystemMessage(content=system), HumanMessage(content=(
        "Use the lookup_code tool to find the code for 'alpha'. Then reason about whether that code is prime "
        "and answer in one sentence."))]
    first = llm.invoke(history)
    history.append(first)
    for call in getattr(first, "tool_calls", None) or []:
        history.append(ToolMessage(content=lookup_code.invoke(call["args"]), tool_call_id=call["id"]))
    if len(history) > 3:
        history.append(llm.invoke(history))
    blocks = sum(_thinking_blocks(message) for message in history)
    return {"model": model_id, "base": base, "llm": llm, "history": history, "system": system, "thinking_blocks": blocks}


def _edited(history: list, system: str) -> list:
    from langchain_core.messages import HumanMessage, SystemMessage

    return [SystemMessage(content=system + " (This line was edited.)"), *history[1:],
            HumanMessage(content="Thanks. Reply with exactly: done")]


def test_a2b_drop_block_on_edit(live: LiveSession, opus_history: dict[str, Any], caplog: pytest.LogCaptureFixture) -> None:
    """Spec: wp-A2b-handoff 'Manual checks' 2."""
    if not opus_history["thinking_blocks"]:
        live.record(ProbeRecord("a2b_drop_block_on_edit", "anthropic", opus_history["model"], "skipped",
                                note="the model wrote no thinking block in turn 1; nothing to drop"))
        pytest.skip("no thinking block in turn 1")
    caplog.set_level(logging.INFO, logger="row_bot")
    reply = opus_history["llm"].invoke(_edited(opus_history["history"], opus_history["system"]))
    transformations = (reply.response_metadata or {}).get("input_transformations") or []
    drops = [item for item in transformations if item.get("type") == "thinking_dropped"]
    observed = {"transformations": transformations[:10], "info_logged": "API dropped" in caplog.text}
    ok = bool(drops) and all(item.get("reason") == "prefix_binding_mismatch" for item in drops) and observed["info_logged"]
    live.record(ProbeRecord("a2b_drop_block_on_edit", "anthropic", opus_history["model"], "pass" if ok else "fail", observed))
    assert ok, observed


def test_a2b_error_mode_recovers(live: LiveSession, opus_history: dict[str, Any], caplog: pytest.LogCaptureFixture,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec: wp-A2b-handoff 'Manual checks' 3. Setting the field opts any account into enforcement."""
    from row_bot.providers.transports import thinking_binding

    if not opus_history["thinking_blocks"]:
        pytest.skip("no thinking block in turn 1")
    caplog.set_level(logging.INFO, logger="row_bot")
    monkeypatch.setattr(thinking_binding, "DROP_BLOCK_BINDING", {"prefix_mismatch_behavior": "error"})
    edited = _edited(opus_history["history"], opus_history["system"])
    with HttpCapture(lambda request: "/v1/messages" in str(request.url)) as capture:
        reply = opus_history["llm"].invoke(edited)
    statuses = [exchange.status for exchange in capture.exchanges]
    retry_body = capture.exchanges[-1].request_json or {} if capture.exchanges else {}
    retry_thinking = sum(
        1 for message in retry_body.get("messages") or [] for block in (message.get("content") or [])
        if isinstance(block, dict) and block.get("type") in {"thinking", "redacted_thinking"}
    )
    first_error = capture.exchanges[0].text()[:400] if capture.exchanges and statuses[0] == 400 else ""
    observed = {
        "statuses": statuses,
        "first_error": first_error,
        "signature_retries": caplog.text.count("thinking signature rejected"),
        "reasoning_fallback": "reasoning_fallback:" in caplog.text,
        "retry_thinking_blocks": retry_thinking,
        "answered": bool(str(reply.content)),
    }
    ok = (statuses[:1] == [400] and statuses[-1] == 200 and len(statuses) == 2 and observed["signature_retries"] == 1
          and not observed["reasoning_fallback"] and retry_thinking == 0 and "bound to a different conversation" in first_error)

    # The remembered-route case: controls are not sent, so only an enforced account rejects the edit.
    caplog.clear()
    base = opus_history["base"]
    monkeypatch.setattr(thinking_binding, "_REJECTED_ROUTES", {(str(base.anthropic_api_url), str(base.model))})
    with HttpCapture(lambda request: "/v1/messages" in str(request.url)) as capture:
        opus_history["llm"].invoke(edited)
    no_header = [exchange.status for exchange in capture.exchanges]
    observed["no_header_statuses"] = no_header
    observed["account_enforced"] = bool(no_header) and no_header[0] == 400
    if observed["account_enforced"]:
        observed["no_header_error_names_beta"] = "thinking-binding-controls" in capture.exchanges[0].text()
        ok = ok and no_header[-1] == 200 and len(no_header) == 2
    live.record(ProbeRecord("a2b_error_mode_recovers", "anthropic", opus_history["model"], "pass" if ok else "fail", observed))
    assert ok, observed


def test_a2b_model_switch(live: LiveSession, opus_history: dict[str, Any]) -> None:
    """Spec: wp-A2b-handoff 'Manual checks' 4 (a probe: model_binding_mismatch is expected, not a bug)."""
    from langchain_core.messages import HumanMessage

    if not opus_history["thinking_blocks"]:
        pytest.skip("no thinking block in turn 1")
    other = _model("anthropic", "claude-sonnet-5-5")
    reply = other.invoke([*opus_history["history"], HumanMessage(content="Reply with exactly: switched")])
    transformations = (reply.response_metadata or {}).get("input_transformations") or []
    reasons = sorted({str(item.get("reason")) for item in transformations if item.get("type") == "thinking_dropped"})
    live.record(ProbeRecord("a2b_model_switch", "anthropic", "claude-opus-5-5 -> claude-sonnet-5-5",
                            "pass" if "model_binding_mismatch" in reasons else "observed", {"drop_reasons": reasons}))


# ── A3: Claude caching ──────────────────────────────────────────────────────


def _cached_messages(provider_id: str, model_id: str, prefix: str, question: str, ttl: str) -> list:
    from langchain_core.messages import HumanMessage, SystemMessage

    from row_bot.prompt_cache import apply_anthropic_system_cache_marker, with_conversation_cache

    messages: list = [SystemMessage(content=prefix), HumanMessage(content=question)]
    messages, _result = apply_anthropic_system_cache_marker(
        messages, provider_id=provider_id, model_id=model_id, cache_eligible_system_message_ids={id(messages[0])}, ttl=ttl,
    )
    return with_conversation_cache(messages, ttl=ttl)


def test_a3_ttl_1h_and_5m_reuse(live: LiveSession) -> None:
    """Spec: wp-A3-handoff 'Manual checks' (direct API): 1h accepted, read next call; 1h entry vs a 5m request."""
    _need("anthropic")
    model_id = os.environ.get(A3_MODEL_ENV, "claude-haiku-5-5")
    llm = _model("anthropic", model_id)
    prefix = _prefix("a3-ttl")
    with HttpCapture(lambda request: "/v1/messages" in str(request.url)) as capture:
        first = _usage(llm.invoke(_cached_messages("anthropic", model_id, prefix, "Reply with exactly: one", "1h")))
        second = _usage(llm.invoke(_cached_messages("anthropic", model_id, prefix, "Reply with exactly: one", "1h")))
        third = _usage(llm.invoke(_cached_messages("anthropic", model_id, prefix, "Reply with exactly: one", "5m")))
    body = capture.exchanges[0].request_json or {} if capture.exchanges else {}
    system_markers = [block.get("cache_control") for block in body.get("system") or [] if isinstance(block, dict) and block.get("cache_control")]
    observed = {
        "system_marker": system_markers[-1] if system_markers else None,
        "top_level_marker": body.get("cache_control"),
        "call1": {"write": first.cache_write_tokens, "write_1h": first.cache_write_1h_tokens, "read": first.cache_read_tokens},
        "call2": {"write": second.cache_write_tokens, "read": second.cache_read_tokens},
        "call3_5m": {"write": third.cache_write_tokens, "read": third.cache_read_tokens},
        "one_hour_entry_read_by_5m_request": bool(third.cache_read_tokens),
    }
    ok = bool(first.cache_write_1h_tokens) and bool(second.cache_read_tokens) and (system_markers[-1:] == [{"type": "ephemeral", "ttl": "1h"}])
    live.record(ProbeRecord("a3_ttl_1h_and_5m_reuse", "anthropic", model_id, "pass" if ok else "fail", observed))
    assert ok, observed


def _main_calls(record: Any) -> list[CallRecord]:
    return [call for call in record.calls if call.purpose in {"main", "chat_only"}]


def test_a3_agent_loop_cache(live: LiveSession) -> None:
    """Spec: wp-A3-handoff warm loop and >15 positions; D3 5m on workflow runs; A4a jumps via the ledger."""
    _need("anthropic")
    state = provider_registry.availability(provider_registry.target("anthropic"))
    guard = live.guard()
    result = orchestrate.run_provider(state, ["warm_loop_cache", "long_turn_positions", "workflow_run"],
                                      live.run_dir, guard, label="checks")
    live.carry(result.get("spent") or {})
    from tests.e2e.live_eval.report import TaskRecord

    records = {task["task_id"]: TaskRecord.from_dict(task) for task in result.get("tasks") or []}
    observed: dict[str, Any] = {"worker": result.get("status"), "reason": result.get("reason", "")}
    problems: list[str] = []
    loop = records.get("warm_loop_cache")
    if loop is not None and loop.status == "pass":
        calls = _main_calls(loop)[1:]
        inputs = sum(int(call.input_tokens or 0) for call in calls)
        reads = sum(int(call.cache_read_tokens or 0) for call in calls)
        observed["warm_loop_share_after_first"] = round(reads / inputs, 3) if inputs else None
        if not inputs or reads / inputs < 0.6:
            problems.append(f"warm loop cache-read share {observed['warm_loop_share_after_first']}")
    else:
        problems.append(f"warm_loop_cache did not pass ({loop.status if loop else 'missing'})")
    long_turn = records.get("long_turn_positions")
    if long_turn is not None and long_turn.status == "pass":
        calls = _main_calls(long_turn)
        misses = [index for index in range(1, len(calls))
                  if int(calls[index].cache_read_tokens or 0) < 0.8 * int(calls[index - 1].input_tokens or 0)]
        observed["long_turn_calls"] = len(calls)
        observed["long_turn_calls_missing_prefix"] = misses
        if misses:
            problems.append(f"long turn calls {misses} did not read the earlier prefix")
    else:
        problems.append(f"long_turn_positions did not pass ({long_turn.status if long_turn else 'missing'})")
    workflow = records.get("workflow_run")
    run = (workflow.metrics.get("workflow") if workflow else None) or {}
    observed["workflow"] = run
    if run.get("calls") and run.get("cache_write_1h_tokens"):
        problems.append("a workflow run wrote 1h cache entries (D3 expects 5m)")
    live.record(ProbeRecord("a3_agent_loop_cache", "anthropic", state.model, "fail" if problems else "pass", observed,
                            note="; ".join(problems)))
    assert not problems, observed


def test_a3_gateway_markers(live: LiveSession) -> None:
    """Spec: wp-A3-handoff: OpenRouter anthropic/* markers honoured; Requesty auto_cache; Requesty manual ttl (probe)."""
    import httpx

    ran = False
    problems: list[str] = []
    if _key("openrouter"):
        ran = True
        model_id = os.environ.get(provider_registry.OPENROUTER_CLAUDE_ENV, provider_registry.OPENROUTER_CLAUDE_DEFAULT)
        llm = _model("openrouter", model_id)
        prefix = _prefix("a3-openrouter")
        with _conversation(f"le_or_{uuid.uuid4().hex[:8]}"), HttpCapture(lambda r: "openrouter.ai" in str(r.url)) as capture:
            first = _usage(llm.invoke(_cached_messages("openrouter", model_id, prefix, "Reply with exactly: one", "1h")))
            second = _usage(llm.invoke(_cached_messages("openrouter", model_id, prefix, "Reply with exactly: one", "1h")))
        body = capture.exchanges[0].request_json or {} if capture.exchanges else {}
        observed = {
            "statuses": [exchange.status for exchange in capture.exchanges],
            "top_level_marker": body.get("cache_control"),
            "system_block_marker": json.dumps(body.get("messages", [{}])[0]).count("cache_control") > 0 if body.get("messages") else False,
            "call1": {"write": first.cache_write_tokens, "read": first.cache_read_tokens, "cost": first.cost},
            "call2": {"write": second.cache_write_tokens, "read": second.cache_read_tokens, "cost": second.cost},
        }
        ok = all(status == 200 for status in observed["statuses"]) and len(observed["statuses"]) == 2 and bool(second.cache_read_tokens)
        if not ok:
            problems.append("openrouter")
        live.record(ProbeRecord("a3_gateway_markers", "openrouter", model_id, "pass" if ok else "fail", observed))
    if _key("requesty"):
        ran = True
        model_id = os.environ.get(provider_registry.REQUESTY_CLAUDE_ENV, provider_registry.REQUESTY_CLAUDE_DEFAULT)
        llm = _model("requesty", model_id)
        prefix = _prefix("a3-requesty")
        with HttpCapture(lambda r: "requesty.ai" in str(r.url)) as capture:
            first = _usage(llm.invoke(_cached_messages("requesty", model_id, prefix, "Reply with exactly: one", "1h")))
            second = _usage(llm.invoke(_cached_messages("requesty", model_id, prefix, "Reply with exactly: one", "1h")))
        body = capture.exchanges[0].request_json or {} if capture.exchanges else {}
        observed = {
            "statuses": [exchange.status for exchange in capture.exchanges],
            "auto_cache_sent": (body.get("requesty") or {}).get("auto_cache"),
            "call1": {"write": first.cache_write_tokens, "read": first.cache_read_tokens},
            "call2": {"write": second.cache_write_tokens, "read": second.cache_read_tokens},
        }
        ok = bool(observed["auto_cache_sent"]) and bool(second.cache_read_tokens)
        if not ok:
            problems.append("requesty auto_cache")
        live.record(ProbeRecord("a3_gateway_markers", "requesty", model_id, "pass" if ok else "fail", observed))
        # Manual block markers with ttl, sent directly (Row-Bot's builder drops block markers on this route).
        manual: dict[str, Any] = {}
        payload = {
            "model": model_id, "max_tokens": 16,
            "messages": [
                {"role": "system", "content": [{"type": "text", "text": _prefix("a3-requesty-manual"),
                                                "cache_control": {"type": "ephemeral", "ttl": "1h"}}]},
                {"role": "user", "content": "Reply with exactly: one"},
            ],
        }
        for attempt in (1, 2):
            response = httpx.post("https://router.requesty.ai/v1/chat/completions", json=payload, timeout=60,
                                  headers={"Authorization": f"Bearer {_key('requesty')}"})
            usage = (response.json() if response.headers.get("content-type", "").startswith("application/json") else {}).get("usage") or {}
            manual[f"call{attempt}"] = {"status": response.status_code, "usage": usage}
            live.carry({"tokens": int(usage.get("total_tokens") or 0), "unpriced_tokens": int(usage.get("total_tokens") or 0), "calls": 1})
        live.record(ProbeRecord("a3_requesty_manual_ttl_probe", "requesty", model_id, "observed", manual,
                                note="decision 2: does manual cache_control with ttl pass through?"))
    if not ran:
        pytest.skip("set OPENROUTER_API_KEY or REQUESTY_API_KEY")
    assert not problems, problems


def test_a3_gateway_acceptance_probe(live: LiveSession) -> None:
    """Spec: wp-A3-handoff: MiniMax, OpenCode-Anthropic and Atlas acceptance of cache markers (recorded only)."""
    ran = False
    if _key("minimax"):
        ran = True
        _probe_anthropic_format_gateway(live, "minimax", provider_registry.target("minimax").model())
    for provider_id in ("opencode_zen", "opencode_go"):
        if _key(provider_id):
            ran = True
            from row_bot import models
            from row_bot.providers.transports.anthropic_cancellable import OpenCodeChatAnthropic

            model_id = os.environ.get(OPENCODE_CLAUDE_ENV, "claude-haiku-4-5")
            try:
                models.refresh_cloud_provider_models(provider_id)
                llm = _model(provider_id, model_id)
            except Exception as exc:
                live.record(ProbeRecord("a3_gateway_acceptance_probe", provider_id, model_id, "skipped",
                                        note=f"{type(exc).__name__}: {exc}"[:300]))
                continue
            if not isinstance(llm, OpenCodeChatAnthropic):
                live.record(ProbeRecord("a3_gateway_acceptance_probe", provider_id, model_id, "skipped",
                                        note=f"{model_id} does not use the Anthropic transport ({type(llm).__name__}); set {OPENCODE_CLAUDE_ENV}"))
                continue
            _probe_anthropic_format_gateway(live, provider_id, model_id, llm=llm)
    if _key("atlascloud"):
        ran = True
        model_id = provider_registry.target("atlascloud").model()
        llm = _model("atlascloud", model_id)
        observed: dict[str, Any] = {}
        endpoint = getattr(llm, "endpoint", None)
        if isinstance(endpoint, dict):
            endpoint["extra_body"] = {**(endpoint.get("extra_body") or {}), "cache_control": {"type": "ephemeral"}}
        prefix = _prefix("a3-atlas")
        from langchain_core.messages import HumanMessage, SystemMessage

        with HttpCapture(lambda r: "atlascloud" in str(r.url)) as capture:
            for attempt in (1, 2):
                try:
                    usage = _usage(llm.invoke([SystemMessage(content=prefix), HumanMessage(content="Reply with exactly: one")]))
                    observed[f"call{attempt}"] = {"read": usage.cache_read_tokens, "write": usage.cache_write_tokens,
                                                  "input": usage.input_tokens}
                except Exception as exc:
                    observed[f"call{attempt}"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        observed["statuses"] = [exchange.status for exchange in capture.exchanges]
        observed["top_level_marker_sent"] = any("cache_control" in (exchange.request_json or {}) for exchange in capture.exchanges)
        live.record(ProbeRecord("a3_gateway_acceptance_probe", "atlascloud", model_id, "observed", observed,
                                note="top-level cache_control only: Row-Bot's Atlas builder drops block markers"))
    if not ran:
        pytest.skip("set MINIMAX_API_KEY, OPENCODE_ZEN_API_KEY / OPENCODE_GO_API_KEY or ATLASCLOUD_API_KEY")


def _probe_anthropic_format_gateway(live: LiveSession, provider_id: str, model_id: str, *, llm: Any = None) -> None:
    llm = llm or _model(provider_id, model_id)
    observed: dict[str, Any] = {}
    try:
        llm.prompt_cache = True  # test-only: the route is off in product until this probe shows acceptance
    except Exception as exc:
        observed["prompt_cache_flag"] = f"could not set: {exc}"[:200]
    prefix = _prefix(f"a3-{provider_id}")
    for ttl in ("5m", "1h"):
        with HttpCapture() as capture:
            for attempt in (1, 2):
                try:
                    usage = _usage(llm.invoke(_cached_messages("anthropic", model_id, prefix, "Reply with exactly: one", ttl)))
                    observed[f"{ttl}_call{attempt}"] = {"read": usage.cache_read_tokens, "write": usage.cache_write_tokens}
                except Exception as exc:
                    observed[f"{ttl}_call{attempt}"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        observed[f"{ttl}_statuses"] = [exchange.status for exchange in capture.exchanges]
    accepted = all(status == 200 for key, value in observed.items() if key.endswith("_statuses") for status in value)
    live.record(ProbeRecord("a3_gateway_acceptance_probe", provider_id, model_id, "observed", observed,
                            note=f"accepted without a 400: {accepted}"))


# ── F17: output-limit stop reasons ──────────────────────────────────────────


def _capped(provider_id: str, llm: Any, tokens: int = 24) -> Any:
    if provider_id == "google":
        return llm.bind(max_output_tokens=tokens)
    if provider_id == "ollama":
        return llm.bind(options={"num_predict": tokens})
    return llm.bind(max_tokens=tokens)


def test_f17_output_limit_detected(live: LiveSession) -> None:
    """Spec: wp-A3-handoff F17; the stop field each provider reports and whether the truncation notice fires."""
    from langchain_core.messages import HumanMessage

    import row_bot.agent as agent

    states = _available()
    if not states:
        pytest.skip("no provider credentials in the environment")
    problems = []
    for state in states:
        pid = state.target.provider_id
        observed: dict[str, Any] = {}
        try:
            reply = _stream(_capped(pid, _model(pid, state.model)), [HumanMessage(content="Write a 300-word story about a lighthouse.")])
            metadata = dict(getattr(reply, "response_metadata", None) or {})
            field_name = next((name for name in ("stop_reason", "finish_reason", "done_reason") if metadata.get(name)), "")
            value = metadata.get(field_name) if field_name else None
            observed = {"field": field_name, "value": value, "notice_fires": value in agent._OUTPUT_LIMIT_STOP_REASONS}
        except Exception as exc:
            observed = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        if pid == "anthropic" and not observed.get("notice_fires"):
            problems.append(observed)
        live.record(ProbeRecord("f17_output_limit_detected", pid, state.model,
                                "pass" if observed.get("notice_fires") else "observed", observed))
    assert not problems, problems


# ── B1: usage shapes ────────────────────────────────────────────────────────


def test_b1_usage_shapes(live: LiveSession) -> None:
    """Spec: wp-B1-handoff 'Provider shapes not verified offline' and 'Manual checks'."""
    from langchain_core.messages import HumanMessage

    states = _available()
    if not states:
        pytest.skip("no provider credentials in the environment")
    problems = []
    for state in states:
        pid = state.target.provider_id
        baseline = _baseline(live)
        observed: dict[str, Any] = {}
        try:
            llm = _model(pid, state.model)
            llm.invoke([HumanMessage(content="Reply with exactly: counted")])
            _stream(llm, [HumanMessage(content="Reply with exactly: counted")])
        except Exception as exc:
            observed["error"] = f"{type(exc).__name__}: {exc}"[:300]
        rows = [row for row in _ledger_rows_since(live, baseline) if row.provider == pid or pid == "ollama"]
        observed["rows"] = [
            {key: getattr(row, key) for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                                                "reasoning_tokens", "cost", "ttft_ms")}
            for row in rows
        ]
        complete = len(rows) >= 2 and all(row.input_tokens is not None and row.output_tokens is not None for row in rows)
        if pid == "openrouter":
            costs = [row.cost for row in rows]
            observed["cost_present"] = all(cost is not None for cost in costs)
            tokens = sum(int(row.input_tokens or 0) + int(row.output_tokens or 0) for row in rows)
            observed["cost_per_1k_tokens"] = round(sum(c for c in costs if c) / tokens * 1000, 6) if tokens else None
            complete = complete and observed["cost_present"]
        if not complete and "error" not in observed:
            problems.append(pid)
        live.record(ProbeRecord("b1_usage_shapes", pid, state.model, "pass" if complete else "fail", observed))
    assert not problems, f"calls without counts in the ledger: {problems}"


def test_b1_ollama_prompt_eval_reuse(live: LiveSession) -> None:
    """Spec: wp-B1-handoff: does prompt_eval_count count only new tokens when Ollama reuses its cache? (probe)"""
    from langchain_core.messages import HumanMessage, SystemMessage

    from row_bot import models

    _need("ollama")
    model_id = provider_registry.availability(provider_registry.target("ollama")).model
    models.adopt_saved_default(f"model:ollama:{model_id}")
    llm = _model("ollama", model_id)
    prefix = filler(8_000, seed="b1-ollama")
    with HttpCapture(lambda r: "/api/chat" in str(r.url)) as capture:
        for question in ("Reply with exactly: one", "Reply with exactly: two"):
            llm.invoke([SystemMessage(content=prefix), HumanMessage(content=question)])
    counts = []
    for exchange in capture.exchanges:
        final = next((line for line in reversed(exchange.ndjson()) if line.get("done")), {})
        counts.append(final.get("prompt_eval_count"))
    live.record(ProbeRecord("b1_ollama_prompt_eval_reuse", "ollama", model_id, "observed",
                            {"prompt_eval_counts": counts,
                             "second_counts_only_new_tokens": len(counts) == 2 and None not in counts and counts[1] < counts[0] / 2}))


def test_b1_deepseek_hit_miss(live: LiveSession) -> None:
    """Spec: wp-B1-handoff: DeepSeek prompt_cache_hit_tokens/miss reach the ledger (key held in memory only)."""
    from langchain_core.messages import HumanMessage, SystemMessage

    from row_bot.providers import auth_store, config as provider_config
    from row_bot.providers.custom import normalize_custom_endpoint

    _need("deepseek")
    endpoint = normalize_custom_endpoint({
        "id": "live-eval-deepseek", "base_url": "https://api.deepseek.com/v1", "profile": "generic_openai",
        "auth_required": True, "models": [{"id": "deepseek-chat"}],
    })
    provider_id = endpoint["provider_id"]
    saved = provider_config.load_provider_config()
    provider_config.save_provider_config({**saved, "custom_endpoints": [*(saved.get("custom_endpoints") or []), endpoint]})
    auth_store._set_session_provider_secret(provider_id, "api_key", _key("deepseek"))
    try:
        from row_bot.providers.runtime import create_chat_model

        llm = create_chat_model("deepseek-chat", provider_id)
        prefix = filler(12_000, seed="b1-deepseek")
        usages = []
        for _ in range(2):
            usages.append(_usage(llm.invoke([SystemMessage(content=prefix), HumanMessage(content="Reply with exactly: one")])))
            time.sleep(3)  # DeepSeek's disk cache needs a moment to register a prefix
    finally:
        auth_store._delete_session_provider_secret(provider_id, "api_key")
        provider_config.save_provider_config(saved)
    observed = {"calls": [{"input": u.input_tokens, "cache_read": u.cache_read_tokens} for u in usages]}
    ok = usages[1].cache_read_tokens is not None and usages[1].cache_read_tokens > 0
    live.record(ProbeRecord("b1_deepseek_hit_miss", "deepseek", "deepseek-chat", "pass" if ok else "fail", observed))
    assert ok, observed


# ── B2: cache keys and reasoning replay ─────────────────────────────────────


def test_b2_openai_cache_key_and_store(live: LiveSession) -> None:
    """Spec: wp-B2-handoff 'Manual checks' OpenAI: key on both APIs, store:false + include, cached tokens."""
    from langchain_core.messages import HumanMessage, SystemMessage

    _need("openai")
    problems = []
    for model_id, responses_api in ((provider_registry.target("openai").model(), True),
                                    (os.environ.get(OPENAI_CHAT_MODEL_ENV, "gpt-4.1-mini"), False)):
        llm = _model("openai", model_id)
        prefix = _prefix("b2-openai")
        usages = []
        with _conversation(f"le_oa_{uuid.uuid4().hex[:8]}"), HttpCapture(lambda r: "api.openai.com" in str(r.url)) as capture:
            for _ in range(2):
                usages.append(_usage(llm.invoke([SystemMessage(content=prefix), HumanMessage(content="Reply with exactly: one")])))
        bodies = [exchange.request_json or {} for exchange in capture.exchanges]
        keys = [body.get("prompt_cache_key") for body in bodies]
        observed = {
            "statuses": [exchange.status for exchange in capture.exchanges],
            "prompt_cache_key_sent": all(keys) and len(set(keys)) == 1,
            "store": bodies[0].get("store") if bodies else None,
            "include": bodies[0].get("include") if bodies else None,
            "cached_tokens": [u.cache_read_tokens for u in usages],
        }
        ok = observed["prompt_cache_key_sent"] and all(status == 200 for status in observed["statuses"])
        if responses_api:
            ok = ok and observed["store"] is False and "reasoning.encrypted_content" in (observed["include"] or [])
        if not ok:
            problems.append(model_id)
        live.record(ProbeRecord("b2_openai_cache_key_and_store", "openai", model_id, "pass" if ok else "fail", observed))
    assert not problems, problems


def test_b2_openai_encrypted_rejection(live: LiveSession, caplog: pytest.LogCaptureFixture) -> None:
    """Spec: wp-B2-handoff: a rejected replayed ciphertext is retried once without it; the 400 names 'encrypted'."""
    from langchain_core.messages import AIMessage, HumanMessage

    _need("openai")
    caplog.set_level(logging.INFO, logger="row_bot")
    model_id = provider_registry.target("openai").model()
    llm = _model("openai", model_id)
    garbled = AIMessage(content=[
        {"type": "reasoning", "id": "rs_" + uuid.uuid4().hex + uuid.uuid4().hex[:16], "summary": [],
         "encrypted_content": "gAAAAAB" + "x" * 120},
        {"type": "text", "text": "Hello."},
    ])
    with _conversation(f"le_enc_{uuid.uuid4().hex[:8]}"), HttpCapture(lambda r: "api.openai.com" in str(r.url)) as capture:
        reply = llm.invoke([HumanMessage(content="Say hello."), garbled, HumanMessage(content="Reply with exactly: recovered")])
    statuses = [exchange.status for exchange in capture.exchanges]
    first_error = capture.exchanges[0].text()[:400] if capture.exchanges and statuses[0] == 400 else ""
    observed = {"statuses": statuses, "first_error": first_error,
                "retries_logged": caplog.text.count("replayed reasoning was rejected"),
                "answered": "recovered" in str(reply.content).lower()}
    ok = statuses[:1] == [400] and statuses[-1] == 200 and observed["retries_logged"] == 1 and "encrypted" in first_error.lower()
    live.record(ProbeRecord("b2_openai_encrypted_rejection", "openai", model_id, "pass" if ok else "fail", observed))
    assert ok, observed


def test_b2_xai_conversation_header(live: LiveSession) -> None:
    """Spec: wp-B2-handoff xAI: x-grok-conv-id per conversation; does it raise cached tokens? (probe)"""
    from langchain_core.messages import HumanMessage, SystemMessage

    _need("xai")
    model_id = provider_registry.target("xai").model()
    llm = _model("xai", model_id)
    prefix = _prefix("b2-xai")
    run_tag = uuid.uuid4().hex[:8]
    calls = []
    with HttpCapture(lambda r: "api.x.ai" in str(r.url)) as capture:
        for thread in ("A", "A", "B"):
            with _conversation(f"le_xai_{thread}_{run_tag}"):
                usage = _usage(llm.invoke([SystemMessage(content=prefix), HumanMessage(content="Reply with exactly: one")]))
                calls.append({"conversation": thread, "cache_read": usage.cache_read_tokens, "input": usage.input_tokens})
    headers = [exchange.request_headers.get("x-grok-conv-id") for exchange in capture.exchanges]
    observed = {"calls": calls, "header_sent": all(headers), "same_header_within_conversation": len(headers) >= 2 and headers[0] == headers[1],
                "different_header_across_conversations": len(headers) >= 3 and headers[2] != headers[0]}
    live.record(ProbeRecord("b2_xai_conversation_header", "xai", model_id,
                            "pass" if observed["header_sent"] else "fail", observed))
    assert observed["header_sent"] and observed["same_header_within_conversation"], observed


def test_b2_openrouter_session(live: LiveSession) -> None:
    """Spec: wp-B2-handoff OpenRouter: session_id keeps a conversation on one upstream (probe)."""
    from langchain_core.messages import HumanMessage, SystemMessage

    _need("openrouter")
    model_id = os.environ.get(provider_registry.OPENROUTER_CLAUDE_ENV, provider_registry.OPENROUTER_CLAUDE_DEFAULT)
    llm = _model("openrouter", model_id)
    prefix = _prefix("b2-openrouter")
    reads = []
    with _conversation(f"le_ors_{uuid.uuid4().hex[:8]}"), HttpCapture(lambda r: "openrouter.ai" in str(r.url)) as capture:
        for _ in range(2):
            reads.append(_usage(llm.invoke([SystemMessage(content=prefix), HumanMessage(content="Reply with exactly: one")])).cache_read_tokens)
    sessions = [(exchange.request_json or {}).get("session_id") for exchange in capture.exchanges]
    upstreams = []
    for exchange in capture.exchanges:
        try:
            upstreams.append(exchange.json().get("provider"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            events = exchange.sse_events()
            upstreams.append(next((event.get("provider") for event in events if event.get("provider")), None))
    observed = {"session_id_sent": all(sessions) and len(set(sessions)) == 1, "upstreams": upstreams, "cache_reads": reads}
    live.record(ProbeRecord("b2_openrouter_session", "openrouter", model_id,
                            "pass" if observed["session_id_sent"] else "fail", observed))
    assert observed["session_id_sent"], observed


def test_b2_ollama_keep_alive(live: LiveSession) -> None:
    """Spec: wp-B2-handoff Ollama: 30-minute keep-alive and no reload across call paths (eval task ollama_no_reload)."""
    _need("ollama")
    state = provider_registry.availability(provider_registry.target("ollama"))
    result = orchestrate.run_provider(state, ["ollama_no_reload"], live.run_dir, live.guard(), label="checks")
    live.carry(result.get("spent") or {})
    task = next(iter(result.get("tasks") or []), {})
    observed = (task.get("metrics") or {}).get("ollama") or {"reason": task.get("reason") or result.get("reason")}
    live.record(ProbeRecord("b2_ollama_keep_alive", "ollama", state.model, task.get("status", "error"), observed))
    assert task.get("status") == "pass", observed


# ── C1: structured output for background calls ─────────────────────────────


def _c1_cases() -> list[tuple[str, str, Any, list]]:
    from langchain_core.messages import HumanMessage, SystemMessage

    from row_bot import dream_cycle, goals, memory_extraction
    from row_bot.providers.background import structured_answer
    from row_bot.tools import memory_tool

    return [
        ("extraction", "extraction", memory_extraction._extraction_answer(), [
            SystemMessage(content="Extract durable memories from the conversation as JSON matching the schema."),
            HumanMessage(content="User: My sister Ana lives in Lisbon and works as a nurse.")]),
        ("insights", "dream", dream_cycle._insights_answer(), [
            HumanMessage(content="System snapshot: 0 errors last week, 12 memories, 2 unused skills. Return insights as JSON.")]),
        ("condition", "condition", structured_answer("condition", {"answer": {"type": "string", "enum": ["yes", "no"]}}), [
            SystemMessage(content="Answer only yes or no. Do not call tools."), HumanMessage(content="Is 10 greater than 3?")]),
        ("verifier", "verifier", goals._verifier_answer(), [
            HumanMessage(content="Goal: write a haiku about rain. Last step: the haiku was written and shown. Judge progress.")]),
        ("contradiction", "contradiction", memory_tool._contradiction_answer(), [
            HumanMessage(content="Stored: Ana lives in Lisbon. New: Ana lives in Porto. Do they contradict?")]),
    ]


def _effort_sent(provider_id: str, body: dict[str, Any]) -> Any:
    if provider_id == "anthropic":
        return {"effort": (body.get("output_config") or {}).get("effort"), "thinking": body.get("thinking")}
    if provider_id in {"openai", "xai"}:
        return body.get("reasoning") or body.get("reasoning_effort")
    if provider_id == "google":
        return (body.get("generationConfig") or body.get("generation_config") or {}).get("thinkingConfig")
    if provider_id == "ollama":
        return {"think": body.get("think")}
    return None


def test_c1_structured_output(live: LiveSession) -> None:
    """Spec: wp-C1-handoff decisions 1-2: schemas accepted per provider; effort used; reasoning against the headroom."""
    from row_bot import models
    from row_bot.providers import background
    from tests.e2e.live_eval.tasks import parse_json_answer

    states = _available(("anthropic", "openai", "google", "xai", "ollama"))
    if not states:
        pytest.skip("set ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY, XAI_API_KEY or a local Ollama model")
    problems = []
    for state in states:
        pid = state.target.provider_id
        ref = f"model:{pid}:{state.model}"
        models.adopt_saved_default(ref)
        for name, purpose, answer, messages in _c1_cases():
            baseline = _baseline(live)
            observed: dict[str, Any] = {}
            with HttpCapture() as capture:
                try:
                    text = background.invoke_background(purpose, messages, answer=answer, thread_model=ref)
                    parsed = parse_json_answer(text)
                    required = list((answer.schema.get("properties") or {}).keys())
                    observed["valid"] = all(key in parsed for key in required)
                except Exception as exc:
                    observed["valid"] = False
                    observed["error"] = f"{type(exc).__name__}: {exc}"[:300]
            refused = ref in background._schema_refusals
            body = next((exchange.request_json for exchange in capture.exchanges if isinstance(exchange.request_json, dict)), {}) or {}
            rows = [row for row in _ledger_rows_since(live, baseline) if row.purpose == purpose]
            observed.update({
                "schema_refused": refused,
                "effort_sent": _effort_sent(pid, body),
                "reasoning_tokens": [row.reasoning_tokens for row in rows],
                "output_tokens": [row.output_tokens for row in rows],
            })
            if refused or not observed["valid"]:
                problems.append(f"{pid}/{name}")
            live.record(ProbeRecord("c1_structured_output", pid, f"{state.model} {name}",
                                    "fail" if refused or not observed["valid"] else "pass", observed))
    assert not problems, problems


# ── A4b: near-limit compaction and reactive overflow ────────────────────────


def test_a4b_near_limit_and_overflow(live: LiveSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec: PLAN WP B3 / A4b. Expected to fail before A4b merges; runs only when asked for."""
    if os.environ.get(A4B_ENV) != "1":
        pytest.skip(f"set {A4B_ENV}=1 (after A4b merges) and {A4B_MODEL_ENV}=model:<provider>:<model with a small window>")
    ref = os.environ.get(A4B_MODEL_ENV, "")
    try:
        _, provider_id, model_id = ref.split(":", 2)
    except ValueError:
        pytest.skip(f"{A4B_MODEL_ENV} must look like model:<provider>:<model>")
    _need(provider_id)
    window = int(os.environ.get(A4B_WINDOW_ENV, "") or 32_768)
    chapter_chars = 10_000
    turns = math.ceil(window * 1.3 / (chapter_chars / 3.5)) + 1
    monkeypatch.setenv("ROW_BOT_LIVE_EVAL_LONG_WINDOW", str(window))
    monkeypatch.setenv("ROW_BOT_LIVE_EVAL_LONG_TURNS", str(turns))
    monkeypatch.setenv("ROW_BOT_LIVE_EVAL_LONG_CHAPTER_CHARS", str(chapter_chars))
    state = provider_registry.Availability(provider_registry.target(provider_id), model_id, True)
    observed: dict[str, Any] = {"window": window, "turns": turns}
    for label, variant in (("near_limit", ""), ("reactive_overflow", "inflate-context=4")):
        result = orchestrate.run_provider(state, ["long_thread_compaction"], live.run_dir, live.guard(),
                                          variant_text=variant, label=f"a4b-{label}")
        live.carry(result.get("spent") or {})
        task = next(iter(result.get("tasks") or []), {})
        observed[label] = {"status": task.get("status"), "reason": task.get("reason") or result.get("reason"),
                           "compactions": (task.get("by_purpose") or {}).get("compaction", 0),
                           "errors": (task.get("errors") or [])[:2],
                           "expected_words": [chapter_word(index) for index in range(min(turns, 3))]}
    ok = all((observed[label]["status"] == "pass") for label in ("near_limit", "reactive_overflow"))
    live.record(ProbeRecord("a4b_near_limit_and_overflow", provider_id, model_id, "pass" if ok else "fail", observed))
    assert ok, observed
