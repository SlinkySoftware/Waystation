"""Encrypted files under ``<data dir>/keys`` (ARCHITECTURE §5, §7) and their re-encryption with the active secret
key after a rotation (T-O11, ``waystation_worker.rotate``).

- ``<repo id>.json``: a hosted apt/yum repository's OpenPGP signing key (``waystation_common.signing``): JSON whose
  ``private`` is the ciphertext, associated data ``signing-key:<repo id>``.
- ``registry-token.key``: the Docker registry token signing key (``waystation_server.security.registry_token``): the
  ciphertext alone on one line, associated data ``registry-token-signing-key``.

``rotate_file`` re-encrypts one file: it decrypts the stored value, encrypts it again with the active key, checks
that the new value decrypts to the same plaintext, writes the new content to a temporary file in the same directory
(the old file's mode and, where the process may, its owner; fsync) and renames it over the old file, then fsyncs the
directory. A reader sees the old file or the new one, never a partial one, and both decrypt while the old key is
still configured; the key material itself does not change, so signatures and issued tokens stay valid.
Blocking file and CPU work: call it in a thread (ARCHITECTURE §9).
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from waystation_common.crypto import Keyring
from waystation_common.errors import CryptoError
from waystation_common.signing import key_aad

KEYS_DIR = 'keys'
REGISTRY_TOKEN_KEY_FILE = 'registry-token.key'  # noqa: S105 - a file name, not a secret
REGISTRY_TOKEN_KEY_AAD = b'registry-token-signing-key'


def key_files(root: Path) -> list[Path]:
    """The encrypted files in the keys directory ``root`` (temporary files, starting with ``.``, are skipped)."""
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.iterdir()
        if not path.name.startswith('.')
        and path.is_file()
        and (path.name == REGISTRY_TOKEN_KEY_FILE or path.suffix == '.json')
    )


def _read(path: Path) -> tuple[str, bytes, dict[str, Any] | None]:
    """(ciphertext, associated data, the JSON document or None for the registry token key)."""
    try:
        text = path.read_text(encoding='utf-8')
        if path.name == REGISTRY_TOKEN_KEY_FILE:
            return text.strip(), REGISTRY_TOKEN_KEY_AAD, None
        doc = json.loads(text)
    except (UnicodeError, ValueError) as exc:
        raise CryptoError(f'unreadable key file ({type(exc).__name__})') from None
    if not isinstance(doc, dict) or not isinstance(doc.get('private'), str):
        raise CryptoError('the key file has no encrypted private key')
    return str(doc['private']), key_aad(path.stem), doc


def needs_rotation(path: Path, keyring: Keyring) -> bool:
    """True when the file was not encrypted with the active key (``CryptoError`` when it is not readable)."""
    sealed, _, _ = _read(path)
    return keyring.needs_rotation(sealed)


def rotate_file(path: Path, keyring: Keyring) -> bool:
    """Re-encrypt ``path`` with the active key; False when it already uses it. ``CryptoError`` when no configured key
    decrypts it (the file is left as it is)."""
    sealed, aad, doc = _read(path)
    if not keyring.needs_rotation(sealed):
        return False
    plaintext = keyring.decrypt(sealed, aad=aad)
    fresh = keyring.encrypt(plaintext, aad=aad)
    if keyring.decrypt(fresh, aad=aad) != plaintext:  # pragma: no cover - AES-GCM round trip
        raise CryptoError('the re-encrypted key does not decrypt')
    content = fresh + '\n' if doc is None else json.dumps({**doc, 'private': fresh})
    replace_file(path, content)
    return True


def replace_file(path: Path, content: str) -> None:
    """Atomically replace ``path`` with ``content`` (same directory, same mode, owner kept where possible)."""
    st = path.stat()
    temp = path.parent / f'.{path.name}.{base64.urlsafe_b64encode(os.urandom(6)).decode()}.tmp'
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IMODE(st.st_mode))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            os.fchmod(handle.fileno(), stat.S_IMODE(st.st_mode))  # the umask may have narrowed it
            if (st.st_uid, st.st_gid) != (os.getuid(), os.getgid()):
                with contextlib.suppress(PermissionError):  # only root may give a file away
                    os.fchown(handle.fileno(), st.st_uid, st.st_gid)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
