from __future__ import annotations

import sys
import types
from dataclasses import replace

import pytest

from tests.fixtures.providers import FakeChatModel, fake_model_info


pytestmark = pytest.mark.contract


def test_fake_chat_model_contract_is_deterministic() -> None:
    model = FakeChatModel(model="contract-model", responses=["first", "second"], stream_chunks=["a", "b"])

    assert model.invoke("hello").content == "first"
    assert model.invoke([("user", "hello again")]).content == "second"
    assert [chunk.content for chunk in model.stream("stream please")] == ["a", "b"]
    assert model.bind_tools([{"name": "fake_tool"}]) is model
    assert model.bound_tools == [{"name": "fake_tool"}]
    assert len(model.invocations) == 3


def test_model_info_cache_round_trips_capability_snapshot() -> None:
    from row_bot.providers.catalog import model_info_from_legacy, model_info_to_cache_entry

    generation_parameters = {
        "options": {"quality": ["low", "medium"], "resolution": ["1k", "2k"]},
        "defaults": {"quality": "medium", "resolution": "1k"},
        "valid_combinations": [
            {"quality": "low", "resolution": "1k"},
            {"quality": "medium", "resolution": "2k"},
        ],
    }
    info = replace(
        fake_model_info(provider_id="openai", model_id="gpt-4o"),
        generation_parameters=generation_parameters,
    )
    entry = model_info_to_cache_entry(info)
    restored = model_info_from_legacy("model:openai:gpt-4o", entry)

    assert restored is not None
    assert restored.selection_ref == "model:openai:gpt-4o"
    assert restored.context_window == 8192
    assert restored.tool_calling is True
    assert restored.streaming is True
    assert "image" in restored.input_modalities
    assert entry["capabilities_snapshot"]["generation_parameters"] == generation_parameters
    assert restored.generation_parameters == generation_parameters


def test_custom_fake_provider_runtime_uses_fake_transport(tmp_path, monkeypatch) -> None:
    from row_bot.providers import config as provider_config
    from row_bot.providers import custom, runtime
    from row_bot.providers.custom import custom_provider_id

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(runtime, "provider_secret_status", lambda *_args, **_kwargs: {"configured": False, "source": "", "fingerprint": ""})

    provider_id = custom_provider_id("contract-fake")
    custom.save_custom_endpoint(
        {
            "id": "contract-fake",
            "name": "Contract Fake",
            "base_url": "http://127.0.0.1:65535/v1",
            "auth_required": False,
            "models": [
                {
                    "id": "fake-chat",
                    "model_id": "fake-chat",
                    "display_name": "Fake Chat",
                    "context_window": 4096,
                    "capabilities_snapshot": fake_model_info(provider_id=provider_id).capability_snapshot(),
                }
            ],
        }
    )

    fake_transport = types.ModuleType("row_bot.providers.transports.openai_compatible")
    fake_transport.ChatOpenAICompatible = FakeChatModel
    monkeypatch.setitem(sys.modules, "row_bot.providers.transports.openai_compatible", fake_transport)

    status = runtime.provider_status(provider_id)
    chat_model = runtime.create_chat_model("fake-chat", provider_id=provider_id)

    assert status["configured"] is True
    assert status["source"] == "no_auth"
    assert isinstance(chat_model, FakeChatModel)
    assert chat_model.model == "fake-chat"
    assert chat_model.kwargs["base_url"] == "http://127.0.0.1:65535/v1"


def test_provider_error_contract_normalizes_common_failures() -> None:
    from row_bot.providers.errors import ProviderErrorKind, normalize_provider_error

    assert normalize_provider_error(ValueError("401 unauthorized API key")).kind == ProviderErrorKind.AUTHENTICATION
    assert normalize_provider_error(RuntimeError("quota exceeded")).kind == ProviderErrorKind.QUOTA_EXHAUSTED
    assert normalize_provider_error(RuntimeError("tools are not supported")).kind == ProviderErrorKind.UNSUPPORTED_CAPABILITY


def test_every_builtin_provider_has_refresh_ownership_or_an_explicit_exemption() -> None:
    from row_bot.models import REFRESHABLE_CLOUD_PROVIDER_IDS
    from row_bot.providers.catalog import list_provider_definitions

    provider_ids = {definition.id for definition in list_provider_definitions()}
    explicit_exemptions = {"ollama"}

    assert set(REFRESHABLE_CLOUD_PROVIDER_IDS).isdisjoint(explicit_exemptions)
    assert set(REFRESHABLE_CLOUD_PROVIDER_IDS) | explicit_exemptions == provider_ids


def _counting_clock(monkeypatch, step: float = 0.01) -> None:
    """Each look at the clock moves it on, so a long stream spans many seconds without real waiting."""
    from row_bot.providers import runtime

    now = {"t": 0.0}

    def monotonic() -> float:
        now["t"] += step
        return now["t"]

    monkeypatch.setattr(runtime, "time", types.SimpleNamespace(monotonic=monotonic))


