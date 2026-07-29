import { useCallback, useEffect, useRef, useState } from 'react'

export interface ApiState<T> {
  data?: T
  error?: Error
  loading: boolean
  failureCount: number
  refresh: () => Promise<void>
}

export function useApi<T>(
  loader: () => Promise<T>,
  dependencies: readonly unknown[] = [],
  isEqual?: (current: T, next: T) => boolean,
): ApiState<T> {
  const loaderRef = useRef(loader)
  loaderRef.current = loader
  const isEqualRef = useRef(isEqual)
  isEqualRef.current = isEqual
  const [data, setData] = useState<T>()
  const dataRef = useRef<T | undefined>(undefined)
  const [error, setError] = useState<Error>()
  const [loading, setLoading] = useState(true)
  const [failureCount, setFailureCount] = useState(0)
  const generation = useRef(0)
  const mounted = useRef(true)
  const hasData = useRef(false)
  const inFlight = useRef<{
    generation: number
    promise: Promise<void>
  } | undefined>(undefined)

  const refresh = useCallback(() => {
    const requestGeneration = generation.current
    if (inFlight.current?.generation === requestGeneration) {
      return inFlight.current.promise
    }
    const pending = (async () => {
      if (!hasData.current) setLoading(true)
      try {
        const loaded = await Promise.resolve().then(() => loaderRef.current())
        if (mounted.current && generation.current === requestGeneration) {
          const unchanged =
            hasData.current &&
            dataRef.current !== undefined &&
            Boolean(isEqualRef.current?.(dataRef.current, loaded))
          hasData.current = true
          if (!unchanged) {
            dataRef.current = loaded
            setData(loaded)
          }
          setError(undefined)
          setFailureCount(0)
        }
      } catch (caught) {
        if (mounted.current && generation.current === requestGeneration) {
          setError(caught instanceof Error ? caught : new Error('Request failed'))
          setFailureCount((count) => count + 1)
        }
      } finally {
        if (mounted.current && generation.current === requestGeneration) {
          setLoading(false)
        }
        if (inFlight.current?.generation === requestGeneration) {
          inFlight.current = undefined
        }
      }
    })()
    inFlight.current = { generation: requestGeneration, promise: pending }
    return pending
  }, dependencies)

  useEffect(() => {
    generation.current += 1
    void refresh()
  }, [refresh])

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      generation.current += 1
      inFlight.current = undefined
    }
  }, [])

  return { data, error, loading, failureCount, refresh }
}
