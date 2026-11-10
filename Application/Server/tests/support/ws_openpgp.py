"""Build OpenPGP keys and signatures in tests (RFC 9580 version 4 packets, signed with ``cryptography``).

Keys are generated per test run: no key material is stored in the repository. ``PgpKey`` makes a key of a given
algorithm; ``cert`` assembles a transferable public key (primary, user ID with a self-signature, subkeys with
binding signatures, optional revocations); ``detached`` signs data. Every knob a verifier must check (hash
algorithm, subpackets, expiry, critical bits, wrong issuer) can be set.
"""

from __future__ import annotations

import base64
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed, decode_dss_signature

HASHES: dict[int, type[hashes.HashAlgorithm]] = {
    2: hashes.SHA1,
    8: hashes.SHA256,
    9: hashes.SHA384,
    10: hashes.SHA512,
    11: hashes.SHA224,
    12: hashes.SHA3_256,
    14: hashes.SHA3_512,
}
P256_OID = bytes.fromhex('2a8648ce3d030107')
ED25519_OID = bytes.fromhex('2b06010401da470f01')
NOW = int(time.time())


def mpi(value: int | bytes) -> bytes:
    number = int.from_bytes(value, 'big') if isinstance(value, bytes) else value
    raw = number.to_bytes((number.bit_length() + 7) // 8, 'big') if number else b''
    return number.bit_length().to_bytes(2, 'big') + raw


def packet(tag: int, body: bytes, *, old: bool = False) -> bytes:
    if old:
        return bytes([0x80 | (tag << 2) | 2]) + len(body).to_bytes(4, 'big') + body
    if len(body) < 192:
        head = bytes([len(body)])
    elif len(body) < 8384:
        n = len(body) - 192
        head = bytes([(n >> 8) + 192, n & 0xFF])
    else:
        head = b'\xff' + len(body).to_bytes(4, 'big')
    return bytes([0xC0 | tag]) + head + body


def subpacket(kind: int, data: bytes, *, critical: bool = False) -> bytes:
    body = bytes([kind | (0x80 if critical else 0)]) + data
    if len(body) < 192:
        return bytes([len(body)]) + body
    n = len(body) - 192
    return bytes([(n >> 8) + 192, n & 0xFF]) + body


@dataclass
class PgpKey:
    algorithm: str = 'ed25519'  # ed25519 (legacy EdDSA, 22), ed25519v6 (27), ed448 (28), rsa (1), p256 (19), dsa (17)
    created: int = NOW - 86_400
    bits: int = 2048
    signer: Any = field(init=False, repr=False)
    body: bytes = field(init=False)

    def __post_init__(self) -> None:
        head = b'\x04' + self.created.to_bytes(4, 'big')
        match self.algorithm:
            case 'ed25519':
                self.signer = ed25519.Ed25519PrivateKey.generate()
                raw = self.signer.public_key().public_bytes_raw()
                self.body = head + bytes([22, len(ED25519_OID)]) + ED25519_OID + mpi(b'\x40' + raw)
            case 'ed25519v6':
                self.signer = ed25519.Ed25519PrivateKey.generate()
                self.body = head + bytes([27]) + self.signer.public_key().public_bytes_raw()
            case 'ed448':
                self.signer = ed448.Ed448PrivateKey.generate()
                self.body = head + bytes([28]) + self.signer.public_key().public_bytes_raw()
            case 'rsa':
                self.signer = rsa.generate_private_key(public_exponent=65537, key_size=self.bits)
                numbers = self.signer.public_key().public_numbers()
                self.body = head + bytes([1]) + mpi(numbers.n) + mpi(numbers.e)
            case 'p256':
                self.signer = ec.generate_private_key(ec.SECP256R1())
                point = self.signer.public_key().public_bytes(
                    encoding=serialization.Encoding.X962, format=serialization.PublicFormat.UncompressedPoint
                )
                self.body = head + bytes([19, len(P256_OID)]) + P256_OID + mpi(point)
            case 'dsa':
                self.signer = None
                self.body = head + bytes([17]) + mpi(23) + mpi(11) + mpi(5) + mpi(7)
            case _:
                raise ValueError(self.algorithm)

    @property
    def algo_id(self) -> int:
        return {'ed25519': 22, 'ed25519v6': 27, 'ed448': 28, 'rsa': 1, 'p256': 19, 'dsa': 17}[self.algorithm]

    @property
    def fingerprint(self) -> bytes:
        return hashlib.sha1(self.prefix(), usedforsecurity=False).digest()

    @property
    def key_id(self) -> bytes:
        return self.fingerprint[-8:]

    def prefix(self) -> bytes:
        return b'\x99' + len(self.body).to_bytes(2, 'big') + self.body

    def sign_digest(self, digest: bytes, hash_id: int) -> bytes:
        """The algorithm-specific signature fields over ``digest``."""
        match self.algorithm:
            case 'ed25519':
                raw = self.signer.sign(digest)
                return mpi(raw[:32]) + mpi(raw[32:])
            case 'ed25519v6' | 'ed448':
                return bytes(self.signer.sign(digest))
            case 'rsa':
                return mpi(self.signer.sign(digest, padding.PKCS1v15(), Prehashed(HASHES[hash_id]())))
            case 'p256':
                r, s = decode_dss_signature(self.signer.sign(digest, ec.ECDSA(Prehashed(HASHES[hash_id]()))))
                return mpi(r) + mpi(s)
        return mpi(1) + mpi(1)  # dsa: never valid


def signature(
    key: PgpKey,
    signed: bytes,
    sig_type: int,
    *,
    hash_id: int = 8,
    created: int | None = None,
    hashed: list[bytes] | None = None,
    unhashed: list[bytes] | None = None,
    issuer: bool = True,
    version: int = 4,
    tamper: bool = False,
) -> bytes:
    """A signature packet body by ``key`` over ``signed`` (the material before the trailer)."""
    areas = [subpacket(2, (NOW - 60 if created is None else created).to_bytes(4, 'big'))] if created != -1 else []
    if issuer:
        areas.append(subpacket(33, b'\x04' + key.fingerprint))
    hashed_data = b''.join(areas + (hashed or []))
    head = bytes([version, sig_type, key.algo_id, hash_id]) + len(hashed_data).to_bytes(2, 'big') + hashed_data
    h = hashes.Hash(HASHES[hash_id]())
    h.update(signed + (b'tampered' if tamper else b''))
    h.update(head)
    h.update(b'\x04\xff' + len(head).to_bytes(4, 'big'))
    digest = h.finalize()
    unhashed_data = b''.join(([subpacket(16, key.key_id)] if issuer else []) + (unhashed or []))
    return head + len(unhashed_data).to_bytes(2, 'big') + unhashed_data + digest[:2] + key.sign_digest(digest, hash_id)


def uid_signed(key: PgpKey, uid: bytes) -> bytes:
    return key.prefix() + b'\xb4' + len(uid).to_bytes(4, 'big') + uid


@dataclass
class Sub:
    key: PgpKey
    flags: int | None = 0x02
    expires_after: int | None = None
    binding: bool = True  # False: no binding signature
    bad_binding: bool = False
    revoked: bool = False
    hash_id: int = 8


def cert(
    primary: PgpKey,
    *,
    uid: bytes = b'Test Signer <signer@example.com>',
    flags: int | None = 0x03,
    expires_after: int | None = None,
    subkeys: tuple[Sub, ...] = (),
    self_signed: bool = True,
    direct: bool = False,
    revoked: bool = False,
    hash_id: int = 8,
) -> bytes:
    """A binary transferable public key."""
    out = packet(6, primary.body)
    extra: list[bytes] = []
    if flags is not None:
        extra.append(subpacket(27, bytes([flags])))
    if expires_after is not None:
        extra.append(subpacket(9, expires_after.to_bytes(4, 'big')))
    if revoked:
        out += packet(2, signature(primary, primary.prefix(), 0x20, hash_id=hash_id))
    if direct:
        out += packet(2, signature(primary, primary.prefix(), 0x1F, hashed=extra, hash_id=hash_id))
    out += packet(13, uid)
    if self_signed:
        out += packet(2, signature(primary, uid_signed(primary, uid), 0x13, hashed=extra, hash_id=hash_id))
    for sub in subkeys:
        out += packet(14, sub.key.body)
        signed = primary.prefix() + sub.key.prefix()
        if sub.binding:
            sub_extra = [subpacket(27, bytes([sub.flags]))] if sub.flags is not None else []
            if sub.expires_after is not None:
                sub_extra.append(subpacket(9, sub.expires_after.to_bytes(4, 'big')))
            out += packet(
                2, signature(primary, signed, 0x18, hashed=sub_extra, hash_id=sub.hash_id, tamper=sub.bad_binding)
            )
        if sub.revoked:
            out += packet(2, signature(primary, signed, 0x28))
    return out


def armor(data: bytes, kind: str = 'PUBLIC KEY BLOCK', *, checksum: bool = True) -> bytes:
    encoded = base64.b64encode(data).decode()
    lines = [f'-----BEGIN PGP {kind}-----', 'Comment: test', '']
    lines += [encoded[i : i + 64] for i in range(0, len(encoded), 64)]
    if checksum:
        crc = 0xB704CE
        for byte in data:
            crc ^= byte << 16
            for _ in range(8):
                crc <<= 1
                if crc & 0x1000000:
                    crc ^= 0x1864CFB
        lines.append('=' + base64.b64encode((crc & 0xFFFFFF).to_bytes(3, 'big')).decode())
    lines.append(f'-----END PGP {kind}-----')
    return ('\n'.join(lines) + '\n').encode()


def detached(key: PgpKey, data: bytes, *, sig_type: int = 0x00, **kwargs: Any) -> bytes:
    """A binary detached signature packet over ``data``."""
    signed = data.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n') if sig_type == 0x01 else data
    return packet(2, signature(key, signed, sig_type, **kwargs))
