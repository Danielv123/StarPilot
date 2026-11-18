import { useMemo, useRef, useState } from 'react'
import type { SignalSeries, TimelineEvent } from '../api/types'
import { clamp, formatDurationUs } from '../utils'

interface ChartSeries extends SignalSeries {
  muted?: boolean
}

function finiteValues(series: ChartSeries[], startUs: number, endUs: number): number[] {
  return series.flatMap((item) =>
    item.points
      .filter((point) => point.t_us >= startUs && point.t_us <= endUs && point.value != null && Number.isFinite(point.value))
      .map((point) => point.value as number),
  )
}

function domain(values: number[]): [number, number] {
  if (!values.length) return [-1, 1]
  let low = Math.min(...values)
  let high = Math.max(...values)
  if (low === high) {
    low -= Math.abs(low || 1) * 0.2
    high += Math.abs(high || 1) * 0.2
  }
  const padding = (high - low) * 0.12
  return [low - padding, high + padding]
}

function buildPath(
  series: ChartSeries,
  startUs: number,
  endUs: number,
  low: number,
  high: number,
  width: number,
  height: number,
  left: number,
  top: number,
): string {
  let drawing = false
  return series.points
    .filter((point) => point.t_us >= startUs && point.t_us <= endUs)
    .map((point) => {
      if (point.value == null || !Number.isFinite(point.value)) {
        drawing = false
        return ''
      }
      const x = left + ((point.t_us - startUs) / Math.max(1, endUs - startUs)) * width
      const y = top + (1 - ((point.value as number) - low) / Math.max(0.000001, high - low)) * height
      const command = drawing ? 'L' : 'M'
      drawing = true
      return `${command}${x.toFixed(2)},${y.toFixed(2)}`
    })
    .join(' ')
}

function closestValue(series: ChartSeries, tUs: number): number | null {
  let best: { distance: number; value: number | null } | undefined
  for (const point of series.points) {
    const distance = Math.abs(point.t_us - tUs)
    if (!best || distance < best.distance) best = { distance, value: point.value }
  }
  return best?.value ?? null
}

