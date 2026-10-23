"""Users, passwords and API tokens."""
import pytest

from gateway.auth.passwords import PasswordPolicyError
from gateway.auth.users import UserError


def test_users_and_passwords(services):
    users = services.users
    with pytest.raises(PasswordPolicyError):
        users.create("lukas", "short")
    with pytest.raises(UserError, match="lowercase"):
        users.create("Lukas", "long-enough-pw")
    with pytest.raises(UserError, match="lowercase"):
        users.create("x", "long-enough-pw")
    lukas = users.create("lukas", "long-enough-pw", display_name="Lukas")
    assert users.authenticate("lukas", "long-enough-pw") == lukas
    assert users.authenticate("lukas", "wrong-password") is None
    assert users.authenticate("nobody", "long-enough-pw") is None
    users.update(lukas.id, disabled=True)
    assert users.authenticate("lukas", "long-enough-pw") is None
    raw = services.db.one("SELECT password_hash FROM users WHERE id = ?", (lukas.id,))["password_hash"]
    assert raw.startswith("$argon2id$")


def test_bootstrap_admin_and_last_admin(services):
    users = services.users
    admin = users.bootstrap_admin("admin", "admin-password-1")
    assert admin.is_admin
    assert users.bootstrap_admin("admin", "other-password-1") is None      # never overwrites
    assert users.authenticate("admin", "admin-password-1")
    with pytest.raises(UserError, match="last active admin"):
        users.update(admin.id, role="user")
    with pytest.raises(UserError, match="last active admin"):
        users.delete(admin.id)
    second = users.create("masha", "masha-password-1", role="admin")
    users.update(admin.id, role="user")
    assert not users.get(admin.id).is_admin and second.is_admin


def test_tokens(services):
    lukas = services.users.create("lukas", "lukas-password-1")
    token, row = services.tokens.create(lukas.id, "hermes")
    assert token.startswith("tgw_") and len(token) > 40 and row.prefix == token[:10]
    stored = services.db.one("SELECT hash FROM api_tokens WHERE id = ?", (row.id,))["hash"]
    assert token not in stored
    assert services.tokens.authenticate(token) == lukas
    assert services.tokens.get(row.id).last_used is not None
    assert services.tokens.authenticate(token + "x") is None
    other = services.users.create("masha", "masha-password-1")
    assert services.tokens.revoke(other.id, row.id) is False          # not hers
    services.users.update(lukas.id, disabled=True)
    assert services.tokens.authenticate(token) is None                # disabled user
    services.users.update(lukas.id, disabled=False)
    assert services.tokens.revoke(lukas.id, row.id) is True
    assert services.tokens.authenticate(token) is None
