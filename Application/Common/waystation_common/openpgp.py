"""OpenPGP detached-signature verification (RFC 9580; version 4 keys and signatures), on ``cryptography``.

Used where an upstream publishes a file, a detached signature and the signer's public keys, e.g. a Terraform
provider's ``SHA256SUMS`` (FORMATS.md "Terraform / OpenTofu"). Why in-house: no maintained pure-Python OpenPGP
library works on Python 3.14 with the keys the public registries publish (adr/0006, proposed). Only the parsing is
ours; every cryptographic primitive is ``cryptography``'s.

What it checks, as the Terraform and OpenTofu clients (``ProtonMail/go-crypto``) do:

- ``read_certs`` reads armored or binary transferable public keys (several per block). A primary key counts only
  with a valid self-signature (a user ID certification or a direct-key signature, made by itself); a subkey only
  with a valid binding signature by its primary key. The newest valid self-signature decides key flags and
  expiry. A key revoked by itself (a valid key or subkey revocation signature) is never used.
- ``verify_detached`` accepts a version 4 binary or text signature (``0x00``/``0x01``) made by a signing-capable,
  unexpired, unrevoked key of the certificates, with an unexpired signature that names no unknown critical
  subpacket, over SHA-2 or SHA-3 (SHA-1 and MD5 are refused for data; SHA-1 is accepted in self-signatures of old
  keys, as GnuPG and go-crypto do). There is no web of trust: the caller decides which keys to trust.
- Algorithms: RSA (≥ 2048 bits), ECDSA on P-256/P-384/P-521, EdDSA (Ed25519 legacy and RFC 9580 Ed25519/Ed448).
  DSA, ElGamal, version 3 and version 6 packets are not supported (signatures by such keys do not verify).

Inputs are untrusted: lengths are bounded (``MAX_INPUT``, ``MAX_PACKETS``), malformed data raises
``OpenPgpError``, never another exception. Blocking (RSA): call in a thread on request paths.
"""

from __future__ import annotations

import base64
import hashlib
import re
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed, encode_dss_signature

MAX_INPUT = 4 * 1024 * 1024
MAX_PACKETS = 10_000
MIN_RSA_BITS = 2048

# Packet tags
SIGNATURE, PUBLIC_KEY, USER_ID, PUBLIC_SUBKEY, USER_ATTRIBUTE = 2, 6, 13, 14, 17
# Signature types
BINARY, TEXT = 0x00, 0x01
CERTIFICATIONS = frozenset({0x10, 0x11, 0x12, 0x13})
SUBKEY_BINDING, DIRECT_KEY, KEY_REVOCATION, SUBKEY_REVOCATION = 0x18, 0x1F, 0x20, 0x28
# Public-key algorithms
RSA_ALGOS = frozenset({1, 3})  # RSA (encrypt or sign), RSA sign-only
ECDSA, EDDSA_LEGACY, ED25519, ED448 = 19, 22, 27, 28
# Subpackets
SUB_CREATED, SUB_SIG_EXPIRES, SUB_KEY_EXPIRES, SUB_ISSUER, SUB_KEY_FLAGS, SUB_ISSUER_FPR = 2, 3, 9, 16, 27, 33
KNOWN_SUBPACKETS = frozenset({2, 3, 4, 5, 6, 7, 9, 11, 12, 16, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33,
                              34, 35, 37, 38, 39})  # fmt: skip
CAN_SIGN = 0x02

_HASHES: dict[int, type[hashes.HashAlgorithm]] = {
    2: hashes.SHA1,
    8: hashes.SHA256,
    9: hashes.SHA384,
    10: hashes.SHA512,
    11: hashes.SHA224,
    12: hashes.SHA3_256,
    14: hashes.SHA3_512,
}
_WEAK_HASHES = frozenset({2})  # accepted in self-signatures only
_CURVES: dict[bytes, ec.EllipticCurve] = {
    bytes.fromhex('2a8648ce3d030107'): ec.SECP256R1(),
    bytes.fromhex('2b81040022'): ec.SECP384R1(),
    bytes.fromhex('2b81040023'): ec.SECP521R1(),
}
_ED25519_OID = bytes.fromhex('2b06010401da470f01')
_ARMOR_BEGIN = re.compile(r'^-----BEGIN PGP ([A-Z ,/0-9]+)-----$')


class OpenPgpError(ValueError):
    """Malformed OpenPGP data, or a signature that does not verify."""


