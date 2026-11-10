// SPDX-License-Identifier: GPL-3.0-or-later
// Objects declared in the provisioning file (T-O09) carry `managed_by: 'provisioning'`: the UI shows them read-only
// with a hint (the server still decides; an API edit is overwritten by the next apply).

export interface Managed {
  managed_by?: 'provisioning' | null
}

export function isManaged(item: Managed | null | undefined): boolean {
  return item?.managed_by === 'provisioning'
}
