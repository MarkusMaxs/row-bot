"""The X tools stay bound through a transient failure; X is asked once per sign-in (WP A5).

Building a chat's tools used to call X (GET /users/me) on every turn, and a timeout or an X outage
dropped the X tools for that turn: a different tool list, so the cached start of every request was
lost, and the person's X tools vanished without a reason. Fakes only; nothing reaches X.
"""
from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.subsystem


@pytest.fixture
def x(monkeypatch):
    from row_bot.tools import x_tool

    monkeypatch.setenv("X_CLIENT_ID", "client-id")
    monkeypatch.setenv("X_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(x_tool, "_SIGN_IN_VERDICTS", {})
    monkeypatch.setattr(x_tool.account_tokens, "record_check", lambda name, status: None)
    state = {"token": {"access_token": "access-1", "refresh_token": "refresh-1", "expires_at": time.time() + 3600},
             "answers": [], "checks": 0}
    monkeypatch.setattr(x_tool, "_load_token", lambda: state["token"])

    def probe(self):
        state["checks"] += 1
        return state["answers"].pop(0)

    monkeypatch.setattr(x_tool.XTool, "_probe_token_health", probe)
    tool = x_tool.XTool()
    monkeypatch.setattr(tool, "get_config", lambda key, default=None: default)
    return tool, state


def _names(tool) -> list[str]:
    return [bound.name for bound in tool.as_langchain_tools()]


def test_x_is_asked_once_per_sign_in(x):
    tool, state = x
    state["answers"] = [("valid", "Token is valid")]

    assert _names(tool) == ["x_read", "x_post", "x_engage"]
    assert _names(tool) == ["x_read", "x_post", "x_engage"]  # the next turn: no call to X
    assert state["checks"] == 1


def test_a_check_that_cannot_reach_x_keeps_the_tools_bound(x):
    tool, state = x
    state["answers"] = [("error", "Could not verify token: timed out")]

    assert _names(tool) == ["x_read", "x_post", "x_engage"]  # the call itself reports what failed
    assert _names(tool) == ["x_read", "x_post", "x_engage"]
    assert state["checks"] == 1


def test_x_refusing_the_sign_in_leaves_the_tools_out_until_a_new_sign_in(x):
    tool, state = x
    state["answers"] = [("expired", "Token rejected by X API"), ("valid", "Token is valid")]

    assert _names(tool) == []
    assert _names(tool) == []  # the same refused sign-in is not asked about again
    state["token"] = {"access_token": "access-2", "refresh_token": "refresh-2", "expires_at": time.time() + 3600}
    assert _names(tool) == ["x_read", "x_post", "x_engage"]  # signed in again
    assert state["checks"] == 2


def test_an_access_token_past_its_time_is_left_to_the_call_to_refresh(x):
    tool, state = x
    state["token"] = {**state["token"], "expires_at": time.time() - 10}

    assert _names(tool) == ["x_read", "x_post", "x_engage"]
    assert state["checks"] == 0  # binding never waits on a refresh
