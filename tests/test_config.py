import json

import pytest

from scotty_gateway.config import ConfigError, load
from conftest import TOKEN, write_config


def test_substitution_and_defaults(env):
    cfg = load(write_config(env, {"a": {"domain": "x", "type": "http", "url": "http://h/${FAKE_SECRET}"}}))
    s = cfg.servers["a"]
    assert s.url == "http://h/s3cret" and s.enabled and s.mode == "auto" and s.timeout == 60
    assert cfg.token == TOKEN and cfg.member == "tester"


def test_missing_variable_disables_only_that_server(env):
    cfg = load(write_config(env, {
        "a": {"domain": "x", "type": "http", "url": "http://h/${NOPE}"},
        "b": {"domain": "x", "type": "http", "url": "http://ok"}}))
    assert not cfg.servers["a"].enabled and "NOPE" in cfg.servers["a"].reason
    assert cfg.servers["b"].enabled


def test_disabled_with_note(env):
    cfg = load(write_config(env, {"a": {"domain": "x", "type": "http", "url": "u", "disabled": True,
                                        "note": "401s"}}))
    assert cfg.servers["a"].reason == "disabled: 401s"


def test_token_required(env, monkeypatch):
    monkeypatch.delenv("GW_TOKEN")
    with pytest.raises(ConfigError, match="auth.token"):
        load(write_config(env, {}))


def test_short_token_rejected(env, monkeypatch):
    monkeypatch.setenv("GW_TOKEN", "short")
    with pytest.raises(ConfigError, match="shorter"):
        load(write_config(env, {}))


@pytest.mark.parametrize("server,match", [
    ({"domain": "x", "type": "http", "url": "u", "command": "c"}, "unknown keys"),
    ({"domain": "x", "type": "ftp"}, "type"),
    ({"domain": "X!", "type": "http", "url": "u"}, "domain"),
    ({"domain": "x", "type": "stdio"}, "command"),
])
def test_invalid_servers(env, server, match):
    with pytest.raises(ConfigError, match=match):
        load(write_config(env, {"a": server}))


def test_comments_ignored_and_namespace_clash(env):
    ok = {"_note": "comment", "domain": "x", "type": "http", "url": "u", "namespace": "n"}
    load(write_config(env, {"a": ok}))
    with pytest.raises(ConfigError, match="namespace"):
        load(write_config(env, {"a": ok, "b": dict(ok)}))


def test_unknown_top_level_key(env):
    path = env / "g.json"
    path.write_text(json.dumps({"member": "m", "auth": {"token": TOKEN}, "servers": {}, "extra": 1}))
    with pytest.raises(ConfigError, match="unknown keys"):
        load(path)
