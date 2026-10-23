"""Secrets at rest: AES-256-GCM under the master key from the environment.

A stored value is "v1:<key id>:<nonce>:<ciphertext>" (base64url). The
associated data names the row the value belongs to ("secrets:<connection>:<name>"),
so a ciphertext copied into another row does not decrypt. The key id lets a
second key decrypt old values while rotating (GATEWAY_MASTER_KEY_OLD,
`gateway rotate-key`); new values are always written with the current key.
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = "v1"


class CryptoError(ValueError):
    """A value cannot be decrypted: wrong key, unknown key id or tampered data."""


def key_id(key: bytes) -> str:
    return hashlib.sha256(b"tools-gateway key id" + key).hexdigest()[:8]


def generate_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Vault:
    def __init__(self, key: bytes, old_key: bytes | None = None):
        self.current = key_id(key)
        self._keys = {self.current: AESGCM(key)}
        if old_key and key_id(old_key) != self.current:
            self._keys[key_id(old_key)] = AESGCM(old_key)

    def encrypt(self, plaintext: str, aad: str) -> str:
        nonce = os.urandom(12)
        ct = self._keys[self.current].encrypt(nonce, plaintext.encode(), aad.encode())
        return f"{VERSION}:{self.current}:{_b64(nonce)}:{_b64(ct)}"

    def decrypt(self, stored: str, aad: str) -> str:
        try:
            version, kid, nonce, ct = stored.split(":")
        except ValueError:
            raise CryptoError("not an encrypted value") from None
        if version != VERSION:
            raise CryptoError(f"unknown format {version}")
        aes = self._keys.get(kid)
        if aes is None:
            raise CryptoError(f"value was encrypted with key {kid}, which is not configured")
        try:
            return aes.decrypt(_unb64(nonce), _unb64(ct), aad.encode()).decode()
        except (InvalidTag, ValueError):
            raise CryptoError("value does not decrypt (wrong key or tampered)") from None

    def is_current(self, stored: str) -> bool:
        return stored.split(":")[1:2] == [self.current]
