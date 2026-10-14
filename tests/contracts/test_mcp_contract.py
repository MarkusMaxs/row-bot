from __future__ import annotations

import pytest

from tests.fixtures.mcp import FakeCallResult, FakeContentBlock, FakeMcpTool


pytestmark = pytest.mark.contract


def test_mcp_tool_normalization_applies_prefixes_and_destructive_defaults() -> None:
    from row_bot.mcp_client import runtime

    tools = runtime._normalize_tools(
        "File Server",
        {"tools": {"enabled": {"delete_file": True}, "require_approval": ["read_file"]}},
        [
            FakeMcpTool("read_file", "Read file", {"type": "object", "properties": {"path": {"type": "string"}}}),
            FakeMcpTool("delete_file", "Delete file", {"type": "object"}),
        ],
    )

    assert tools["read_file"].prefixed_name == "mcp_file_server_read_file"
    assert tools["read_file"].enabled is True
    assert tools["read_file"].requires_approval is True
    assert tools["delete_file"].destructive is True
    assert tools["delete_file"].enabled is True
    assert tools["delete_file"].requires_approval is True


def test_mcp_result_normalization_handles_errors_structured_content_and_truncation() -> None:
    from row_bot.mcp_client.results import normalize_call_result

    result = FakeCallResult(
        content=[FakeContentBlock("text", "hello world")],
        structuredContent={"ok": True, "value": 3},
        isError=True,
    )

    text = normalize_call_result(result, output_limit=50)

    assert text.startswith("MCP tool error: hello world")
    assert "STRUCTURED_CONTENT" in text
    assert "[Truncated MCP output at 50 characters]" in text


def test_mcp_langchain_wrappers_are_built_from_injected_catalog(monkeypatch) -> None:
    from row_bot.mcp_client import runtime

    server_cfg = {"enabled": True, "transport": "stdio", "tools": {"enabled": {"read_file": True}}}
    monkeypatch.setattr(runtime.mcp_config, "is_globally_enabled", lambda: True)
    monkeypatch.setattr(runtime.mcp_config, "get_config", lambda: {"enabled": True, "servers": {"fake": server_cfg}})
    monkeypatch.setattr(runtime, "discover_enabled_servers", lambda: None)

    with runtime._runtime_lock:
        runtime._catalog.clear()
        runtime._servers.clear()
        runtime._catalog["fake"] = runtime._normalize_tools(
            "fake",
            server_cfg,
            [FakeMcpTool("read_file", "Read a file", {"type": "object", "properties": {"path": {"type": "string"}}})],
        )

    tools = runtime.get_langchain_tools()

    assert [tool.name for tool in tools] == ["mcp_fake_read_file"]
    assert runtime.get_destructive_tool_names() == set()

    with runtime._runtime_lock:
        runtime._catalog.clear()
        runtime._servers.clear()


@pytest.mark.parametrize(("name", "description", "annotations", "effect", "high_impact"), [
    # 1. A destructiveHint is destructive.
    ("lookup", "Look a record up", {"destructiveHint": True}, "mutation", True),
    # 2. A readOnlyHint is read-only whatever the description says (B307)...
    ("check_stock", "Execute a stock check", {"readOnlyHint": True}, "read_only", False),
    # ...but never outweighs a name that changes things.
    ("delete_order", "", {"readOnlyHint": True}, "mutation", True),
    ("save_purchase_orders", "", {"readOnlyHint": True}, "mutation", False),
    # 3. Otherwise the name's verb, above its description (B306).
    ("read_query", "Execute a SELECT query on the SQLite database", None, "read_only", False),
    ("search_docs", "Run a search over the docs", None, "read_only", False),
    ("append_insight", "Add a business insight to the memo", None, "mutation", False),
    ("save_purchase_orders", "Save purchase orders to the ledger", {"readOnlyHint": False}, "mutation", False),
    ("write_query", "Execute an INSERT, UPDATE, or DELETE query on the SQLite database", None, "mutation", True),
    # ...unless the server says it isn't destructive: a change's description then can't make it high impact,
    ("create_pages", "Create pages and share them with your team", None, "mutation", True),
    ("create_pages", "Create pages and share them with your team", {"destructiveHint": False}, "mutation", False),
    # but its name still can.
    ("delete_pages", "Delete pages", {"destructiveHint": False}, "mutation", True),
    # 4. The description only when neither says; a read whose description says it changes anything asks.
    ("get_or_make_page", "Update the page, creating it when missing", None, "unknown", False),
    ("query", "Execute any SQL statement against the database", None, "unknown", False),
    ("query", "Execute a GraphQL query or mutation", None, "unknown", False),
    ("lookup", "Look up an order and refund it", None, "unknown", False),
    ("search", "Search issues and comment on them", None, "unknown", False),
    ("query", "Executes any SQL statement", None, "unknown", False),  # A description is prose: any form counts.
    ("query", "Runs arbitrary SQL, including inserts, updates and deletes", None, "unknown", False),
    ("read_query", "Executes a SELECT query, then drops the table", None, "unknown", False),
    ("search_files", "Run a search and replace across files", None, "unknown", False),
    ("query", "Execute a SELECT INTO statement", None, "unknown", False),
    ("list_orders", "List recent orders", None, "read_only", False),  # Nouns a read describes stay reads.
    ("get_posts", "Get the latest posts and their comments", None, "read_only", False),
    ("terminal", "Execute a shell command", None, "mutation", True),
    ("frobnicate", "Does a thing", None, "unknown", False),
])
def test_tool_classification_weighs_annotations_then_names_then_descriptions(name, description, annotations, effect,
                                                                             high_impact) -> None:
    from row_bot.mcp_client.safety import classify_tool_effect, is_destructive_tool

    tool = {"annotations": annotations} if annotations else None
    assert classify_tool_effect(name, description, tool) == effect
    assert is_destructive_tool(name, description, tool) is high_impact


