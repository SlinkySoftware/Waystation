// SPDX-License-Identifier: GPL-3.0-or-later
// Terraform snippets for the browse views: a required_providers entry and a module block.

const PUBLIC_REGISTRIES = ['registry.terraform.io', 'registry.opentofu.org']

/** host[:port] of a base URL ('' when it is not one). */
export function hostOf(baseUrl: string): string {
  try {
    return new URL(baseUrl).host
  } catch {
    return ''
  }
}

/** A required_providers entry (the local name is the type; the public registries' host names are left out). */
export function requiredProviders(address: string, version: string): string {
  const parts = address.split('/')
  const source = parts.length === 3 && PUBLIC_REGISTRIES.includes(parts[0] ?? '') ? parts.slice(1).join('/') : address
  return `${parts[parts.length - 1]} = { source = "${source}", version = "${version}" }`
}

/** A module block (the local name is the module's name: <namespace>/<name>/<system>, maybe after a host name). */
export function moduleBlock(address: string, version: string): string {
  const parts = address.split('/')
  const name = (parts[parts.length - 2] ?? 'module').replace(/[^A-Za-z0-9_-]/g, '_')
  return `module "${name}" {\n  source  = "${address}"\n  version = "${version}"\n}`
}
