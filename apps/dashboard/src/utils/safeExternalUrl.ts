/**
 * Accept only absolute HTTP(S) links before reflecting provider-owned URLs into
 * an anchor. Tracker payloads are external data and may contain javascript:,
 * data:, credentials, or other navigation schemes a browser must never run.
 */
export function safeExternalHttpUrl(value: string | null | undefined): string | null {
  if (!value || value !== value.trim() || value.length > 4_096) return null
  try {
    const parsed = new URL(value)
    if (
      (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') ||
      parsed.username !== '' ||
      parsed.password !== '' ||
      parsed.hostname === ''
    ) {
      return null
    }
    return parsed.href
  } catch {
    return null
  }
}