export function SignalChart({
  title,
  series,
  startUs,
  endUs,
  playheadUs,
  onSeek,
  height = 170,
  compact = false,
}: {
  title?: string
  series: ChartSeries[]
  startUs: number
  endUs: number
  playheadUs: number
  onSeek?: (tUs: number) => void
  height?: number
  compact?: boolean
}) {
  const [hoverUs, setHoverUs] = useState<number>()
  const svgRef = useRef<SVGSVGElement>(null)
  const chartWidth = 900
  const left = compact ? 14 : 52
  const right = 16
  const top = 14
  const bottom = compact ? 12 : 25
  const plotWidth = chartWidth - left - right
  const plotHeight = height - top - bottom
  const [low, high] = useMemo(() => domain(finiteValues(series, startUs, endUs)), [series, startUs, endUs])
  const cursorUs = hoverUs ?? playheadUs
  const cursorX = left + ((clamp(cursorUs, startUs, endUs) - startUs) / Math.max(1, endUs - startUs)) * plotWidth

  const pointFromEvent = (clientX: number) => {
    const bounds = svgRef.current?.getBoundingClientRect()
    if (!bounds) return
    const localX = ((clientX - bounds.left) / bounds.width) * chartWidth
    return Math.round(startUs + clamp((localX - left) / plotWidth, 0, 1) * (endUs - startUs))
  }

  return (
    <div className={`signal-chart ${compact ? 'signal-chart-compact' : ''}`}>
      {(title || !compact) && (
        <div className="chart-header">
          {title && <strong>{title}</strong>}
          <div className="chart-legend">
            {series.map((item) => (
              <span key={item.id} className={item.muted ? 'legend-muted' : ''}>
                <i style={{ backgroundColor: item.color ?? '#88f0c5' }} />
                {item.label}
                <em>
                  {closestValue(item, cursorUs)?.toFixed(2) ?? '—'} {item.unit}
                </em>
              </span>
            ))}
          </div>
        </div>
      )}
      <svg
        ref={svgRef}
        className="chart-svg"
        viewBox={`0 0 ${chartWidth} ${height}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={`${title ?? 'Telemetry'} chart. Click or drag to seek.`}
        tabIndex={onSeek ? 0 : undefined}
        onPointerMove={(event) => {
          const value = pointFromEvent(event.clientX)
          if (value != null) {
            setHoverUs(value)
            if (event.buttons === 1) onSeek?.(value)
          }
        }}
        onPointerDown={(event) => {
          event.currentTarget.setPointerCapture(event.pointerId)
          const value = pointFromEvent(event.clientX)
          if (value != null) onSeek?.(value)
        }}
        onPointerLeave={() => setHoverUs(undefined)}
        onKeyDown={(event) => {
          if (!onSeek) return
          if (event.key === 'ArrowLeft') onSeek(clamp(playheadUs - 100_000, startUs, endUs))
          if (event.key === 'ArrowRight') onSeek(clamp(playheadUs + 100_000, startUs, endUs))
        }}
      >
        <g className="chart-grid">
          {Array.from({ length: 5 }, (_, index) => {
            const y = top + (plotHeight / 4) * index
            return <line key={`y-${index}`} x1={left} y1={y} x2={left + plotWidth} y2={y} />
          })}
          {Array.from({ length: 7 }, (_, index) => {
            const x = left + (plotWidth / 6) * index
            return <line key={`x-${index}`} x1={x} y1={top} x2={x} y2={top + plotHeight} />
          })}
        </g>
        {!compact && (
          <g className="chart-axis">
            <text x={left - 7} y={top + 4} textAnchor="end">{high.toFixed(1)}</text>
            <text x={left - 7} y={top + plotHeight / 2 + 4} textAnchor="end">{((high + low) / 2).toFixed(1)}</text>
            <text x={left - 7} y={top + plotHeight + 4} textAnchor="end">{low.toFixed(1)}</text>
            <text x={left} y={height - 4}>{formatDurationUs(startUs)}</text>
            <text x={left + plotWidth} y={height - 4} textAnchor="end">{formatDurationUs(endUs)}</text>
          </g>
        )}
        {series.map((item) => (
          <path
            key={item.id}
            d={buildPath(item, startUs, endUs, low, high, plotWidth, plotHeight, left, top)}
            className={`chart-line ${item.muted ? 'chart-line-muted' : ''}`}
            style={{ stroke: item.color ?? '#88f0c5' }}
          />
        ))}
        <line className="chart-playhead" x1={cursorX} x2={cursorX} y1={top} y2={top + plotHeight} />
        {hoverUs != null && (
          <g className="chart-cursor-label">
            <rect x={clamp(cursorX - 31, left, left + plotWidth - 62)} y={top + 4} width={62} height={19} rx={3} />
            <text x={clamp(cursorX, left + 31, left + plotWidth - 31)} y={top + 17} textAnchor="middle">
              {formatDurationUs(cursorUs, true)}
            </text>
          </g>
        )}
      </svg>
    </div>
  )
}

const laneLabels: Record<TimelineEvent['lane'], string> = {
  active: 'ACTIVE',
  driver_overlay: 'DRIVER',
  alert: 'ALERTS',
  gap: 'GAPS',
  saturation: 'LIMIT',
}

const lanes: TimelineEvent['lane'][] = ['active', 'driver_overlay', 'saturation', 'alert', 'gap']

export function EventTimeline({
  events,
  gaps = [],
  startUs,
  endUs,
  playheadUs,
  onSeek,
}: {
  events: TimelineEvent[]
  gaps?: Array<{ start_us: number; end_us: number; reason: string }>
  startUs: number
  endUs: number
  playheadUs: number
  onSeek: (tUs: number) => void
}) {
  const allEvents: TimelineEvent[] = [
    ...events,
    ...gaps.map((gap, index) => ({
      id: `gap-${index}`,
      lane: 'gap' as const,
      start_us: gap.start_us,
      end_us: gap.end_us,
      label: gap.reason,
      severity: 'critical' as const,
    })),
  ]
  const progress = ((clamp(playheadUs, startUs, endUs) - startUs) / Math.max(1, endUs - startUs)) * 100

  return (
    <div
      className="event-timeline"
      role="slider"
      aria-label="Drive event timeline"
      aria-valuemin={startUs}
      aria-valuemax={endUs}
      aria-valuenow={playheadUs}
      tabIndex={0}
      onPointerDown={(event) => {
        const bounds = event.currentTarget.getBoundingClientRect()
        const ratio = clamp((event.clientX - bounds.left - 68) / Math.max(1, bounds.width - 68), 0, 1)
        onSeek(Math.round(startUs + ratio * (endUs - startUs)))
      }}
      onKeyDown={(event) => {
        if (event.key === 'ArrowLeft') onSeek(clamp(playheadUs - 500_000, startUs, endUs))
        if (event.key === 'ArrowRight') onSeek(clamp(playheadUs + 500_000, startUs, endUs))
      }}
    >
      {lanes.map((lane) => (
        <div className="timeline-lane" key={lane}>
          <span>{laneLabels[lane]}</span>
          <div className="timeline-track">
            {allEvents
              .filter((event) => event.lane === lane)
              .map((event) => {
                const left = ((event.start_us - startUs) / Math.max(1, endUs - startUs)) * 100
                const width = Math.max(0.35, ((event.end_us - event.start_us) / Math.max(1, endUs - startUs)) * 100)
                return (
                  <i
                    key={event.id}
                    className={`timeline-event event-${event.lane} severity-${event.severity ?? 'info'}`}
                    style={{ left: `${left}%`, width: `${width}%` }}
                    title={`${event.label} · ${formatDurationUs(event.start_us)}–${formatDurationUs(event.end_us)}`}
                  />
                )
              })}
            <i className="timeline-playhead" style={{ left: `${progress}%` }} />
          </div>
        </div>
      ))}
      <div className="timeline-scale">
        <span />
        <div>
          <span>{formatDurationUs(startUs)}</span>
          <span>{formatDurationUs((startUs + endUs) / 2)}</span>
          <span>{formatDurationUs(endUs)}</span>
        </div>
      </div>
    </div>
  )
}
