"""In-house OpenPGP (RFC 9580 / RFC 4880, version 4 keys and signatures) on ``cryptography``: reading public keys
and verifying detached signatures, plus the packet encoders ``waystation_common.signing`` builds keys and
signatures with (ARCHITECTURE §2; adr/0007).

Deliberately small. What it reads:

- **Armor** (``-----BEGIN PGP ...-----``, optional headers, base64, optional CRC-24 checked when present) or binary
  packets; several armored blocks in one text are all read.
- **Packets** with new- or old-format headers and definite lengths only (partial / indeterminate lengths, used for
  streamed data, never appear in keys or detached signatures and are refused), bounded in size and count.
- **Public keys and subkeys** (tags 6 and 14), version 4: RSA (1, 2, 3), EdDSA legacy with Ed25519 (22) and
  Ed25519 (27). Other algorithms are skipped, not guessed. A key block configured by an operator is trusted as given:
  binding and revocation signatures, expiry and key flags are **not** evaluated (the operator chose the keys).
- **Signatures** (tag 2), version 4, of a binary (0x00) or text (0x01) document with SHA-224/256/384/512. SHA-1 and
  MD5 signatures are refused. A critical hashed subpacket of an unknown type refuses the signature (RFC 9580
  "critical" bit); an expired signature (creation time + signature expiration) is refused.

Pure CPU work on bytes: callers on the event loop run it in a thread (ARCHITECTURE §9).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa, utils
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from waystation_common.errors import ValidationFailed

MAX_INPUT = 1024 * 1024
MAX_PACKETS = 4096

TAG_SIGNATURE = 2
TAG_PUBLIC_KEY = 6
TAG_USER_ID = 13
TAG_PUBLIC_SUBKEY = 14

ALG_RSA = (1, 2, 3)
ALG_RSA_SIGN = 1
ALG_EDDSA_LEGACY = 22
ALG_ED25519 = 27
ED25519_OID = bytes.fromhex('2b06010401da470f01')

HASHES: dict[int, tuple[str, type[hashes.HashAlgorithm]]] = {
    8: ('sha256', hashes.SHA256),
    9: ('sha384', hashes.SHA384),
    10: ('sha512', hashes.SHA512),
    11: ('sha224', hashes.SHA224),
}
HASH_SHA256 = 8
HASH_SHA512 = 10

SIG_BINARY = 0x00
SIG_TEXT = 0x01
SIG_POSITIVE_CERT = 0x13

# Signature subpacket types (RFC 9580).
SUB_CREATED = 2
SUB_SIG_EXPIRES = 3
SUB_KEY_EXPIRES = 9
SUB_PREF_SYMMETRIC = 11
SUB_ISSUER = 16
SUB_PREF_HASH = 21
SUB_PREF_COMPRESSION = 22
SUB_KEYSERVER_PREFS = 23
SUB_PRIMARY_UID = 25
SUB_KEY_FLAGS = 27
SUB_FEATURES = 30
SUB_ISSUER_FPR = 33
KNOWN_SUBPACKETS = frozenset(
    {
        SUB_CREATED,
        SUB_SIG_EXPIRES,
        SUB_KEY_EXPIRES,
        SUB_PREF_SYMMETRIC,
        SUB_ISSUER,
        SUB_PREF_HASH,
        SUB_PREF_COMPRESSION,
        SUB_KEYSERVER_PREFS,
        SUB_PRIMARY_UID,
        SUB_KEY_FLAGS,
        SUB_FEATURES,
        SUB_ISSUER_FPR,
    }
)

_BEGIN = re.compile(r'^-----BEGIN PGP ([A-Z ]+)-----$')
_HEADER = re.compile(r'^[A-Za-z][A-Za-z0-9-]*: ')


class OpenPgpError(ValidationFailed):
    """Malformed or unsupported OpenPGP data, or a signature that does not verify."""

    code = 'openpgp'


# ---------------------------------------------------------------------------- armor
def crc24(data: bytes) -> int:
    crc = 0xB704CE
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    return crc & 0xFFFFFF


def armor(kind: str, data: bytes) -> str:
    """ASCII armor (``kind``: ``PUBLIC KEY BLOCK``, ``SIGNATURE``), 64-column base64 with the CRC-24 line."""
    body = base64.b64encode(data).decode('ascii')
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    check = base64.b64encode(crc24(data).to_bytes(3, 'big')).decode('ascii')
    return '\n'.join([f'-----BEGIN PGP {kind}-----', '', *lines, f'={check}', f'-----END PGP {kind}-----', ''])


def dearmor(data: bytes | str, kind: str | None = None) -> list[bytes]:
    """The binary contents of every armored block in ``data`` (of type ``kind`` when given); binary input (no
    ``-----BEGIN PGP`` line) is returned as one block."""
    raw = data.encode('utf-8', 'replace') if isinstance(data, str) else data
    if len(raw) > MAX_INPUT:
        raise OpenPgpError('OpenPGP data is too large')
    if b'-----BEGIN ' not in raw:
        if not raw:
            raise OpenPgpError('no OpenPGP data')
        return [raw]
    blocks: list[bytes] = []
    lines = iter(raw.decode('ascii', 'replace').splitlines())
    for line in lines:
        begin = _BEGIN.match(line.strip())
        if begin is None:
            continue
        blocks_kind = begin.group(1)
        block = _read_block(lines, blocks_kind)
        if kind is None or blocks_kind == kind:
            blocks.append(block)
    if not blocks:
        raise OpenPgpError('no armored OpenPGP block found')
    return blocks


def _read_block(lines: Iterator[str], kind: str) -> bytes:
    body: list[str] = []
    checksum: str | None = None
    in_headers = True
    for raw_line in lines:
        line = raw_line.strip()
        if line == f'-----END PGP {kind}-----':
            return _decode_body(body, checksum)
        if in_headers:
            if not line:
                in_headers = False
                continue
            if _HEADER.match(line):
                continue
            in_headers = False
        if line.startswith('=') and len(line) == 5:
            checksum = line[1:]
        elif line:
            body.append(line)
    raise OpenPgpError('armored OpenPGP block is not terminated')


def _decode_body(body: list[str], checksum: str | None) -> bytes:
    try:
        data = base64.b64decode(''.join(body), validate=True)
        expected = base64.b64decode(checksum, validate=True) if checksum is not None else None
    except binascii.Error, ValueError:
        raise OpenPgpError('armored OpenPGP block is not valid base64') from None
    if expected is not None and crc24(data).to_bytes(3, 'big') != expected:
        raise OpenPgpError('armored OpenPGP block fails its checksum')
    return data


# ---------------------------------------------------------------------------- packets
@dataclass(frozen=True)
class Packet:
    tag: int
    body: bytes


def packets(data: bytes) -> list[Packet]:
    """Split binary OpenPGP data into packets (definite lengths only, bounded)."""
    out: list[Packet] = []
    pos = 0
    while pos < len(data):
        if len(out) >= MAX_PACKETS:
            raise OpenPgpError('too many OpenPGP packets')
        first = data[pos]
        if not first & 0x80:
            raise OpenPgpError('not an OpenPGP packet')
        pos += 1
        if first & 0x40:
            tag = first & 0x3F
            length, pos = _new_length(data, pos)
        else:
            tag = (first >> 2) & 0x0F
            size = {0: 1, 1: 2, 2: 4}.get(first & 0x03)
            if size is None:
                raise OpenPgpError('indeterminate OpenPGP packet lengths are not supported')
            length, pos = int.from_bytes(_take(data, pos, size), 'big'), pos + size
        out.append(Packet(tag, _take(data, pos, length)))
        pos += length
    return out


def _take(data: bytes, pos: int, length: int) -> bytes:
    if pos + length > len(data):
        raise OpenPgpError('truncated OpenPGP data')
    return data[pos : pos + length]


def _new_length(data: bytes, pos: int) -> tuple[int, int]:
    o1 = _take(data, pos, 1)[0]
    if o1 < 192:
        return o1, pos + 1
    if o1 < 224:
        return ((o1 - 192) << 8) + _take(data, pos + 1, 1)[0] + 192, pos + 2
    if o1 == 255:
        return int.from_bytes(_take(data, pos + 1, 4), 'big'), pos + 5
    raise OpenPgpError('partial OpenPGP packet lengths are not supported')


def encode_packet(tag: int, body: bytes) -> bytes:
    """``body`` with a new-format packet header (one-, two- or five-octet length)."""
    n = len(body)
    if n < 192:
        head = bytes([n])
    elif n < 8384:
        n -= 192
        head = bytes([(n >> 8) + 192, n & 0xFF])
    else:
        head = b'\xff' + n.to_bytes(4, 'big')
    return bytes([0xC0 | tag]) + head + body


def mpi(value: int | bytes) -> bytes:
    raw = value if isinstance(value, bytes) else value.to_bytes((value.bit_length() + 7) // 8 or 1, 'big')
    raw = raw.lstrip(b'\x00') or b'\x00'
    bits = 0 if raw == b'\x00' else (len(raw) - 1) * 8 + raw[0].bit_length()
    return bits.to_bytes(2, 'big') + (raw if bits else b'')


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        out = _take(self.data, self.pos, n)
        self.pos += n
        return out

    def byte(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return int.from_bytes(self.take(2), 'big')

    def u32(self) -> int:
        return int.from_bytes(self.take(4), 'big')

    def mpi(self) -> bytes:
        bits = self.u16()
        return self.take((bits + 7) // 8)

    def rest(self) -> bytes:
        return self.take(len(self.data) - self.pos)


# ---------------------------------------------------------------------------- public keys
@dataclass(frozen=True)
class PublicKey:
    """A version 4 public key or subkey Waystation can verify with."""

    algorithm: int
    created: int
    body: bytes  # the packet body (fingerprint input)
    material: tuple[bytes, ...]  # RSA (n, e); EdDSA (32-byte point)
    fingerprint: bytes = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, 'fingerprint', v4_fingerprint(self.body))

    @property
    def key_id(self) -> bytes:
        return self.fingerprint[-8:]

    @property
    def fingerprint_hex(self) -> str:
        return self.fingerprint.hex().upper()

    def verify(
        self, signature: bytes | tuple[bytes, bytes], digest: bytes, hash_cls: type[hashes.HashAlgorithm]
    ) -> bool:
        try:
            if self.algorithm in ALG_RSA:
                n, e = (int.from_bytes(x, 'big') for x in self.material)
                key = rsa.RSAPublicNumbers(e, n).public_key()
                raw = signature if isinstance(signature, bytes) else signature[0]
                size = (key.key_size + 7) // 8
                if len(raw) > size:
                    return False
                key.verify(raw.rjust(size, b'\x00'), digest, padding.PKCS1v15(), utils.Prehashed(hash_cls()))
                return True
            if isinstance(signature, bytes):
                raw = signature
            else:
                raw = signature[0].rjust(32, b'\x00') + signature[1].rjust(32, b'\x00')
            ed25519.Ed25519PublicKey.from_public_bytes(self.material[0]).verify(raw, digest)
            return True
        except InvalidSignature, ValueError:
            return False


def v4_fingerprint(body: bytes) -> bytes:
    return hashlib.sha1(b'\x99' + len(body).to_bytes(2, 'big') + body, usedforsecurity=False).digest()


def parse_public_key(body: bytes) -> PublicKey | None:
    """A key packet body, or None when its version or algorithm is not supported."""
    r = _Reader(body)
    if r.byte() != 4:
        return None
    created = r.u32()
    algorithm = r.byte()
    if algorithm in ALG_RSA:
        n, e = r.mpi(), r.mpi()
        return PublicKey(algorithm, created, body, (n, e))
    if algorithm == ALG_EDDSA_LEGACY:
        oid = r.take(r.byte())
        point = r.mpi()
        if oid != ED25519_OID or len(point) != 33 or point[0] != 0x40:
            return None
        return PublicKey(algorithm, created, body, (point[1:],))
    if algorithm == ALG_ED25519:
        return PublicKey(algorithm, created, body, (r.take(32),))
    return None


def read_public_keys(data: bytes | str) -> list[PublicKey]:
    """Every supported primary key and subkey in ``data`` (armored ``PUBLIC KEY BLOCK``s or binary)."""
    keys: list[PublicKey] = []
    for block in dearmor(data, 'PUBLIC KEY BLOCK'):
        for packet in packets(block):
            if packet.tag in (TAG_PUBLIC_KEY, TAG_PUBLIC_SUBKEY):
                key = parse_public_key(packet.body)
                if key is not None:
                    keys.append(key)
    if not keys:
        raise OpenPgpError('no supported OpenPGP public key (RSA or Ed25519, version 4) found')
    return keys


# ---------------------------------------------------------------------------- signatures
@dataclass(frozen=True)
class Signature:
    sig_type: int
    algorithm: int
    hash_algorithm: int
    hashed: bytes  # version .. end of the hashed subpackets (the hash trailer input)
    left16: bytes
    value: bytes | tuple[bytes, bytes]
    created: int | None
    expires: int | None  # seconds after creation, None: never
    issuers: tuple[bytes, ...]  # key ids and fingerprints the signature names
    critical_unknown: bool


def _subpackets(data: bytes) -> Iterator[tuple[int, bool, bytes]]:
    r = _Reader(data)
    while r.pos < len(data):
        o1 = r.byte()
        if o1 < 192:
            length = o1
        elif o1 < 255:
            length = ((o1 - 192) << 8) + r.byte() + 192
        else:
            length = r.u32()
        if length == 0:
            raise OpenPgpError('empty signature subpacket')
        content = r.take(length)
        yield content[0] & 0x7F, bool(content[0] & 0x80), content[1:]


def parse_signature(body: bytes) -> Signature | None:
    """A signature packet body, or None when its version or algorithm is not supported."""
    r = _Reader(body)
    if r.byte() != 4:
        return None
    sig_type, algorithm, hash_algorithm = r.byte(), r.byte(), r.byte()
    hashed_area = r.take(r.u16())
    hashed = body[: r.pos]
    unhashed_area = r.take(r.u16())
    left16 = r.take(2)
    value: bytes | tuple[bytes, bytes]
    if algorithm in ALG_RSA:
        value = r.mpi()
    elif algorithm == ALG_EDDSA_LEGACY:
        value = (r.mpi(), r.mpi())
    elif algorithm == ALG_ED25519:
        value = r.take(64)
    else:
        return None
    created: int | None = None
    expires: int | None = None
    issuers: list[bytes] = []
    critical_unknown = False
    for area, is_hashed in ((hashed_area, True), (unhashed_area, False)):
        for kind, critical, content in _subpackets(area):
            if is_hashed and critical and kind not in KNOWN_SUBPACKETS:
                critical_unknown = True
            if kind == SUB_CREATED and is_hashed and len(content) == 4:
                created = int.from_bytes(content, 'big')
            elif kind == SUB_SIG_EXPIRES and is_hashed and len(content) == 4:
                expires = int.from_bytes(content, 'big') or None
            elif kind == SUB_ISSUER and len(content) == 8:
                issuers.append(content)
            elif kind == SUB_ISSUER_FPR and len(content) > 1:
                issuers.append(content[1:])
    return Signature(
        sig_type=sig_type,
        algorithm=algorithm,
        hash_algorithm=hash_algorithm,
        hashed=hashed,
        left16=left16,
        value=value,
        created=created,
        expires=expires,
        issuers=tuple(issuers),
        critical_unknown=critical_unknown,
    )


def read_signatures(data: bytes | str) -> list[Signature]:
    """The supported signatures in ``data`` (an armored ``SIGNATURE`` or binary packets)."""
    out: list[Signature] = []
    for block in dearmor(data, 'SIGNATURE'):
        for packet in packets(block):
            if packet.tag == TAG_SIGNATURE:
                sig = parse_signature(packet.body)
                if sig is not None:
                    out.append(sig)
    if not out:
        raise OpenPgpError('no supported OpenPGP signature found')
    return out


def canonical_text(data: bytes) -> bytes:
    """Line endings as CRLF (the input of a text-mode signature)."""
    return data.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')


def trailer(hashed: bytes) -> bytes:
    return hashed + b'\x04\xff' + len(hashed).to_bytes(4, 'big')


def _names(sig: Signature, key: PublicKey) -> bool:
    return not sig.issuers or any(i in (key.key_id, key.fingerprint) for i in sig.issuers)


def verify_detached(
    data: bytes, signature: bytes | str, keys: Sequence[PublicKey], *, now: float | None = None
) -> PublicKey:
    """The key that made a valid signature of ``data`` among the signatures in ``signature``; raises
    ``OpenPgpError`` when none verifies with any of ``keys``."""
    sigs = read_signatures(signature)
    moment = time.time() if now is None else now
    reasons: set[str] = set()
    for sig in sigs:
        if sig.sig_type not in (SIG_BINARY, SIG_TEXT):
            reasons.add('not a document signature')
            continue
        algorithm = HASHES.get(sig.hash_algorithm)
        if algorithm is None:
            reasons.add('unsupported or weak hash algorithm')
            continue
        if sig.critical_unknown:
            reasons.add('unknown critical subpacket')
            continue
        if sig.created is not None and sig.expires is not None and moment > sig.created + sig.expires:
            reasons.add('signature expired')
            continue
        name, hash_cls = algorithm
        content = canonical_text(data) if sig.sig_type == SIG_TEXT else data
        digest = hashlib.new(name, content + trailer(sig.hashed)).digest()
        if digest[:2] != sig.left16:
            reasons.add('signature does not match the data')
            continue
        for key in keys:
            if not _names(sig, key):
                continue
            if (sig.algorithm in ALG_RSA) != (key.algorithm in ALG_RSA):
                continue
            if key.verify(sig.value, digest, hash_cls):
                return key
        reasons.add('no configured key made the signature')
    raise OpenPgpError('OpenPGP signature does not verify: ' + ', '.join(sorted(reasons)))


# ---------------------------------------------------------------------------- encoding (signing side)
def subpacket(kind: int, content: bytes, *, critical: bool = False) -> bytes:
    data = bytes([kind | (0x80 if critical else 0)]) + content
    n = len(data)
    if n < 192:
        return bytes([n]) + data
    if n < 8384:
        n -= 192
        return bytes([(n >> 8) + 192, n & 0xFF]) + data
    return b'\xff' + n.to_bytes(4, 'big') + data


def rsa_key_body(public: rsa.RSAPublicKey, created: int) -> bytes:
    numbers = public.public_numbers()
    return bytes([4]) + created.to_bytes(4, 'big') + bytes([ALG_RSA_SIGN]) + mpi(numbers.n) + mpi(numbers.e)


def ed25519_key_body(public: ed25519.Ed25519PublicKey, created: int) -> bytes:
    """EdDSA legacy (algorithm 22): what GnuPG 2.2+, rpm (Sequoia) and librepo read for v4 keys."""
    point = b'\x40' + public.public_bytes(Encoding.Raw, PublicFormat.Raw)
    oid = bytes([len(ED25519_OID)]) + ED25519_OID
    return bytes([4]) + created.to_bytes(4, 'big') + bytes([ALG_EDDSA_LEGACY]) + oid + mpi(point)


SigningKey = rsa.RSAPrivateKey | ed25519.Ed25519PrivateKey


def make_signature(
    private: SigningKey,
    key: PublicKey,
    sig_type: int,
    prefix: bytes,
    *,
    created: int,
    hashed_extra: bytes = b'',
    hash_algorithm: int = HASH_SHA512,
) -> bytes:
    """A version 4 signature packet (with header) over ``prefix`` (the document, or the key and user ID being
    certified) by ``private``, whose public half is ``key``. Hashed: creation time, issuer fingerprint and
    ``hashed_extra``; unhashed: the issuer key id."""
    name, hash_cls = HASHES[hash_algorithm]
    hashed_area = (
        subpacket(SUB_CREATED, created.to_bytes(4, 'big'))
        + subpacket(SUB_ISSUER_FPR, b'\x04' + key.fingerprint)
        + hashed_extra
    )
    hashed = bytes([4, sig_type, key.algorithm, hash_algorithm]) + len(hashed_area).to_bytes(2, 'big') + hashed_area
    digest = hashlib.new(name, prefix + trailer(hashed)).digest()
    if isinstance(private, rsa.RSAPrivateKey):
        value = mpi(private.sign(digest, padding.PKCS1v15(), utils.Prehashed(hash_cls())))
    else:
        raw = private.sign(digest)
        value = mpi(raw[:32]) + mpi(raw[32:])
    unhashed = subpacket(SUB_ISSUER, key.key_id)
    body = hashed + len(unhashed).to_bytes(2, 'big') + unhashed + digest[:2] + value
    return encode_packet(TAG_SIGNATURE, body)


def sign_detached(private: SigningKey, key: PublicKey, data: bytes, *, created: int | None = None) -> str:
    """An armored detached binary-document signature of ``data`` (``repomd.xml.asc``, ``Release.gpg``)."""
    when = int(time.time()) if created is None else created
    return armor('SIGNATURE', make_signature(private, key, SIG_BINARY, data, created=when))


def certificate(private: SigningKey, key: PublicKey, user_id: str, *, created: int | None = None) -> str:
    """The armored transferable public key: the key, one user ID and its positive self-certification (key flags:
    certify and sign; preferred hashes SHA-512, SHA-256), as ``rpm --import`` / ``gpg --import`` expect."""
    when = int(time.time()) if created is None else created
    uid = user_id.encode('utf-8')
    prefix = b'\x99' + len(key.body).to_bytes(2, 'big') + key.body + b'\xb4' + len(uid).to_bytes(4, 'big') + uid
    extra = (
        subpacket(SUB_KEY_FLAGS, b'\x03')
        + subpacket(SUB_PREF_HASH, bytes([HASH_SHA512, HASH_SHA256]))
        + subpacket(SUB_PRIMARY_UID, b'\x01')
    )
    cert = make_signature(private, key, SIG_POSITIVE_CERT, prefix, created=when, hashed_extra=extra)
    data = encode_packet(TAG_PUBLIC_KEY, key.body) + encode_packet(TAG_USER_ID, uid) + cert
    return armor('PUBLIC KEY BLOCK', data)