def _stdio_launch_environment(monkeypatch, cfg: dict) -> dict[str, str]:
    """The environment a stdio server would start with, captured instead of starting it."""
    import asyncio
    from contextlib import asynccontextmanager

    from row_bot.mcp_client import runtime

    monkeypatch.setattr(runtime, "_resolve_stdio_command", lambda command, _env: command)
    captured = []

    @asynccontextmanager
    async def stdio(params, errlog=None):
        captured.append(params)
        yield (object(), object())

    class Session:
        def __init__(self, *_args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def initialize(self):
            return None

    monkeypatch.setattr(runtime, "stdio_client", stdio)
    monkeypatch.setattr(runtime, "ClientSession", Session)
    server = runtime.McpServerRuntime("fixture", {"command": "fixture-server", **cfg})

    async def connect():
        await server._connect()
        await server.exit_stack.aclose()

    asyncio.run(connect())
    return dict(captured[0].env)


@pytest.fixture
def host_secrets(monkeypatch):
    """Row-Bot's environment on a self-hosted install: provider and channel keys, and its own secret."""
    for name, value in {"OPENAI_API_KEY": "sk-external-synthetic", "TELEGRAM_BOT_TOKEN": "telegram-synthetic",
                        "ROW_BOT_LAUNCH_SECRET": "launch-secret-synthetic", "WORKSPACE_FLAVOUR": "kept"}.items():
        monkeypatch.setenv(name, value)


def test_a_stdio_server_starts_without_row_bots_keys_unless_it_is_given_them(monkeypatch, host_secrets) -> None:
    environment = _stdio_launch_environment(monkeypatch, {"env": {"SERVER_API_KEY": "explicit-synthetic"}})

    assert not {"OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN", "ROW_BOT_LAUNCH_SECRET"} & set(environment)
    assert environment["SERVER_API_KEY"] == "explicit-synthetic"  # Its own variables, given explicitly.
    assert environment["WORKSPACE_FLAVOUR"] == "kept"


def test_a_stdio_server_inherits_only_the_keys_it_opts_into(monkeypatch, host_secrets) -> None:
    environment = _stdio_launch_environment(monkeypatch, {"inherit_env": ["OPENAI_API_KEY"]})

    assert environment["OPENAI_API_KEY"] == "sk-external-synthetic"
    assert "TELEGRAM_BOT_TOKEN" not in environment and "ROW_BOT_LAUNCH_SECRET" not in environment


def test_a_servers_opt_in_keeps_only_variable_names() -> None:
    from row_bot.mcp_client.config import normalize_server_config

    cfg = normalize_server_config("fixture", {"command": "fixture-server",
                                              "inherit_env": ["OPENAI_API_KEY", "BAD NAME", 7, "$(x)"]})

    assert cfg["inherit_env"] == ["OPENAI_API_KEY"]
    assert "inherit_env" not in normalize_server_config("fixture", {"command": "fixture-server"})
