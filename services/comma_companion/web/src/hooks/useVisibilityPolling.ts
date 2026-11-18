import { useEffect } from 'react'

export function useVisibilityPolling(
  refresh: () => Promise<void>,
  failureCount: number,
  intervalMs = 5_000,
  maximumBackoffMs = 60_000,
  enabled = true,
): void {
  useEffect(() => {
    if (!enabled) return
    let disposed = false
    let timer: number | undefined
    const delay = Math.min(
      maximumBackoffMs,
      intervalMs * (2 ** Math.min(failureCount, 6)),
    )

    const clear = () => {
      if (timer != null) window.clearTimeout(timer)
      timer = undefined
    }
    const schedule = (waitMs = delay) => {
      clear()
      if (disposed || document.visibilityState !== 'visible') return
      timer = window.setTimeout(() => {
        timer = undefined
        const startedAt = Date.now()
        void refresh().finally(() => {
          if (!disposed) {
            schedule(Math.max(0, delay - (Date.now() - startedAt)))
          }
        })
      }, waitMs)
    }
    const visibilityChanged = () => {
      if (document.visibilityState === 'visible') schedule(0)
      else clear()
    }

    document.addEventListener('visibilitychange', visibilityChanged)
    schedule()
    return () => {
      disposed = true
      clear()
      document.removeEventListener('visibilitychange', visibilityChanged)
    }
  }, [enabled, failureCount, intervalMs, maximumBackoffMs, refresh])
}