def test_a_token_by_token_local_stream_rechecks_authority_about_once_a_second(monkeypatch) -> None:
    """B288: a streaming Ollama sends one chunk per token; the per-response authority check is costly."""
    from row_bot.providers.runtime import captured_http_clients
    from tests.helpers.fake_ollama import fake_streaming_ollama

    _counting_clock(monkeypatch)
    checks = []
    with fake_streaming_ollama(tokens=[f"w{i} " for i in range(400)]) as server:
        with captured_http_clients(server.url, lambda: checks.append(1)) as (sync, _async):
            response = sync.post(f"{server.url}/api/chat", json={"model": server.model, "stream": True})
            lines = [line for line in response.iter_lines() if line]

    assert len(lines) == 401
    assert 3 <= len(checks) <= 25  # request, response, about one a second of the stream, and the end


def test_revoking_authority_still_stops_a_stream_part_way(monkeypatch) -> None:
    from row_bot.providers.runtime import captured_http_clients
    from tests.helpers.fake_ollama import fake_streaming_ollama

    _counting_clock(monkeypatch)
    checks = []

    def validate() -> None:
        checks.append(1)
        if len(checks) > 4:
            raise PermissionError("document authority revoked")

    with fake_streaming_ollama(tokens=[f"w{i} " for i in range(400)]) as server:
        with captured_http_clients(server.url, validate) as (sync, _async):
            read = []
            with pytest.raises(PermissionError):
                with sync.stream("POST", f"{server.url}/api/chat", json={"model": server.model}) as response:
                    for line in response.iter_lines():
                        read.append(line)
    assert 0 < len(read) < 400


def _usage_shapes() -> dict:
    import json
    from pathlib import Path

    payloads = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures" / "provider_usage_payloads.json").read_text(encoding="utf-8")
    )
    return {name: value for name, value in payloads.items() if not name.startswith("_")}


@pytest.mark.parametrize("shape", sorted(_usage_shapes()))
def test_every_usage_shape_keeps_one_meaning_for_input_and_output(shape) -> None:
    """Input always includes cache reads and writes; output always includes reasoning.

    The usage ledger, the context meter and compaction calibration all rely on
    this, whatever provider or transport reported the counts.
    """
    from row_bot.prompt_cache import normalize_model_usage

    payload = _usage_shapes()[shape]
    usage = normalize_model_usage(payload["response_metadata"], payload["usage_metadata"])

    if usage.input_tokens is not None:
        assert usage.input_tokens >= (usage.cache_read_tokens or 0) + (usage.cache_write_tokens or 0)
    if usage.output_tokens is not None:
        assert usage.output_tokens >= (usage.reasoning_tokens or 0)
    if usage.cache_write_1h_tokens is not None:
        assert usage.cache_write_tokens is not None and usage.cache_write_1h_tokens <= usage.cache_write_tokens
    assert usage.cost is None or usage.cost >= 0


def test_a_conversation_has_one_opaque_cache_key_per_provider(monkeypatch) -> None:
    """Cache routing keys (WP B2): stable within a conversation, unrelated across
    conversations and providers, never the conversation id, and absent outside one."""
    from row_bot.providers.cache_affinity import conversation_cache_key

    current = {"thread": ""}
    monkeypatch.setitem(
        sys.modules, "row_bot.agent", types.SimpleNamespace(get_current_thread_id=lambda: current["thread"])
    )

    def key(thread: str, provider: str) -> str | None:
        current["thread"] = thread
        return conversation_cache_key(provider)

    conversation = "whatsapp:15550001111"
    first = key(conversation, "openai")

    assert first == key(conversation, "openai")
    assert first != key("another-conversation", "openai")
    assert first != key(conversation, "openrouter")
    assert "15550001111" not in first
    assert len(first) <= 64  # OpenAI's prompt_cache_key limit; OpenRouter's session_id allows 256
    assert key("", "openai") is None


def test_every_request_for_a_local_chat_model_keeps_its_context_size_and_lifetime(monkeypatch) -> None:
    """Ollama reloads a runner, losing its prompt cache, when num_ctx changes (WP B2, F13).

    The agent, background jobs and Chat Only build the model by different routes,
    and some calls pass their own options; every request still carries the same
    num_ctx and the chat keep_alive.
    """
    from langchain_core.messages import HumanMessage

    import row_bot.models as models
    from row_bot.providers import runtime
    from row_bot.providers.transports.ollama_local import OLLAMA_CHAT_KEEP_ALIVE

    monkeypatch.delenv("OLLAMA_KEEP_ALIVE", raising=False)
    monkeypatch.setattr(models, "_ollama_base_url", lambda: "http://127.0.0.1:9")  # nothing listens
    monkeypatch.setattr(models, "_num_ctx", 16_384)
    monkeypatch.setattr(models, "_local_context_mode", "fixed")
    monkeypatch.setattr(models, "get_model_max_context", lambda *args, **kwargs: 8_192)
    monkeypatch.setattr(models, "_observed_local_context", {})
    monkeypatch.setattr(models, "_override_llm_cache", {})

    built = [
        models.get_llm_for("model:ollama:contract-local:7b"),
        runtime.create_chat_model("model:ollama:contract-local:7b"),
    ]
    prompt = [HumanMessage(content="hi")]
    requests = [
        params
        for model in built
        for params in (model._chat_params(prompt), model._chat_params(prompt, options={"num_predict": 8}))
    ]

    assert {params["options"]["num_ctx"] for params in requests} == {8_192}
    assert {params["keep_alive"] for params in requests} == {OLLAMA_CHAT_KEEP_ALIVE}
