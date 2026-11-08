// SPDX-License-Identifier: GPL-3.0-or-later
// Generated with assets/formats.svg (scripts/gen-format-sprite.mjs): titles and brand colours of the sprite symbols.

export const brandMarks = {
  docker: { title: 'Docker', hex: '#2496ED' },
  npm: { title: 'npm', hex: '#CB3837' },
  debian: { title: 'Debian', hex: '#A81D33' },
  ubuntu: { title: 'Ubuntu', hex: '#E95420' },
  raspberrypi: { title: 'Raspberry Pi', hex: '#A22846' },
  redhat: { title: 'Red Hat', hex: '#EE0000' },
  rockylinux: { title: 'Rocky Linux', hex: '#10B981' },
  almalinux: { title: 'AlmaLinux', hex: '#000000' },
  fedora: { title: 'Fedora', hex: '#51A2DA' },
  pypi: { title: 'PyPI', hex: '#3775A9' },
  maven: { title: 'Apache Maven', hex: '#C71A36' },
  go: { title: 'Go', hex: '#00ADD8' },
  helm: { title: 'Helm', hex: '#0F1689' },
  terraform: { title: 'Terraform', hex: '#844FBA' },
  opentofu: { title: 'OpenTofu', hex: '#FFDA18' },
  ansible: { title: 'Ansible', hex: '#EE0000' },
  swift: { title: 'Swift', hex: '#F05138' },
  nuget: { title: 'NuGet', hex: '#004880' },
  cargo: { title: 'Rust', hex: '#000000' },
  rubygems: { title: 'RubyGems', hex: '#E9573F' },
  composer: { title: 'Composer', hex: '#885630' },
  conda: { title: 'Anaconda', hex: '#44A833' },
  alpine: { title: 'Alpine Linux', hex: '#0D597F' },
  huggingface: { title: 'Hugging Face', hex: '#FFD21E' },
  raw: { title: 'Raw', hex: '#0f766e' },
} as const

export type BrandMark = keyof typeof brandMarks
