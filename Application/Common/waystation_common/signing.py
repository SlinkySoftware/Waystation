"""Per-repository OpenPGP signing keys for hosted apt/yum repositories (ARCHITECTURE §2, §5; adr/0007, *Proposed*).

The one place that signs repository metadata. Implementation (adr/0007, in-house option): keys are generated with
``cryptography`` and version 4 packets are written here (key, user ID, self-certification, detached signatures);
``waystation_common.openpgp`` (adr/0006) reads and verifies them like any other OpenPGP data. No ``gpg`` binary, no
keyring directory. If adr/0007 is declined for ``gpg`` in the worker image, only this module changes.

- **Keys**: RSA 3072 (read by every rpm/dnf/apt/gpg generation in use, including GnuPG 2.0 on EL7), one per
  repository, created on first use by the worker that builds the repository's index. The certificate (public key,
  one user ID ``Waystation repository <name> (<repo id>)``, positive self-certification with key flags certify +
  sign, SHA-512) is public and served by the repository; the private key never leaves the data directory.
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
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa, utils

from waystation_common import openpgp
from waystation_common.crypto import Keyring
from waystation_common.datadir import KEYS_FILE_MODE
from waystation_common.errors import CryptoError

FORMAT = 1
RSA_BITS = 3072
_REPO_ID = re.compile(r'^[A-Za-z0-9_-]{1,64}$')

ALG_RSA = 1
HASH_SHA256, HASH_SHA512 = 8, 10
SIG_BINARY, SIG_POSITIVE_CERT = 0x00, 0x13
TAG_SIGNATURE, TAG_PUBLIC_KEY, TAG_USER_ID = 2, 6, 13
SUB_CREATED, SUB_ISSUER, SUB_PREF_HASH, SUB_PRIMARY_UID, SUB_KEY_FLAGS, SUB_ISSUER_FPR = 2, 16, 21, 25, 27, 33


# ---------------------------------------------------------------------------- packet encoding
def _crc24(data: bytes) -> int:
    crc = 0xB704CE
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    return crc & 0xFFFFFF


def armor(kind: str, data: bytes) -> str:
    """ASCII armor (``kind``: ``PUBLIC KEY BLOCK``, ``SIGNATURE``): 64-column base64 and the CRC-24 line."""
    body = base64.b64encode(data).decode('ascii')
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    check = base64.b64encode(_crc24(data).to_bytes(3, 'big')).decode('ascii')
    return '\n'.join([f'-----BEGIN PGP {kind}-----', '', *lines, f'={check}', f'-----END PGP {kind}-----', ''])


def _length(n: int) -> bytes:
    """A new-format packet / subpacket length (one, two or five octets)."""
    if n < 192:
        return bytes([n])
    if n < 8384:
        n -= 192
        return bytes([(n >> 8) + 192, n & 0xFF])
    return b'\xff' + n.to_bytes(4, 'big')


def packet(tag: int, body: bytes) -> bytes:
    return bytes([0xC0 | tag]) + _length(len(body)) + body


def subpacket(kind: int, content: bytes, *, critical: bool = False) -> bytes:
    data = bytes([kind | (0x80 if critical else 0)]) + content
    return _length(len(data)) + data


def mpi(value: int | bytes) -> bytes:
    raw = value if isinstance(value, bytes) else value.to_bytes((value.bit_length() + 7) // 8 or 1, 'big')
    raw = raw.lstrip(b'\x00')
    bits = (len(raw) - 1) * 8 + raw[0].bit_length() if raw else 0
    return bits.to_bytes(2, 'big') + raw


def rsa_key_body(public: rsa.RSAPublicKey, created: int) -> bytes:
    """A version 4 RSA public key packet body."""
    numbers = public.public_numbers()
    return bytes([4]) + created.to_bytes(4, 'big') + bytes([ALG_RSA]) + mpi(numbers.n) + mpi(numbers.e)


@dataclass(frozen=True)
class PublicPart:
    """The public half of a signing key as OpenPGP sees it."""

    body: bytes  # the public key packet body
    fingerprint: bytes

    @classmethod
    def of(cls, private: rsa.RSAPrivateKey, created: int) -> PublicPart:
        body = rsa_key_body(private.public_key(), created)
        return cls(body, hashlib.sha1(b'\x99' + len(body).to_bytes(2, 'big') + body, usedforsecurity=False).digest())

    @property
    def key_id(self) -> bytes:
        return self.fingerprint[-8:]

    @property
    def fingerprint_hex(self) -> str:
        return self.fingerprint.hex().upper()


def make_signature(
    private: rsa.RSAPrivateKey,
    key: PublicPart,
    sig_type: int,
    signed: bytes,
    *,
    created: int,
    hashed_extra: bytes = b'',
) -> bytes:
    """A version 4 RSA/SHA-512 signature packet over ``signed`` (the document, or the key and user ID certified).
    Hashed subpackets: creation time, issuer fingerprint, ``hashed_extra``; unhashed: the issuer key id."""
    area = (
        subpacket(SUB_CREATED, created.to_bytes(4, 'big'))
        + subpacket(SUB_ISSUER_FPR, b'\x04' + key.fingerprint)
        + hashed_extra
    )
    hashed = bytes([4, sig_type, ALG_RSA, HASH_SHA512]) + len(area).to_bytes(2, 'big') + area
    digest = hashlib.sha512(signed + hashed + b'\x04\xff' + len(hashed).to_bytes(4, 'big')).digest()
    value = private.sign(digest, padding.PKCS1v15(), utils.Prehashed(hashes.SHA512()))
    unhashed = subpacket(SUB_ISSUER, key.key_id)
    return packet(TAG_SIGNATURE, hashed + len(unhashed).to_bytes(2, 'big') + unhashed + digest[:2] + mpi(value))


def sign_detached(private: rsa.RSAPrivateKey, key: PublicPart, data: bytes, *, created: int | None = None) -> str:
    """An armored detached binary-document signature of ``data``."""
    when = int(time.time()) if created is None else created
    return armor('SIGNATURE', make_signature(private, key, SIG_BINARY, data, created=when))


def certificate(private: rsa.RSAPrivateKey, key: PublicPart, user_id: str, *, created: int | None = None) -> str:
    """The armored transferable public key: the key, one user ID and its positive self-certification (key flags
    certify and sign; preferred hashes SHA-512, SHA-256), as ``rpm --import`` / ``gpg --import`` expect."""
    when = int(time.time()) if created is None else created
    uid = user_id.encode('utf-8')
    signed = b'\x99' + len(key.body).to_bytes(2, 'big') + key.body + b'\xb4' + len(uid).to_bytes(4, 'big') + uid
    extra = (
        subpacket(SUB_KEY_FLAGS, b'\x03')
        + subpacket(SUB_PREF_HASH, bytes([HASH_SHA512, HASH_SHA256]))
        + subpacket(SUB_PRIMARY_UID, b'\x01')
    )
    cert = make_signature(private, key, SIG_POSITIVE_CERT, signed, created=when, hashed_extra=extra)
    return armor('PUBLIC KEY BLOCK', packet(TAG_PUBLIC_KEY, key.body) + packet(TAG_USER_ID, uid) + cert)


# ---------------------------------------------------------------------------- repository keys
@dataclass(frozen=True)
class RepoKey:
    """A repository's signing key, ready to sign."""

    repo_id: str
    signer: rsa.RSAPrivateKey  # the private key
    public: PublicPart
    certificate: str  # armored public key block, served to clients

    @property
    def fingerprint(self) -> str:
        return self.public.fingerprint_hex

    def sign(self, data: bytes, *, created: int | None = None) -> str:
        """An armored detached signature of ``data``."""
        return sign_detached(self.signer, self.public, data, created=created)


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
        public = PublicPart.of(private, int(doc['created']))
        if public.fingerprint_hex != doc.get('fingerprint'):
            raise CryptoError('the signing key file does not match its fingerprint')
        return RepoKey(repo_id, private, public, str(doc['certificate']))

    def load_or_create(self, repo_id: str, user_id: str) -> RepoKey:
        """The repository's key, generating and storing it on first use."""
        target = self.path(repo_id)
        if target.exists():
            return self.load(repo_id)
        created = int(time.time())
        private = rsa.generate_private_key(public_exponent=65537, key_size=RSA_BITS)
        public = PublicPart.of(private, created)
        der = private.private_bytes(
            serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        cert_text = certificate(private, public, user_id, created=created)
        doc = {
            'format': FORMAT,
            'algorithm': f'rsa{RSA_BITS}',
            'created': created,
            'fingerprint': public.fingerprint_hex,
            'certificate': cert_text,
            'private': self.keyring.encrypt(der, aad=_aad(repo_id)),
        }
        # The certificate must read back as a valid, signing-capable key (adr/0006's reader).
        (cert,) = openpgp.read_certs(cert_text.encode())
        if cert.fingerprint != public.fingerprint_hex or not cert.primary.can_sign(created):
            raise CryptoError('the generated certificate does not verify')
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
