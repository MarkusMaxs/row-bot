"""A turn whose model's sign-in can't be used says to sign in again, not "interrupted · Resume" (B351)."""
from __future__ import annotations

import pytest

from tests.contracts.client_platform.test_headless_lifecycle import platform, submit  # noqa: F401

pytestmark = pytest.mark.subsystem


class _SignInExpired:
    def stream(self, *_args, **_kwargs):
        from row_bot.providers.runtime import ProviderSignInRequired

        raise ProviderSignInRequired("codex", "ChatGPT", "Codex subscription runtime needs an in-app ChatGPT login.")
        yield  # A generator, as a real stream is.

    def resume(self, *_args, **_kwargs):
        return self.stream()


def test_a_turn_refused_for_an_expired_sign_in_names_whose_sign_in(platform, monkeypatch):  # noqa: F811
    published: list[tuple[str, dict]] = []
    original = platform.projection.publish

    def recording(conversation_id, kind, payload, *args, **kwargs):
        published.append((kind, dict(payload)))
        return original(conversation_id, kind, payload, *args, **kwargs)

    monkeypatch.setattr(platform.projection, "publish", recording)

    accepted = submit(platform, _SignInExpired(), "sign-in-expired")
    assert platform.registry.get(accepted["execution_id"]).producer_done.wait(10)

    errors = [payload for kind, payload in published if kind == "generation.error"]
    assert errors == [{"code": "sign_in_required", "provider": "ChatGPT"}]
