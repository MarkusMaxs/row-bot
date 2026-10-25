"""The X tools stay bound through a transient failure; X is asked once per sign-in (WP A5).

Building a chat's tools used to call X (GET /users/me) on every turn, and a timeout or an X outage
dropped the X tools for that turn: a different tool list, so the cached start of every request was
lost, and the person's X tools vanished without a reason. With several accounts (N7) the tools stay
while any account's sign-in works. A fake X API answers; nothing reaches X.
"""
from __future__ import annotations

import httpx
import pytest

from row_bot import account_tokens, x_accounts
from tests.helpers.fake_x_api import PERSONAL, ROWBOT, FakeXApi, install

pytestmark = pytest.mark.subsystem

_TOOLS = ["x_accounts", "x_read", "x_post", "x_engage"]


@pytest.fixture
def x(tmp_path, monkeypatch):
    from row_bot.tools import x_tool

    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr(x_tool, "_SIGN_IN_VERDICTS", {})
    fake = FakeXApi()
    install(monkeypatch, fake)
    state = {"down": False}
    answer = fake.handle

    def x_api(request):
        if state["down"]:
            raise httpx.ConnectTimeout("X is unreachable")
        return answer(request)
    monkeypatch.setattr(x_accounts, "transport", httpx.MockTransport(x_api))
    tool = x_tool.XTool()
    monkeypatch.setattr(tool, "get_config", lambda key, default=None: default)
    return tool, fake, state


def _names(tool) -> list[str]:
    return [bound.name for bound in tool.as_langchain_tools()]


def _checks(fake) -> int:
    return fake.requests.count(("GET", "/2/users/me"))


def test_x_is_asked_once_per_sign_in(x):
    tool, fake, _ = x
    x_accounts.add(fake.sign_in(*ROWBOT))
    before = _checks(fake)

    assert _names(tool) == _TOOLS
    assert _names(tool) == _TOOLS  # the next turn: no call to X
    assert _checks(fake) - before == 1


def test_a_check_that_cannot_reach_x_keeps_the_tools_bound(x):
    tool, fake, state = x
    x_accounts.add(fake.sign_in(*ROWBOT))
    state["down"] = True

    assert _names(tool) == _TOOLS  # the call itself reports what failed
    assert _names(tool) == _TOOLS


def test_x_refusing_every_sign_in_leaves_the_tools_out_until_a_new_sign_in(x):
    tool, fake, _ = x
    x_accounts.add(fake.sign_in(*ROWBOT))
    fake.users.clear()  # X turns the sign-in down.

    assert _names(tool) == []
    before = _checks(fake)
    assert _names(tool) == []  # the same refused sign-in is not asked about again
    assert _checks(fake) == before
    x_accounts.add(fake.sign_in(*ROWBOT, access="access-2"))  # signed in again
    assert _names(tool) == _TOOLS


def test_one_working_account_keeps_the_tools_while_another_is_refused(x):
    tool, fake, _ = x
    x_accounts.add(fake.sign_in(*ROWBOT))
    x_accounts.add(fake.sign_in(*PERSONAL))
    del fake.users["access-rowbot_ai"]

    assert _names(tool) == _TOOLS


def test_an_access_token_past_its_time_is_left_to_the_call_to_refresh(x):
    tool, fake, _ = x
    x_accounts.add(fake.sign_in(*ROWBOT, expires_at=946684800.0))
    before = _checks(fake)

    assert _names(tool) == _TOOLS
    assert _checks(fake) == before  # binding never waits on a refresh


def test_the_sign_in_from_before_binds_the_tools_until_it_becomes_an_account(x):
    tool, fake, state = x
    account_tokens.write("x", fake.sign_in(*ROWBOT))
    state["down"] = True  # Not named yet: X can't be reached.

    assert _names(tool) == _TOOLS
