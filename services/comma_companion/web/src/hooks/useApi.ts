import { useCallback, useEffect, useRef, useState } from 'react'

export interface ApiState<T> {
  data?: T
  error?: Error
  loading: boolean
  failureCount: number
  refresh: () => Promise<void>
}

export function useApi<T>(loader: () => Promise<T>, dependencies: readonly unknown[] = []): ApiState<T> {
  const loaderRef = useRef(loader)
  loaderRef.current = loader
  const [data, setData] = useState<T>()
  const [error, setError] = useState<Error>()
  const [loading, setLoading] = useState(true)
  const [failureCount, setFailureCount] = useState(0)
  const generation = useRef(0)
  const mounted = useRef(true)
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
      setLoading(true)
      try {
        const loaded = await Promise.resolve().then(() => loaderRef.current())
        if (mounted.current && generation.current === requestGeneration) {
          setData(loaded)
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
