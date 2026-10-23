"""Password hashing with Argon2id (argon2-cffi defaults, rehashed when they change)."""
from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_LENGTH = 10

_hasher = PasswordHasher()
# Verified against when the user does not exist, so a miss costs the same time.
_DUMMY = _hasher.hash("tools-gateway dummy password")


class PasswordPolicyError(ValueError):
    pass


def check_policy(password: str) -> None:
    if len(password) < MIN_LENGTH:
        raise PasswordPolicyError(f"passwords need at least {MIN_LENGTH} characters")


def hash_password(password: str) -> str:
    check_policy(password)
    return _hasher.hash(password)


def verify(stored: str | None, password: str) -> bool:
    try:
        return _hasher.verify(stored or _DUMMY, password) and stored is not None
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(stored: str) -> bool:
    return _hasher.check_needs_rehash(stored)
