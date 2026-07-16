from __future__ import annotations

from typing import Protocol

import keyring
from keyring.errors import KeyringError


SERVICE_NAME = "career-assistant-executor"


class SecretStoreUnavailableError(RuntimeError):
    pass


class SecretStore(Protocol):
    def get(self, key: str) -> str | None:
        raise NotImplementedError

    def set(self, key: str, value: str) -> None:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError


class WindowsCredentialStore:
    def get(self, key: str) -> str | None:
        try:
            return keyring.get_password(SERVICE_NAME, key)
        except KeyringError as error:
            raise SecretStoreUnavailableError(
                "windows credential store unavailable"
            ) from error

    def set(self, key: str, value: str) -> None:
        try:
            keyring.set_password(SERVICE_NAME, key, value)
        except KeyringError as error:
            raise SecretStoreUnavailableError(
                "windows credential store unavailable"
            ) from error

    def delete(self, key: str) -> None:
        try:
            keyring.delete_password(SERVICE_NAME, key)
        except keyring.errors.PasswordDeleteError:
            return
        except KeyringError as error:
            raise SecretStoreUnavailableError(
                "windows credential store unavailable"
            ) from error
