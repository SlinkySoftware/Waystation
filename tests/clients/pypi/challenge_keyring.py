"""A keyring backend for the PyPI harness: hands pip the API token from ``WS_KEYRING_TOKEN`` and records that it was
asked. pip consults a keyring only after a ``401`` challenge (``--keyring-provider import``), so a non-empty
``WS_KEYRING_MARK`` file proves the server challenged an index URL that carried no credentials (ADR 0005)."""

import os

from keyring.backend import KeyringBackend
from keyring.credentials import SimpleCredential


def _asked() -> None:
    with open(os.environ['WS_KEYRING_MARK'], 'a', encoding='utf-8') as mark:
        mark.write('asked\n')


class ChallengeKeyring(KeyringBackend):
    priority = 1

    def get_password(self, service: str, username: str) -> str | None:
        _asked()
        return os.environ['WS_KEYRING_TOKEN'] if username == '__token__' else None

    def get_credential(self, service: str, username: str | None) -> SimpleCredential:
        _asked()
        return SimpleCredential('__token__', os.environ['WS_KEYRING_TOKEN'])

    def set_password(self, service: str, username: str, password: str) -> None:
        raise NotImplementedError

    def delete_password(self, service: str, username: str) -> None:
        raise NotImplementedError
