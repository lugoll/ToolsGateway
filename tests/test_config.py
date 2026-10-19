import pytest

from scotty_gateway.config import ConfigError, load
from conftest import TOKEN, write_config, write_recipe


def test_substitution_and_defaults(env):
    cfg = load(write_config(env, {"a-tester": {"domain": "x", "type": "http", "url": "http://h/${FAKE_SECRET}"}}))
    s = cfg.servers["a-tester"]
    assert s.url == "http://h/s3cret" and s.enabled and s.mode == "auto" and s.timeout == 60
    assert cfg.token == TOKEN and cfg.member == "tester"


def test_token_defaults_to_gateway_token(env, monkeypatch):
    monkeypatch.setenv("GATEWAY_TOKEN", "g" * 32)
    path = env / "m.toml"
    path.write_text('member = "m"\n')
    assert load(path).token == "g" * 32


def test_missing_variable_disables_only_that_server(env):
    cfg = load(write_config(env, {
        "a-tester": {"domain": "x", "type": "http", "url": "http://h/${NOPE}"},
        "b-tester": {"domain": "x", "type": "http", "url": "http://ok"}}))
    assert not cfg.servers["a-tester"].enabled and "NOPE" in cfg.servers["a-tester"].reason
    assert cfg.servers["b-tester"].enabled


def test_disabled_with_note(env):
    cfg = load(write_config(env, {"a-tester": {"domain": "x", "type": "http", "url": "u", "disabled": True,
                                               "note": "401s"}}))
    assert cfg.servers["a-tester"].reason == "disabled: 401s"


def test_disabled_needs_note(env):
    with pytest.raises(ConfigError, match="note"):
        load(write_config(env, {"a-tester": {"domain": "x", "type": "http", "url": "u", "disabled": True}}))


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
    ({"domain": "x", "type": "stdio"}, "command"),
])
def test_invalid_servers(env, server, match):
    with pytest.raises(ConfigError, match=match):
        load(write_config(env, {"a-tester": server}))


def test_invalid_recipe_domain(env):
    with pytest.raises(ConfigError, match="domain"):
        load(write_config(env, {"a-tester": {"domain": "X!", "type": "http", "url": "u"}}))


def test_comments_ignored_and_namespace_clash(env):
    ok = {"_note": "comment", "domain": "x", "type": "http", "url": "u", "namespace": "n"}
    load(write_config(env, {"a-tester": ok}))
    with pytest.raises(ConfigError, match="namespace"):
        load(write_config(env, {"a-tester": ok, "b-tester": dict(ok)}))


def test_unknown_member_key_and_recipe(env):
    path = write_config(env, {})
    path.write_text("extra = 1\n" + path.read_text())
    with pytest.raises(ConfigError, match="unknown keys"):
        load(path)
    with pytest.raises(ConfigError, match="recipe"):
        load(write_config(env, {}, entries=[{"recipe": "nope"}]))


def test_accounts_are_picked_per_member(env):
    write_recipe(env / "connectors", "mail", "email",
                 {"type": "native", "module": "mail", "config": {"timeout": 5}},
                 accounts={"a": {"backend": "imap", "email": "${FAKE_SECRET}"}, "b": {"backend": "gmail"}})
    cfg = load(write_config(env, {}, entries=[{"recipe": "mail", "accounts": ["a"]}]))
    s = cfg.servers["mail-tester"]
    assert s.config == {"timeout": 5, "accounts": {"a": {"backend": "imap", "email": "s3cret"}}}
    with pytest.raises(ConfigError, match="unknown: c"):
        load(write_config(env, {}, entries=[{"recipe": "mail", "accounts": ["c"]}]))
    with pytest.raises(ConfigError, match="pick from"):
        load(write_config(env, {}, entries=[{"recipe": "mail"}]))


def test_accounts_on_a_recipe_without_accounts(env):
    write_recipe(env / "connectors", "plain", "x", {"type": "http", "url": "u"})
    with pytest.raises(ConfigError, match="has no accounts"):
        load(write_config(env, {}, entries=[{"recipe": "plain", "accounts": ["a"]}]))


def test_unused_recipe_domains_are_still_served(env):
    write_recipe(env / "connectors", "other", "calendar", {"type": "http", "url": "u"})
    cfg = load(write_config(env, {"a-tester": {"domain": "x", "type": "http", "url": "u"}}))
    assert cfg.domains() == ["x", "calendar"] and set(cfg.servers) == {"a-tester"}