# ---------------------------------------------------------------------------- armor and packets
def _crc24(data: bytes) -> int:
    crc = 0xB704CE
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    return crc & 0xFFFFFF


def dearmor(data: bytes) -> bytes:
    """Binary OpenPGP data from ASCII armor (every armored block, concatenated); binary input is returned as is."""
    if len(data) > MAX_INPUT:
        raise OpenPgpError('OpenPGP data is too large')
    if not data.lstrip().startswith(b'-----BEGIN PGP '):
        return data
    try:
        lines = data.decode('ascii').splitlines()
    except UnicodeDecodeError:
        raise OpenPgpError('armored OpenPGP data is not ASCII') from None
    out = bytearray()
    i = 0
    found = False
    while i < len(lines):
        begin = _ARMOR_BEGIN.match(lines[i].strip())
        i += 1
        if begin is None:
            continue
        kind = begin.group(1)
        while i < len(lines) and ':' in lines[i] and lines[i].strip():  # armor headers
            i += 1
        body: list[str] = []
        checksum: str | None = None
        while i < len(lines) and not lines[i].strip().startswith('-----END PGP '):
            line = lines[i].strip()
            if line.startswith('=') and len(line) == 5:
                checksum = line[1:]
            elif line:
                body.append(line)
            i += 1
        if i >= len(lines) or lines[i].strip() != f'-----END PGP {kind}-----':
            raise OpenPgpError('armored OpenPGP data has no end line')
        i += 1
        try:
            block = base64.b64decode(''.join(body), validate=True)
            crc = base64.b64decode(checksum, validate=True) if checksum is not None else None
        except ValueError:
            raise OpenPgpError('armored OpenPGP data is not base64') from None
        if crc is not None and int.from_bytes(crc, 'big') != _crc24(block):
            raise OpenPgpError('armored OpenPGP data has a bad checksum')
        out += block
        found = True
    if not found:
        raise OpenPgpError('no armored OpenPGP data')
    return bytes(out)


