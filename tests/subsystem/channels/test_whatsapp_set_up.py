"""B345: WhatsApp is set up to deliver to only once a phone is linked, not because its bridge is installed."""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.subsystem


def test_whatsapp_is_set_up_only_after_a_phone_is_linked(tmp_path, monkeypatch):
    from row_bot.channels import whatsapp

    monkeypatch.setattr(whatsapp, "SESSION_DIR", tmp_path / "whatsapp_session")
    monkeypatch.setattr(whatsapp, "is_configured", lambda: True)  # The bundled bridge is present.
    channel = whatsapp.WhatsAppChannel()
    assert channel.is_configured() is True
    assert channel.is_set_up() is False  # Installed, never linked.
    whatsapp.SESSION_DIR.mkdir()
    # Credentials saved before any phone scanned the code: still not linked.
    (whatsapp.SESSION_DIR / "creds.json").write_text(json.dumps({"noiseKey": {}}), encoding="utf-8")
    assert channel.is_set_up() is False
    (whatsapp.SESSION_DIR / "creds.json").write_text(
        json.dumps({"noiseKey": {}, "me": {"id": "synthetic:1@s.whatsapp.net"}}), encoding="utf-8")
    assert channel.is_set_up() is True
