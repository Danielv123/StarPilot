import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Device } from '../api/types'

const apiMocks = vi.hoisted(() => ({
  device: vi.fn(),
  commands: vi.fn(),
  command: vi.fn(),
  commandStatus: vi.fn(),
  confirmPassword: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: apiMocks,
  demoMode: false,
}))

import DeviceDetailPage from './DeviceDetailPage'

const enrolledDevice: Device = {
  id: 'device-1',
  name: 'Ioniq comma',
  state: 'healthy',
  online: true,
  offroad: true,
  capabilities: ['command_rescan', 'command_retry_upload', 'command_cancel_upload'],
}

beforeEach(() => {
  for (const mock of Object.values(apiMocks)) mock.mockReset()
  apiMocks.device.mockResolvedValue(enrolledDevice)
})

afterEach(() => {
  cleanup()
})

describe('recent device commands', () => {
  it('loads the bounded per-device audit history with terminal outcomes', async () => {
    apiMocks.commands.mockResolvedValue([
      {
        id: 'command-failed',
        device_id: 'device-1',
        type: 'retry_upload',
        state: 'failed',
        issued_at: '2026-07-29T09:00:00Z',
        finished_at: '2026-07-29T09:00:05Z',
        error: 'no matching retained spool data',
      },
      {
        id: 'command-ok',
        device_id: 'device-1',
        type: 'rescan',
        state: 'succeeded',
        issued_at: '2026-07-29T08:00:00Z',
        finished_at: '2026-07-29T08:00:02Z',
        message: 'scan requested',
      },
    ])

    render(
      <MemoryRouter initialEntries={['/devices/device-1']}>
        <Routes>
          <Route path="/devices/:deviceId" element={<DeviceDetailPage />} />
        </Routes>
      </MemoryRouter>,
    )

    const history = await screen.findByText('Recent commands')
    const panel = history.closest('section')
    expect(panel).not.toBeNull()
    expect(within(panel as HTMLElement).getByText('Retry upload')).toBeInTheDocument()
    expect(within(panel as HTMLElement).getByText('failed')).toBeInTheDocument()
    expect(within(panel as HTMLElement).getByText('no matching retained spool data')).toBeInTheDocument()
    expect(within(panel as HTMLElement).getByText('Rescan')).toBeInTheDocument()
    expect(within(panel as HTMLElement).getByText('scan requested')).toBeInTheDocument()
    await waitFor(() => expect(apiMocks.commands).toHaveBeenCalledWith('device-1', 8))
  })
})
