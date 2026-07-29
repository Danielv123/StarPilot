import { act, renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { useApi } from './useApi'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

describe('useApi dependency generations', () => {
  it('starts the new loader immediately and ignores a delayed stale response', async () => {
    const oldRequest = deferred<string>()
    const newRequest = deferred<string>()
    const oldLoader = vi.fn(() => oldRequest.promise)
    const newLoader = vi.fn(() => newRequest.promise)

    const state = renderHook(
      ({ generation }) => useApi(
        generation === 'old' ? oldLoader : newLoader,
        [generation],
      ),
      { initialProps: { generation: 'old' } },
    )

    await waitFor(() => expect(oldLoader).toHaveBeenCalledTimes(1))
    state.rerender({ generation: 'new' })
    await waitFor(() => expect(newLoader).toHaveBeenCalledTimes(1))

    oldRequest.resolve('stale')
    await Promise.resolve()
    expect(state.result.current.data).not.toBe('stale')

    newRequest.resolve('current')
    await waitFor(() => expect(state.result.current.data).toBe('current'))
    expect(state.result.current.loading).toBe(false)
  })

  it('keeps rendered data out of the loading state during a background refresh', async () => {
    const nextRequest = deferred<string>()
    const loader = vi.fn()
      .mockResolvedValueOnce('initial')
      .mockReturnValueOnce(nextRequest.promise)
    const state = renderHook(() => useApi(loader, []))

    await waitFor(() => expect(state.result.current.data).toBe('initial'))
    expect(state.result.current.loading).toBe(false)

    let refresh!: Promise<void>
    act(() => {
      refresh = state.result.current.refresh()
    })
    expect(state.result.current.loading).toBe(false)

    nextRequest.resolve('updated')
    await act(async () => refresh)
    expect(state.result.current.data).toBe('updated')
    expect(state.result.current.loading).toBe(false)
  })

  it('preserves the current value when a background response has equal content', async () => {
    const initial = { version: 1, items: ['ready'] }
    const loader = vi.fn()
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce({ version: 1, items: ['ready'] })
    const state = renderHook(() => useApi(
      loader,
      [],
      (current, next) => JSON.stringify(current) === JSON.stringify(next),
    ))

    await waitFor(() => expect(state.result.current.data).toBe(initial))
    await act(async () => state.result.current.refresh())

    expect(state.result.current.data).toBe(initial)
    expect(loader).toHaveBeenCalledTimes(2)
  })
})
