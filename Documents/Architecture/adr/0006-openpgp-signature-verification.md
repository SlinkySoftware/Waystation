# ADR 0006: OpenPGP signature verification in-house on `cryptography`

Status: accepted (2026-11-10, approved by James).

## Context
The Terraform/OpenTofu provider mirror (T-P5-TFP) must verify each provider version's `SHA256SUMS` against its
detached OpenPGP signature and the public keys the origin registry publishes (`signing_keys.gpg_public_keys`). A
network mirror is trusted by the clients: they check archives against the mirror's hashes and never see the
signature, so Waystation's check is the only one. Other formats will need the same primitive (apt `InRelease`/
`Release.gpg` and yum `repomd.xml.asc` verification on proxies). ARCHITECTURE §2 only covers OpenPGP *signing*
(`waystation_common.signing`, chosen in T-P5-APTH).

Options examined (on Python 3.14, against the real keys and signatures recorded for the client harness):

- **PGPy**: pure Python, but unmaintained and does not import on Python 3.13+ (`imghdr` was removed).
- **pysequoia** (Sequoia bindings, wheels): imports, verifies OpenTofu's signatures, but **rejects HashiCorp's**:
  Sequoia requires the signing subkey to have been bound *at the signature's creation time*, and HashiCorp re-issued
  its subkey binding signatures in 2026 (extending their expiry) without keeping the older ones. GnuPG and the
  Terraform client (ProtonMail go-crypto) accept these signatures. A mirror that refuses every registry.terraform.io
  provider is not usable.
- **gpgv in the server image** (subprocess, temporary keyring): vetted and accepts the signatures, but adds a binary
  and subprocess management to the server image and to the shared dev image every test run uses, plus temp-file
  handling per verification.
- **In-house verifier on `cryptography`** (already a dependency): parse armor and the packets needed (v4 public keys
  and subkeys, user IDs, signatures and their subpackets), apply the same policy as go-crypto, and leave every
  cryptographic operation (RSA PKCS#1 v1.5, ECDSA, Ed25519/Ed448, hashing) to `cryptography`.

## Decision (proposed)
- `waystation_common.openpgp`: `read_certs(data)` and `verify_detached(data, signature, certs)`. Policy: version 4
  keys and signatures only; RSA ≥ 2048 bits, ECDSA P-256/384/521, EdDSA (legacy Ed25519 and RFC 9580 Ed25519/Ed448);
  a primary key counts only with a valid self-signature, a subkey only with a valid binding signature by its primary;
  the newest valid self-signature decides key flags and expiry; revoked (by itself) or expired keys, keys without the
  signing flag, expired signatures, unknown critical subpackets, and MD5/SHA-1 data signatures are refused (SHA-1 is
  accepted in self-signatures of old keys, as GnuPG and go-crypto do). No web of trust: callers pass the keys they
  trust. Inputs are bounded (4 MiB, 10 000 packets); malformed data raises `OpenPgpError`. Blocking: callers run it
  in a thread.
- Tested with keys and signatures generated per test run for every algorithm and policy branch
  (`Application/Server/tests/support/ws_openpgp.py`, no key material committed) and with the real HashiCorp and
  OpenTofu signatures recorded for the client harness.
- ARCHITECTURE §2 would gain: "OpenPGP signature verification (provider `SHA256SUMS`, later apt/yum proxy metadata)
  is in-house in `waystation_common.openpgp` on `cryptography` (adr/0006)."

## Consequences
- No new dependency, no subprocess, no image change; the verifier is about 450 lines of parsing that Waystation
  maintains (the risk sits in parsing untrusted input, which is bounded and fuzz-friendly; the cryptography is not
  ours).
- Unsupported: version 6 keys/signatures (RFC 9580 "crypto refresh"), DSA, ElGamal and brainpool curves. A provider
  signed that way fails verification (502) until support is added; `verify_signatures: false` on the repository is
  the documented escape hatch.
- If not approved: replace the module's two functions with a `gpgv` call (temporary keyring from the armored keys,
  `--status-fd` and a `VALIDSIG` check) in the server image; the callers and tests of the mirror stay the same.
