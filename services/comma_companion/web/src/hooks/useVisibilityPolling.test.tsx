import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useVisibilityPolling } from './useVisibilityPolling'

function deferred() {
  let resolve!: () => void
  const promise = new Promise<void>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

afterEach(() => {
  vi.useRealTimers()
})

describe('useVisibilityPolling cadence', () => {
  it('subtracts request time from the next interval', async () => {
    vi.useFakeTimers()
    const first = deferred()
    const refresh = vi.fn()
      .mockReturnValueOnce(first.promise)
      .mockResolvedValue(undefined)

    renderHook(() => useVisibilityPolling(refresh, 0, 1_000))

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000)
    })
    expect(refresh).toHaveBeenCalledTimes(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(400)
      first.resolve()
      await Promise.resolve()
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(599)
    })
    expect(refresh).toHaveBeenCalledTimes(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1)
    })
    expect(refresh).toHaveBeenCalledTimes(2)
  })
})