def packets(data: bytes) -> Iterator[tuple[int, bytes]]:
    """``(tag, body)`` of each packet. Partial (streamed) lengths are refused: keys and signatures never use them."""
    pos = 0
    count = 0
    while pos < len(data):
        count += 1
        if count > MAX_PACKETS:
            raise OpenPgpError('too many OpenPGP packets')
        ctb = data[pos]
        pos += 1
        if not ctb & 0x80:
            raise OpenPgpError('not an OpenPGP packet')
        if ctb & 0x40:  # new format
            tag = ctb & 0x3F
            if pos >= len(data):
                raise OpenPgpError('truncated OpenPGP packet')
            first = data[pos]
            if first < 192:
                length, pos = first, pos + 1
            elif first < 224:
                if pos + 1 >= len(data):
                    raise OpenPgpError('truncated OpenPGP packet')
                length, pos = ((first - 192) << 8) + data[pos + 1] + 192, pos + 2
            elif first == 255:
                length, pos = int.from_bytes(data[pos + 1 : pos + 5], 'big'), pos + 5
            else:
                raise OpenPgpError('partial-length OpenPGP packets are not supported here')
        else:
            tag = (ctb >> 2) & 0x0F
            size = (1, 2, 4, 0)[ctb & 0x03]
            if size == 0:
                length = len(data) - pos
            else:
                length = int.from_bytes(data[pos : pos + size], 'big')
                pos += size
        if pos > len(data) or pos + length > len(data):
            raise OpenPgpError('truncated OpenPGP packet')
        yield tag, data[pos : pos + length]
        pos += length


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise OpenPgpError('truncated OpenPGP packet')
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out

    def byte(self) -> int:
        return self.take(1)[0]

    def number(self, n: int) -> int:
        return int.from_bytes(self.take(n), 'big')

    def mpi(self) -> bytes:
        bits = self.number(2)
        return self.take((bits + 7) // 8)

    def oid(self) -> bytes:
        size = self.byte()
        if size in (0, 0xFF):
            raise OpenPgpError('invalid curve OID')
        return self.take(size)

    def rest(self) -> bytes:
        return self.take(len(self.data) - self.pos)


# ---------------------------------------------------------------------------- keys
Verifier = Any  # a cryptography public key object


@dataclass
class Key:
    """A version 4 public key or subkey."""

    body: bytes  # the packet body (hashed into fingerprints and self-signatures)
    algorithm: int
    created: int
    public: Verifier | None  # None: an algorithm that cannot verify signatures here
    fingerprint: bytes = b''
    flags: int | None = None  # key flags of the newest valid self-signature (None: not stated)
    expires: int | None = None  # absolute time (None: never)
    revoked: bool = False
    valid: bool = False  # has a valid self-signature (primary) or binding (subkey)
    _newest: int = field(default=-1, repr=False)

    @property
    def key_id(self) -> bytes:
        return self.fingerprint[-8:]

    def prefix(self) -> bytes:
        return b'\x99' + len(self.body).to_bytes(2, 'big') + self.body

    def can_sign(self, now: int) -> bool:
        if not self.valid or self.revoked or self.public is None:
            return False
        if self.flags is not None and not self.flags & CAN_SIGN:
            return False
        return self.expires is None or now < self.expires


def _rsa(r: _Reader) -> Verifier:
    n = int.from_bytes(r.mpi(), 'big')
    e = int.from_bytes(r.mpi(), 'big')
    if n.bit_length() < MIN_RSA_BITS:
        return None
    try:
        return rsa.RSAPublicNumbers(e, n).public_key()
    except ValueError:
        raise OpenPgpError('invalid RSA key') from None


def _ecdsa(r: _Reader) -> Verifier:
    curve = _CURVES.get(r.oid())
    point = r.mpi()
    if curve is None:
        return None
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(curve, point)
    except ValueError:
        raise OpenPgpError('invalid ECDSA key') from None


def _eddsa_legacy(r: _Reader) -> Verifier:
    oid = r.oid()
    point = r.mpi()
    if oid != _ED25519_OID:
        return None
    if len(point) != 33 or point[0] != 0x40:
        raise OpenPgpError('invalid EdDSA key')
    return ed25519.Ed25519PublicKey.from_public_bytes(point[1:])


def parse_key(body: bytes) -> Key | None:
    """A public key packet body (version 4 only; None for other versions)."""
    r = _Reader(body)
    if r.byte() != 4:
        return None
    created = r.number(4)
    algorithm = r.byte()
    public: Verifier = None
    if algorithm in RSA_ALGOS:
        public = _rsa(r)
    elif algorithm == ECDSA:
        public = _ecdsa(r)
    elif algorithm == EDDSA_LEGACY:
        public = _eddsa_legacy(r)
    elif algorithm == ED25519:
        public = ed25519.Ed25519PublicKey.from_public_bytes(r.take(32))
    elif algorithm == ED448:
        public = ed448.Ed448PublicKey.from_public_bytes(r.take(57))
    fingerprint = hashlib.sha1(b'\x99' + len(body).to_bytes(2, 'big') + body, usedforsecurity=False).digest()
    return Key(body=body, algorithm=algorithm, created=created, public=public, fingerprint=fingerprint)


# ---------------------------------------------------------------------------- signatures
@dataclass(frozen=True)
class Signature:
    type: int
    algorithm: int
    hash_algorithm: int
    hashed_part: bytes  # version .. hashed subpackets (the signed trailer)
    values: tuple[bytes, ...]  # algorithm-specific: RSA (s,), ECDSA/EdDSA (r, s), Ed25519/Ed448 (sig,)
    created: int | None
    expires_after: int | None  # seconds after creation (0: never)
    key_expires_after: int | None
    flags: int | None
    issuer_ids: frozenset[bytes]  # key IDs (8 bytes) named by issuer and issuer fingerprint subpackets
    issuer_fprs: frozenset[bytes]
    unknown_critical: bool

    def names(self, key: Key) -> bool:
        """Whether the signature's issuer subpackets name ``key`` (no issuer named: any key may be tried)."""
        if not self.issuer_ids and not self.issuer_fprs:
            return True
        return key.fingerprint in self.issuer_fprs or key.key_id in self.issuer_ids

    def expired(self, now: int) -> bool:
        return bool(self.expires_after) and self.created is not None and now >= self.created + (self.expires_after or 0)


def _subpackets(data: bytes) -> Iterator[tuple[int, bool, bytes]]:
    r = _Reader(data)
    while r.pos < len(data):
        first = r.byte()
        if first < 192:
            length = first
        elif first < 255:
            length = ((first - 192) << 8) + r.byte() + 192
        else:
            length = r.number(4)
        if length == 0:
            raise OpenPgpError('empty signature subpacket')
        body = r.take(length)
        yield body[0] & 0x7F, bool(body[0] & 0x80), body[1:]


def parse_signature(body: bytes) -> Signature | None:
    """A signature packet body (version 4 only; None for other versions)."""
    r = _Reader(body)
    if r.byte() != 4:
        return None
    sig_type, algorithm, hash_algorithm = r.byte(), r.byte(), r.byte()
    hashed = r.take(r.number(2))
    hashed_part = body[: r.pos]
    unhashed = r.take(r.number(2))
    r.take(2)  # left 16 bits of the hash (a quick check only; the signature decides)
    values: tuple[bytes, ...]
    if algorithm in RSA_ALGOS:
        values = (r.mpi(),)
    elif algorithm in (ECDSA, EDDSA_LEGACY):
        values = (r.mpi(), r.mpi())
    elif algorithm == ED25519:
        values = (r.take(64),)
    elif algorithm == ED448:
        values = (r.take(114),)
    else:
        values = (r.rest(),)
    fields: dict[int, bytes] = {}
    ids: set[bytes] = set()
    fprs: set[bytes] = set()
    unknown_critical = False
    for area, data in ((True, hashed), (False, unhashed)):
        for kind, critical, value in _subpackets(data):
            if area and kind not in fields:
                fields[kind] = value
            if critical and area and kind not in KNOWN_SUBPACKETS:
                unknown_critical = True
            if kind == SUB_ISSUER and len(value) == 8:
                ids.add(value)
            elif kind == SUB_ISSUER_FPR and len(value) == 21 and value[0] == 4:
                fprs.add(value[1:])
                ids.add(value[-8:])

    def number(kind: int) -> int | None:
        value = fields.get(kind)
        return int.from_bytes(value, 'big') if value is not None and len(value) == 4 else None

    flags = fields.get(SUB_KEY_FLAGS)
    return Signature(
        type=sig_type,
        algorithm=algorithm,
        hash_algorithm=hash_algorithm,
        hashed_part=hashed_part,
        values=values,
        created=number(SUB_CREATED),
        expires_after=number(SUB_SIG_EXPIRES),
        key_expires_after=number(SUB_KEY_EXPIRES),
        flags=flags[0] if flags else None,
        issuer_ids=frozenset(ids),
        issuer_fprs=frozenset(fprs),
        unknown_critical=unknown_critical,
    )


def _digest(sig: Signature, signed: bytes, *, allow_weak: bool) -> tuple[bytes, hashes.HashAlgorithm] | None:
    algorithm = _HASHES.get(sig.hash_algorithm)
    if algorithm is None or (sig.hash_algorithm in _WEAK_HASHES and not allow_weak):
        return None
    h = hashes.Hash(algorithm())
    h.update(signed)
    h.update(sig.hashed_part)
    h.update(b'\x04\xff' + len(sig.hashed_part).to_bytes(4, 'big'))
    return h.finalize(), algorithm()


def _check(key: Key, sig: Signature, signed: bytes, *, allow_weak: bool = False) -> bool:
    """Whether ``sig`` over ``signed`` was made by ``key`` (no policy beyond the hash algorithm)."""
    if key.public is None or sig.algorithm != key.algorithm or sig.unknown_critical or sig.created is None:
        return False
    digested = _digest(sig, signed, allow_weak=allow_weak)
    if digested is None:
        return False
    digest, algorithm = digested
    public = key.public
    try:
        if isinstance(public, rsa.RSAPublicKey):
            size = (public.key_size + 7) // 8
            value = sig.values[0]
            if len(value) > size:
                return False
            public.verify(value.rjust(size, b'\x00'), digest, padding.PKCS1v15(), Prehashed(algorithm))
        elif isinstance(public, ec.EllipticCurvePublicKey):
            r, s = (int.from_bytes(v, 'big') for v in sig.values)
            public.verify(encode_dss_signature(r, s), digest, ec.ECDSA(Prehashed(algorithm)))
        elif isinstance(public, ed25519.Ed25519PublicKey):
            if sig.algorithm == EDDSA_LEGACY:
                r, s = sig.values
                if len(r) > 32 or len(s) > 32:
                    return False
                raw = r.rjust(32, b'\x00') + s.rjust(32, b'\x00')
            else:
                raw = sig.values[0]
            public.verify(raw, digest)
        else:
            assert isinstance(public, ed448.Ed448PublicKey)
            public.verify(sig.values[0], digest)
    except InvalidSignature, UnsupportedAlgorithm, ValueError:
        return False
    return True


# ---------------------------------------------------------------------------- certificates
@dataclass
class Cert:
    """A transferable public key: the primary key and its subkeys, with self-signatures applied."""

    primary: Key
    subkeys: list[Key] = field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        return self.primary.fingerprint.hex().upper()

    def keys(self) -> list[Key]:
        return [self.primary, *self.subkeys]


def _apply(key: Key, sig: Signature) -> None:
    """A valid self-signature (certification, direct-key or binding) for ``key``: the newest decides."""
    key.valid = True
    created = sig.created or 0
    if created < key._newest:
        return
    key._newest = created
    if sig.flags is not None:
        key.flags = sig.flags
    key.expires = key.created + sig.key_expires_after if sig.key_expires_after else None


def _certificate(primary: Key, items: list[tuple[int, bytes]]) -> Cert:
    cert = Cert(primary)
    prefix = primary.prefix()
    subject: tuple[int, bytes] | None = None  # the user ID / attribute being certified
    subkey: Key | None = None
    in_subkey = False
    for tag, body in items:
        if tag in (USER_ID, USER_ATTRIBUTE):
            subject, in_subkey, subkey = (tag, body), False, None
        elif tag == PUBLIC_SUBKEY:
            subkey, in_subkey, subject = parse_key(body), True, None
            if subkey is not None:
                cert.subkeys.append(subkey)
        elif tag == SIGNATURE:
            sig = parse_signature(body)
            if sig is None or not sig.names(primary):
                continue
            if in_subkey:
                if subkey is None or sig.type not in (SUBKEY_BINDING, SUBKEY_REVOCATION):
                    continue
                if _check(primary, sig, prefix + subkey.prefix(), allow_weak=True):
                    if sig.type == SUBKEY_REVOCATION:
                        subkey.revoked = True
                    else:
                        _apply(subkey, sig)
            elif sig.type in (DIRECT_KEY, KEY_REVOCATION) and subject is None:
                if _check(primary, sig, prefix, allow_weak=True):
                    if sig.type == KEY_REVOCATION:
                        primary.revoked = True
                    else:
                        _apply(primary, sig)
            elif sig.type in CERTIFICATIONS and subject is not None:
                lead = b'\xb4' if subject[0] == USER_ID else b'\xd1'
                signed = prefix + lead + len(subject[1]).to_bytes(4, 'big') + subject[1]
                if _check(primary, sig, signed, allow_weak=True):
                    _apply(primary, sig)
    if primary.revoked:
        for key in cert.subkeys:
            key.revoked = True
    return cert


def read_certs(data: bytes) -> list[Cert]:
    """Every version 4 transferable public key in ``data`` (armored or binary). Keys of other versions and packets
    that are not part of a public key (secret keys refused) are skipped; malformed data raises ``OpenPgpError``."""
    raw = dearmor(data)
    certs: list[Cert] = []
    primary: Key | None = None
    items: list[tuple[int, bytes]] = []
    for tag, body in packets(raw):
        if tag in (5, 7):
            raise OpenPgpError('secret key material is not accepted')
        if tag == PUBLIC_KEY:
            if primary is not None:
                certs.append(_certificate(primary, items))
            primary, items = parse_key(body), []
        elif primary is not None:
            items.append((tag, body))
    if primary is not None:
        certs.append(_certificate(primary, items))
    return certs


# ---------------------------------------------------------------------------- detached signatures
@dataclass(frozen=True)
class Verified:
    cert_fingerprint: str  # primary key fingerprint (hex, upper-case)
    key_fingerprint: str  # the (sub)key that made the signature
    created: int  # signature creation time (seconds since the epoch)


def _canonical_text(data: bytes) -> bytes:
    return data.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')


def verify_detached(data: bytes, signature: bytes, certs: Sequence[Cert], *, now: int | None = None) -> Verified:
    """Check a detached signature (armored or binary; the first valid one of several counts) over ``data``.
    Returns which key made it; raises ``OpenPgpError`` when no signature verifies with a usable key."""
    when = int(time.time()) if now is None else now
    found = False
    for tag, body in packets(dearmor(signature)):
        if tag != SIGNATURE:
            continue
        sig = parse_signature(body)
        if sig is None or sig.type not in (BINARY, TEXT):
            continue
        found = True
        if sig.expired(when):
            continue
        signed = _canonical_text(data) if sig.type == TEXT else data
        for cert in certs:
            for key in cert.keys():
                if sig.names(key) and key.can_sign(when) and _check(key, sig, signed):
                    return Verified(cert.fingerprint, key.fingerprint.hex().upper(), sig.created or 0)
    if not found:
        raise OpenPgpError('no version 4 data signature found')
    raise OpenPgpError('the signature does not verify with the given keys')
