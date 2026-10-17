/** Pure helpers for the /context screen. */

import type { EvidencePacket } from '@/api/hooks/useContextApi'

export const CONTEXT_TABS = ['documents', 'summaries'] as const
export type ContextTab = (typeof CONTEXT_TABS)[number]

export function isContextTab(value: string | null): value is ContextTab {
  return value !== null && (CONTEXT_TABS as readonly string[]).includes(value)
}

export { formatBytes } from '@/utils/formatBytes'

export interface EvidenceGroup {
  source: string
  packets: EvidencePacket[]
}

/** Group packets by source (alphabetical), preserving in-group server order. */
export function groupEvidence(packets: EvidencePacket[]): EvidenceGroup[] {
  const groups = new Map<string, EvidencePacket[]>()
  for (const packet of packets) {
    const existing = groups.get(packet.source)
    if (existing) existing.push(packet)
    else groups.set(packet.source, [packet])
  }
  return [...groups.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([source, grouped]) => ({ source, packets: grouped }))
}
