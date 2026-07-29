import { describe, expect, it } from 'vitest'
import {
  clamp,
  formatBitrate,
  formatBytes,
  formatDurationUs,
  formatLocalDate,
  mergeQuery,
  metricImprovement,
  percent,
} from './utils'

describe('time and size formatting', () => {
  it('keeps the local drive timestamp in ISO-style 24-hour form', () => {
    expect(formatLocalDate('2026-07-28T20:15:00Z')).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/)
  })

  it('formats route-relative microseconds without using wall time', () => {
    expect(formatDurationUs(65_400_000)).toBe('1:05')
    expect(formatDurationUs(3_665_400_000)).toBe('1:01:05')
    expect(formatDurationUs(65_450_000, true)).toBe('1:05.5')
  })

  it('formats transport and archive units independently', () => {
    expect(formatBytes(1024 ** 3)).toBe('1.0 GB')
    expect(formatBitrate(1_000_000)).toBe('8.0 Mbit/s')
  })
})

describe('timeline helpers', () => {
  it('clamps playhead and progress values', () => {
    expect(clamp(12, 0, 10)).toBe(10)
    expect(percent(3, 4)).toBe(75)
    expect(percent(100, 0)).toBe(0)
  })

  it('preserves unrelated deep-link parameters when moving the playhead', () => {
    expect(mergeQuery('?camera=road&mode=tune', { t: 123_000, mode: null })).toBe('?camera=road&t=123000')
  })

  it('compares signed bias by magnitude rather than signed direction', () => {
    expect(metricImprovement(-0.09, -0.03, 'absolute_lower')).toBeCloseTo(66.67, 1)
    expect(metricImprovement(-0.03, -0.09, 'absolute_lower')).toBeCloseTo(-200)
    expect(metricImprovement(0.18, 0.12, 'lower')).toBeCloseTo(33.33, 1)
  })
})
