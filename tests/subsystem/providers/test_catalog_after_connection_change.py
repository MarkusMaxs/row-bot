"""Settings › Models shows a provider connected or disconnected at once (B411).

Right after a ChatGPT / Codex sign-in, Settings › Models still listed only the
providers of the last refresh until the person pressed Refresh. Now a finished
sign-in (or a key saved) refreshes that provider's catalog in the background,
a disconnect (or a key cleared) changes only what is ready, and open Models
pages hear either on the ``models`` live topic. The provider's model list is
faked: nothing reaches the network.
"""
from __future__ import annotations

import threading
import time

import pytest

from row_bot.application import live_updates as live_module
from row_bot.application.client_models_settings import catalog_provider_summary
from row_bot.application.live_updates import LiveUpdates
from row_bot.providers import auth_store, model_catalog_cache as cache
from row_bot.providers.client_status import list_cached_models
from tests.subsystem.providers import test_provider_settings_commands as credential_commands
from tests.subsystem.providers.test_provider_settings_controls import store  # noqa: F401 -- a fixture
from tests.subsystem.providers.test_subscription_controls import account, command, execute, finish  # noqa: F401

pytestmark = [pytest.mark.subsystem, pytest.mark.platform]

# The real refresh: other tests leave it out (tests/conftest.py); these fake the provider's answer instead.
REFRESH_AFTER_CHANGE = cache.provider_connection_changed
CLAUDE = {"model:claude_subscription:claude-sonnet-5": {"provider": "claude_subscription", "label": "Claude Sonnet 5"}}


def _codex_models() -> dict[str, dict]:
    from row_bot.providers.catalog import model_info_to_cache_entry
    from row_bot.providers.codex import fallback_codex_model_infos

    return {info.selection_ref: model_info_to_cache_entry(info) for info in fallback_codex_model_infos()}


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    """The saved catalog of the last refresh (Claude only), and a ChatGPT that lists its models once signed in."""
    live = LiveUpdates()
    monkeypatch.setattr(live_module, "live_updates", live)
    monkeypatch.setattr(cache, "CATALOG_CACHE_PATH", tmp_path / "model_catalog_cache.json")
    monkeypatch.setattr(cache, "provider_connection_changed", REFRESH_AFTER_CHANGE)
    cache.write_model_catalog_cache(cache.CatalogCacheSnapshot(
        cache.CACHE_VERSION, time.time() - 20 * 60, dict(CLAUDE), [],
        {"claude_subscription": {"status": "live", "count": 1}}, (), "startup"))
    saved = dict(CLAUDE)
    asked: list[str | None] = []
    hold = threading.Event()
    hold.set()

    def provider_catalog(*, provider_id=None):
        asked.append(provider_id)
        assert hold.wait(5)
        if provider_id == "codex" and auth_store.get_provider_secret("codex", "access_token"):
            saved.update(_codex_models())
        return dict(saved), {provider_id or "all": {"status": "live", "count": len(saved)}}

    monkeypatch.setattr(cache, "_refresh_cloud_cache", provider_catalog)
    monkeypatch.setattr(cache, "_refresh_ollama_rows", lambda: [])
    yield live, asked, hold
    hold.set()
    _settled()


def _settled() -> None:
    for thread in threading.enumerate():
        if thread.name == "model-catalog-refresh":
            thread.join(5)
    assert not cache.is_model_catalog_refresh_running()


def _chat_providers() -> dict[str, dict]:
    return {row["provider_id"]: row for row in catalog_provider_summary("chat")["providers"]}


@pytest.mark.parametrize("account", ["codex"], indirect=True)
def test_a_finished_sign_in_lists_the_providers_models_without_refresh(account, catalog):
    provider, owner, *_ = account
    live, asked, _hold = catalog
    assert "codex" not in _chat_providers()  # As Settings › Models showed it: Claude only.
    seen = live.frame()["topics"]["models"]

    flow = execute(owner, command(provider))["flow"]
    assert finish(account, flow)["flow"]["state"] == "connected"
    _settled()

    assert asked == ["codex"]  # Only the provider that changed is asked for its models.
    codex = _chat_providers()["codex"]
    assert codex["total"] == codex["ready"] == len(_codex_models())
    assert list_cached_models(provider_id="claude_subscription").total == 1  # The others are kept.
    assert live.frame()["topics"]["models"] > seen  # Open Models pages read the catalog again.


@pytest.mark.parametrize("account", ["codex"], indirect=True)
def test_after_a_disconnect_its_models_are_no_longer_ready_and_nobody_is_asked(account, catalog):
    provider, owner, *_ = account
    live, asked, _hold = catalog
    finish(account, execute(owner, command(provider))["flow"])
    _settled()
    assert _chat_providers()["codex"]["ready"] == len(_codex_models())
    seen = live.frame()["topics"]["models"]

    execute(owner, command(provider, "disconnect"))
    _settled()

    assert asked == ["codex"]  # Only the sign-in asked ChatGPT; a disconnect contacts nobody.
    assert _chat_providers()["codex"]["ready"] == 0
    assert live.frame()["topics"]["models"] > seen  # Open Models pages read again and stop offering them.


def test_a_sign_in_during_a_running_refresh_is_refreshed_again_once_it_ends(catalog):
    """A refresh already under way may have read the provider before the sign-in: it is never just dropped."""
    live, asked, hold = catalog
    hold.clear()
    assert cache.start_model_catalog_refresh_background(reason="scheduled", force=True)
    deadline = time.monotonic() + 5
    while not asked and time.monotonic() < deadline:
        time.sleep(0.01)

    # Signed in while the scheduled refresh reads every provider.
    cache.provider_connection_changed("codex", connected=True)
    hold.set()
    _settled()

    assert asked == [None, "codex"]
    assert live.frame()["topics"]["models"] == 2


@pytest.mark.parametrize(("operation", "connected"), [("save", True), ("clear", False), ("restore", True)])
def test_a_saved_or_cleared_api_key_shows_in_the_catalog(store, monkeypatch, operation, connected):  # noqa: F811
    changed: list[tuple[str, bool]] = []
    monkeypatch.setattr(cache, "provider_connection_changed",
                        lambda provider_id, *, connected: changed.append((provider_id, connected)))
    if operation == "restore":
        credential_commands.execute(credential_commands.intent("clear"))
        changed.clear()

    credential_commands.execute(credential_commands.intent(operation))

    assert changed == [("openai", connected)]
