"""Per-repository OpenPGP signing keys for hosted apt/yum repositories (ARCHITECTURE §2, §5; adr/0007, *Proposed*).

The one place that signs repository metadata. Implementation (adr/0007, in-house option): keys are generated with
``cryptography`` and signatures made by ``waystation_common.openpgp`` (version 4 packets); no ``gpg`` binary, no
keyring directory. If the ADR is declined for ``gpg`` in the worker image, only this module changes.

- **Keys**: RSA 3072 (read by every rpm/dnf/apt/gpg generation in use, including GnuPG 2.0 on EL7), one per
  repository, created on first use by the worker that builds the repository's index. The certificate (public key,
  one user ID ``Waystation repository <name> (<repo id>)``, self-certification with SHA-512) is public and served
  by the repository; the private key never leaves the data directory.
- **Storage**: ``keys/<repo id>.json`` under ``WAYSTATION_DATA_DIR`` (0700 directory, 0600 files, never readable by
  the web tier): format version, algorithm, creation time, fingerprint, the certificate, and the private key as
  PKCS#8 DER encrypted with ``WAYSTATION_SECRET_KEYS`` (AES-256-GCM, ``waystation_common.crypto``; associated data
  ``signing-key:<repo id>``, so a key file copied to another repository's name does not decrypt). Created atomically
  (written to a temporary file, then hard-linked into place, so two workers racing on a new repository agree on one
  key), never rewritten.
- **Signing**: detached binary-document signatures, SHA-512, armored (``repomd.xml.asc``; apt ``Release.gpg``).

Blocking CPU and file work: call it in a thread (ARCHITECTURE §9).
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from waystation_common import openpgp
from waystation_common.crypto import Keyring
from waystation_common.datadir import KEYS_FILE_MODE
from waystation_common.errors import CryptoError

FORMAT = 1
RSA_BITS = 3072
_REPO_ID = re.compile(r'^[A-Za-z0-9_-]{1,64}$')


@dataclass(frozen=True)
class RepoKey:
    """A repository's signing key, ready to sign."""

    repo_id: str
    private: rsa.RSAPrivateKey
    public: openpgp.PublicKey
    certificate: str  # armored public key block, served to clients

    @property
    def fingerprint(self) -> str:
        return self.public.fingerprint_hex

    def sign(self, data: bytes, *, created: int | None = None) -> str:
        """An armored detached signature of ``data``."""
        return openpgp.sign_detached(self.private, self.public, data, created=created)


def _aad(repo_id: str) -> bytes:
    return f'signing-key:{repo_id}'.encode()


class KeyStore:
    """Signing keys under ``<data dir>/keys``, encrypted with the instance's secret keys."""

    def __init__(self, root: Path, keyring: Keyring) -> None:
        self.root = root
        self.keyring = keyring

    @classmethod
    def in_data_dir(cls, data_dir: Path, keyring: Keyring) -> KeyStore:
        return cls(data_dir / 'keys', keyring)

    def path(self, repo_id: str) -> Path:
        if not _REPO_ID.match(repo_id):
            raise CryptoError('invalid repository id for a signing key')
        return self.root / f'{repo_id}.json'

    def exists(self, repo_id: str) -> bool:
        return self.path(repo_id).exists()

    def load(self, repo_id: str) -> RepoKey:
        doc = json.loads(self.path(repo_id).read_text())
        if doc.get('format') != FORMAT or doc.get('algorithm') != f'rsa{RSA_BITS}':
            raise CryptoError('unsupported signing key file')
        der = self.keyring.decrypt(str(doc['private']), aad=_aad(repo_id))
        private = serialization.load_der_private_key(der, password=None)
        if not isinstance(private, rsa.RSAPrivateKey):
            raise CryptoError('the signing key is not an RSA key')
        public = openpgp.parse_public_key(openpgp.rsa_key_body(private.public_key(), int(doc['created'])))
        if public is None or public.fingerprint_hex != doc.get('fingerprint'):
            raise CryptoError('the signing key file does not match its fingerprint')
        return RepoKey(repo_id, private, public, str(doc['certificate']))

    def load_or_create(self, repo_id: str, user_id: str) -> RepoKey:
        """The repository's key, generating and storing it on first use."""
        target = self.path(repo_id)
        if target.exists():
            return self.load(repo_id)
        created = int(time.time())
        private = rsa.generate_private_key(public_exponent=65537, key_size=RSA_BITS)
        public = openpgp.parse_public_key(openpgp.rsa_key_body(private.public_key(), created))
        assert public is not None  # an RSA key body always parses
        der = private.private_bytes(
            serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        doc = {
            'format': FORMAT,
            'algorithm': f'rsa{RSA_BITS}',
            'created': created,
            'fingerprint': public.fingerprint_hex,
            'certificate': openpgp.certificate(private, public, user_id, created=created),
            'private': self.keyring.encrypt(der, aad=_aad(repo_id)),
        }
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = self.root / f'.{repo_id}.{base64.urlsafe_b64encode(os.urandom(6)).decode()}.tmp'
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, KEYS_FILE_MODE)
        try:
            with os.fdopen(fd, 'w') as fh:
                json.dump(doc, fh)
                fh.flush()
                os.fsync(fh.fileno())
            with contextlib.suppress(FileExistsError):  # another worker stored one first: use that
                os.link(temp, target)  # atomic, and never replaces an existing key
        finally:
            temp.unlink(missing_ok=True)
        dir_fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return self.load(repo_id)
