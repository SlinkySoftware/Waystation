// SPDX-License-Identifier: GPL-3.0-or-later
// What a docker referrer is, from its artifact type (T-U05): cosign/notation signatures, SBOMs (SPDX, CycloneDX,
// Syft), in-toto attestations (provenance, vulnerability scans), anything else.

export type ReferrerKind = 'signature' | 'sbom' | 'attestation' | 'artifact'

export function referrerKind(artifactType: string | null | undefined): ReferrerKind {
  const type = (artifactType ?? '').toLowerCase()
  if (/sig|signature|notary|notation/.test(type)) return 'signature'
  if (/spdx|cyclonedx|syft|sbom/.test(type)) return 'sbom'
  if (/in-toto|intoto|attestation|provenance|slsa|vuln/.test(type)) return 'attestation'
  return 'artifact'
}
