#!/usr/bin/env python3
"""Small encrypted credential vault for local integration profiles."""

from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class CredentialVaultError(RuntimeError):
    pass


class CredentialVault:
    def __init__(self, key_path: Path):
        self.key_path = key_path
        self._fernet = Fernet(self._load_or_create_key())

    def _load_or_create_key(self) -> bytes:
        configured = os.environ.get("AGI_CREDENTIAL_MASTER_KEY", "").strip()
        if configured:
            return configured.encode("ascii")
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        if self.key_path.exists():
            return self.key_path.read_bytes().strip()
        key = Fernet.generate_key()
        descriptor = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key)
        return key

    def encrypt(self, value: dict) -> str:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self._fernet.encrypt(payload).decode("ascii")

    def decrypt(self, token: str) -> dict:
        try:
            value = json.loads(self._fernet.decrypt(token.encode("ascii")).decode("utf-8"))
        except (InvalidToken, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CredentialVaultError("integration credential cannot be decrypted") from exc
        if not isinstance(value, dict):
            raise CredentialVaultError("integration credential payload is invalid")
        return value
