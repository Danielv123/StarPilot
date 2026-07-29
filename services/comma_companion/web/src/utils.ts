export function formatLocalDate(value?: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.valueOf())) return '—'
  const parts = new Intl.DateTimeFormat(undefined, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(date)
  const part = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((item) => item.type === type)?.value ?? ''
  return `${part('year')}-${part('month')}-${part('day')} ${part('hour')}:${part('minute')}`
}

export function formatRelativeTime(value?: string | null, now = Date.now()): string {
  if (!value) return 'never'
  const then = new Date(value).valueOf()
  if (Number.isNaN(then)) return 'unknown'
  const seconds = Math.round((then - now) / 1000)
  const abs = Math.abs(seconds)
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })
  if (abs < 60) return formatter.format(seconds, 'second')
  if (abs < 3600) return formatter.format(Math.round(seconds / 60), 'minute')
  if (abs < 86400) return formatter.format(Math.round(seconds / 3600), 'hour')
  return formatter.format(Math.round(seconds / 86400), 'day')
}

export function formatBytes(value?: number | null, digits = 1): string {
  if (value == null || !Number.isFinite(value)) return '—'
  if (value === 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  const index = Math.min(Math.floor(Math.log(Math.abs(value)) / Math.log(1024)), units.length - 1)
  return `${(value / 1024 ** index).toFixed(index === 0 ? 0 : digits)} ${units[index]}`
}

export function formatBitrate(bytesPerSecond?: number | null): string {
  if (bytesPerSecond == null || !Number.isFinite(bytesPerSecond)) return '—'
  const bits = bytesPerSecond * 8
  if (bits >= 1_000_000_000) return `${(bits / 1_000_000_000).toFixed(1)} Gbit/s`
  if (bits >= 1_000_000) return `${(bits / 1_000_000).toFixed(1)} Mbit/s`
  if (bits >= 1_000) return `${(bits / 1_000).toFixed(1)} kbit/s`
  return `${Math.round(bits)} bit/s`
}

export function formatDurationUs(value?: number | null, precise = false): string {
  if (value == null || !Number.isFinite(value)) return '—'
  const totalSeconds = Math.max(0, value / 1_000_000)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  const secondsText = precise ? seconds.toFixed(1).padStart(4, '0') : Math.floor(seconds).toString().padStart(2, '0')
  if (hours > 0) return `${hours}:${minutes.toString().padStart(2, '0')}:${secondsText}`
  return `${minutes}:${secondsText}`
}

export function formatDistance(metres?: number | null): string {
  if (metres == null || !Number.isFinite(metres)) return '—'
  return `${(metres / 1000).toFixed(1)} km`
}

export function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value))
}

export function percent(numerator?: number | null, denominator?: number | null): number {
  if (!denominator || numerator == null) return 0
  return clamp((numerator / denominator) * 100, 0, 100)
}

export function metricImprovement(
  baseline: number,
  candidate: number,
  objective: 'lower' | 'absolute_lower' | 'higher',
): number {
  const baselineScore = objective === 'absolute_lower' ? Math.abs(baseline) : baseline
  const candidateScore = objective === 'absolute_lower' ? Math.abs(candidate) : candidate
  const scale = Math.abs(baselineScore)
  if (scale < Number.EPSILON) {
    if (Math.abs(candidateScore - baselineScore) < Number.EPSILON) return 0
    const better = objective === 'higher'
      ? candidateScore > baselineScore
      : candidateScore < baselineScore
    return better ? 100 : -100
  }
  return objective === 'higher'
    ? ((candidateScore - baselineScore) / scale) * 100
    : ((baselineScore - candidateScore) / scale) * 100
}

export function mergeQuery(search: string, updates: Record<string, string | number | null | undefined>): string {
  const params = new URLSearchParams(search)
  Object.entries(updates).forEach(([key, value]) => {
    if (value == null || value === '') params.delete(key)
    else params.set(key, String(value))
  })
  const output = params.toString()
  return output ? `?${output}` : ''
}

export function getCsrfToken(): string | undefined {
  return document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content
}
