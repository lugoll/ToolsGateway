"""Encryption, the secret store, connections and the audit log."""
import dataclasses

import pytest

from gateway.app import Services
from gateway.settings import parse_key
from gateway.storage.crypto import CryptoError, Vault, generate_key
from gateway.storage.connections import ConnectionSetupError

KEY = parse_key(generate_key(), "k")
OTHER = parse_key(generate_key(), "k")


def test_round_trip_and_tamper():
    v = Vault(KEY)
    stored = v.encrypt("app-password", "secrets:1:password")
    assert "app-password" not in stored and stored.startswith(f"v1:{v.current}:")
    assert v.decrypt(stored, "secrets:1:password") == "app-password"
    with pytest.raises(CryptoError):
        v.decrypt(stored, "secrets:2:password")            # copied into another row
    with pytest.raises(CryptoError):
        Vault(OTHER).decrypt(stored, "secrets:1:password")   # wrong key
    head, ct = stored.rsplit(":", 1)
    flipped = ct[:4] + ("B" if ct[4] != "B" else "C") + ct[5:]       # a byte inside, not padding bits
    with pytest.raises(CryptoError):
        v.decrypt(f"{head}:{flipped}", "secrets:1:password")


def test_parse_key_rejects_short_keys():
    from gateway.settings import SettingsError
    with pytest.raises(SettingsError, match="32 bytes"):
        parse_key("c2hvcnQ", "GATEWAY_MASTER_KEY")


def test_wrong_master_key_refuses_to_open(settings):
    Services.open(settings).db.close()
    with pytest.raises(CryptoError, match="does not open this database"):
        Services.open(dataclasses.replace(settings, master_key=OTHER))


def test_rotation(settings):
    s = Services.open(settings)
    user = s.users.create("lukas", "lukas-password-1")
    c = s.connections.create(user.id, s.catalog.get("icloud-mail"), "icloud", "", {"email": "a@icloud.com"},
                             {"password": "pw-1"}, by="lukas")
    s.secrets.set_instance("google_client_secret", "gcs", by="admin")
    s.db.close()
    rotated = dataclasses.replace(settings, master_key=OTHER, master_key_old=settings.master_key)
    s = Services.open(rotated)
    assert s.secrets.rotate() == 2
    s.db.close()
    s = Services.open(dataclasses.replace(rotated, master_key_old=None))     # old key gone
    assert s.secrets.get(c.id, "password") == "pw-1"
    assert s.secrets.get_instance("google_client_secret") == "gcs"


def test_connections_validate_and_keep_secrets_write_only(services):
    s = services
    user = s.users.create("lukas", "lukas-password-1")
    entry = s.catalog.get("imap-mail")
    with pytest.raises(ConnectionSetupError, match="IMAP server is required"):
        s.connections.create(user.id, entry, "box", "", {"email": "a@b.c"}, {"password": "x"}, by="lukas")
    with pytest.raises(ConnectionSetupError, match="Password required"):
        s.connections.create(user.id, entry, "box", "", {"email": "a@b.c", "host": "h"}, {}, by="lukas")
    with pytest.raises(ConnectionSetupError, match="Port: expected a number"):
        s.connections.create(user.id, entry, "box", "", {"email": "a@b.c", "host": "h", "port": "x"},
                             {"password": "x"}, by="lukas")
    c = s.connections.create(user.id, entry, "box", "", {"email": "a@b.c", "host": "h", "password": "leak"},
                             {"password": "pw"}, by="lukas")
    assert c.settings == {"email": "a@b.c", "host": "h", "port": 993, "tls": True}   # no secret in settings
    assert c.label == "Other IMAP mailbox"
    raw = s.db.one("SELECT value FROM secrets WHERE connection_id = ?", (c.id,))["value"]
    assert "pw" not in raw.split(":")[-1] and s.secrets.get(c.id, "password") == "pw"
    assert s.secrets.describe(c.id)["password"]["updated_by"] == "lukas"
    with pytest.raises(ConnectionSetupError, match="already have"):
        s.connections.create(user.id, entry, "box", "", {"email": "a@b.c", "host": "h"}, {"password": "x"}, by="l")
    assert s.connections.get(c.id, user_id=user.id + 1) is None                       # not someone else's
    assert s.connections.effective_capabilities(c) == {"read", "mark_read", "draft", "move"}
    c = s.connections.update(c, capabilities=["read"])
    assert s.connections.effective_capabilities(c) == {"read"}


def test_audit_buffer_and_retention(services):
    audit = services.audit
    audit.max_rows = 5
    for i in range(8):
        audit.record("lukas", "call", f"tool{i}", integration="mail")
    assert services.db.one("SELECT COUNT(*) AS n FROM audit")["n"] == 0      # still buffered
    assert [e.action for e in audit.query(user="lukas", limit=3)] == ["tool7", "tool6", "tool5"]
    services.db.execute("UPDATE audit SET ts = '2000-01-01T00:00:00+00:00' WHERE action = 'tool7'")
    assert audit.prune() == 3                       # one too old, two over the row limit
    assert [e.action for e in audit.query()] == ["tool6", "tool5", "tool4", "tool3", "tool2"]
