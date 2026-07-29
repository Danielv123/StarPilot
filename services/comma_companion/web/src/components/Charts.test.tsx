import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { EventTimeline, SignalChart } from './Charts'

describe('SignalChart', () => {
  it('renders typed signal metadata and supports keyboard seeking', () => {
    const seek = vi.fn()
    render(
      <SignalChart
        title="Lateral response"
        series={[
          {
            id: 'lateral.actual_acceleration',
            label: 'Actual lateral accel',
            unit: 'm/s²',
            color: '#63b3ff',
            points: [
              { t_us: 0, value: 0 },
              { t_us: 1_000_000, value: 1 },
            ],
          },
        ]}
        startUs={0}
        endUs={1_000_000}
        playheadUs={500_000}
        onSeek={seek}
      />,
    )

    expect(screen.getByText('Actual lateral accel')).toBeInTheDocument()
    fireEvent.keyDown(screen.getByRole('img'), { key: 'ArrowRight' })
    expect(seek).toHaveBeenCalledWith(600_000)
  })
})

describe('EventTimeline', () => {
  it('exposes a route-relative integer playhead', () => {
    render(
      <EventTimeline
        events={[
          {
            id: 'driver-1',
            lane: 'driver_overlay',
            start_us: 2_000_000,
            end_us: 2_500_000,
            label: 'Driver torque',
          },
        ]}
        startUs={0}
        endUs={10_000_000}
        playheadUs={2_100_000}
        onSeek={() => undefined}
      />,
    )
    expect(screen.getByRole('slider')).toHaveAttribute('aria-valuenow', '2100000')
  })
})
