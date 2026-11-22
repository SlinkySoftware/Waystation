// SPDX-License-Identifier: GPL-3.0-or-later
import { describe, expect, it } from 'vitest'

import { hostOf, moduleBlock, requiredProviders } from './terraform'

describe('Terraform browse snippets', () => {
  it('writes required_providers entries without the public registry host names', () => {
    expect(requiredProviders('registry.terraform.io/hashicorp/null', '3.2.4')).toBe('null = { source = "hashicorp/null", version = "3.2.4" }')
    expect(requiredProviders('registry.example.com/platform/inventory', '1.2.0')).toBe('inventory = { source = "registry.example.com/platform/inventory", version = "1.2.0" }')
  })

  it('writes module blocks named after the module', () => {
    expect(moduleBlock('terraform-aws-modules/vpc/aws', '6.5.0')).toBe('module "vpc" {\n  source  = "terraform-aws-modules/vpc/aws"\n  version = "6.5.0"\n}')
    expect(moduleBlock('registry.example.com/platform/network/aws', '2.3.0')).toContain('source  = "registry.example.com/platform/network/aws"')
  })

  it('takes the host name of a base URL', () => {
    expect(hostOf('https://registry.example.com')).toBe('registry.example.com')
    expect(hostOf('https://registry.example.com:8443/')).toBe('registry.example.com:8443')
    expect(hostOf('')).toBe('')
  })
})
