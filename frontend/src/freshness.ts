export function interruptExpired(interrupt: { expires_at?: string; expired?: boolean }, now = Date.now()): boolean {
  const end = Date.parse(interrupt.expires_at || '')
  return interrupt.expired === true || !Number.isFinite(end) || end <= now
}
