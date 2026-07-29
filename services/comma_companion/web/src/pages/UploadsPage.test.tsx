import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Device, Upload } from '../api/types'

const apiMocks = vi.hoisted(() => ({
  uploadPage: vi.fn(),
  uploadSnapshot: vi.fn(),
  devices: vi.fn(),
  jobs: vi.fn(),
  jobCounts: vi.fn(),
  command: vi.fn(),
  commandStatus: vi.fn(),
  cancelJob: vi.fn(),
  retryJob: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: apiMocks,
  demoMode: false,
}))

import UploadsPage, { ACTIVE_UPLOAD_POLL_INTERVAL_MS } from './UploadsPage'

const uploadSnapshot = {
  active_uploads: 0,
  failed_uploads: 0,
  completed_uploads: 0,
  bytes_received: 0,
  bytes_expected: 0,
  pending_bytes: 0,
  bytes_per_second_60s: 0,
  by_device: {},
}

const agentFileId = 'a'.repeat(64)

function upload(overrides: Partial<Upload> = {}): Upload {
  return {
    id: 'upload-1',
    device_id: 'device-1',
    filename: 'road.hevc',
    kind: 'road_camera',
    size_bytes: 1_000,
    received_bytes: 400,
    state: 'uploading',
    created_at: '2026-07-29T08:00:00Z',
    updated_at: '2026-07-29T08:01:00Z',
    ...overrides,
  }
}

function device(capabilities: string[], overrides: Partial<Device> = {}): Device {
  return {
    id: 'device-1',
    name: 'Comma',
    state: 'healthy',
    online: true,
    capabilities,
    ...overrides,
  }
}

function arrange(uploads: Upload[], devices: Device[]) {
  apiMocks.uploadPage.mockResolvedValue({
    items: uploads,
    total: uploads.length,
    limit: 100,
    offset: 0,
  })
  apiMocks.uploadSnapshot.mockResolvedValue({
    ...uploadSnapshot,
    active_uploads: uploads.filter((item) => item.state === 'uploading').length,
    failed_uploads: uploads.filter((item) => item.state === 'failed').length,
  })
  apiMocks.devices.mockResolvedValue(devices)
  apiMocks.jobs.mockResolvedValue({ items: [], total: 0 })
  apiMocks.jobCounts.mockResolvedValue({})
}

beforeEach(() => {
  for (const mock of Object.values(apiMocks)) mock.mockReset()
})

afterEach(() => {
  cleanup()
})

it('refreshes active transfer progress every second', () => {
  expect(ACTIVE_UPLOAD_POLL_INTERVAL_MS).toBe(1_000)
})

describe('device upload controls', () => {
  it('confirms a supported cancel and targets the stable agent file', async () => {
    arrange([upload({ file_id: agentFileId })], [device(['command_cancel_upload'])])
    apiMocks.command.mockResolvedValue({
      id: 'command-1',
      device_id: 'device-1',
      type: 'cancel_upload',
      state: 'succeeded',
      message: 'spool data was retained',
    })

    render(<MemoryRouter><UploadsPage /></MemoryRouter>)

    fireEvent.click(await screen.findByRole('button', { name: 'Cancel transfer road.hevc' }))
    expect(screen.getByText('Cancel this transfer? The comma retains its protected spool copy.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm cancel' }))

    await waitFor(() => {
      expect(apiMocks.command).toHaveBeenCalledWith('device-1', {
        type: 'cancel_upload',
        args: { file_id: agentFileId },
        expires_in_seconds: 3_600,
      })
    })
    expect(await screen.findByText('spool data was retained')).toBeInTheDocument()
  })

  it('retries only failed uploads through the advertised device capability', async () => {
    arrange(
      [upload({
        id: 'failed-upload',
        file_id: agentFileId,
        filename: 'rlog.bz2',
        state: 'failed',
      })],
      [device(['command_retry_upload'])],
    )
    apiMocks.command.mockResolvedValue({
      id: 'command-2',
      device_id: 'device-1',
      type: 'retry_upload',
      state: 'succeeded',
      message: 'queued for retry',
    })

    render(<MemoryRouter><UploadsPage /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', { name: 'Retry transfer rlog.bz2' }))

    await waitFor(() => {
      expect(apiMocks.command).toHaveBeenCalledWith('device-1', {
        type: 'retry_upload',
        args: { file_id: agentFileId },
        expires_in_seconds: 3_600,
      })
    })
  })

  it('uses upload_id fallback only for a row explicitly marked as device-origin', async () => {
    arrange(
      [upload({ source_kind: 'device' })],
      [device(['command_cancel_upload'])],
    )
    apiMocks.command.mockResolvedValue({
      id: 'command-legacy',
      device_id: 'device-1',
      type: 'cancel_upload',
      state: 'succeeded',
    })

    render(<MemoryRouter><UploadsPage /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel transfer road.hevc' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm cancel' }))

    await waitFor(() => {
      expect(apiMocks.command).toHaveBeenCalledWith('device-1', {
        type: 'cancel_upload',
        args: { upload_id: 'upload-1' },
        expires_in_seconds: 3_600,
      })
    })
  })

  it('does not render device commands for importer or unmarked historical rows', async () => {
    arrange(
      [
        upload({
          id: 'imported-upload',
          filename: 'imported.hevc',
          source_kind: 'importer',
        }),
        upload({
          id: 'unmarked-upload',
          filename: 'historical-rlog.bz2',
          state: 'failed',
        }),
      ],
      [device(['command_cancel_upload', 'command_retry_upload'])],
    )

    render(<MemoryRouter><UploadsPage /></MemoryRouter>)

    expect(await screen.findByText('imported.hevc')).toBeInTheDocument()
    expect(screen.getByText('historical-rlog.bz2')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Cancel transfer imported.hevc' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry transfer historical-rlog.bz2' })).not.toBeInTheDocument()
    expect(apiMocks.command).not.toHaveBeenCalled()
  })

  it('explains why an unadvertised command is unavailable', async () => {
    arrange([upload({ file_id: agentFileId })], [device([])])

    render(<MemoryRouter><UploadsPage /></MemoryRouter>)

    const button = await screen.findByRole('button', { name: 'Cancel transfer road.hevc' })
    expect(button).toBeDisabled()
    expect(screen.getByText(
      'The agent did not advertise command_cancel_upload; this action is unavailable.',
    )).toBeInTheDocument()
    expect(apiMocks.command).not.toHaveBeenCalled()
  })
})
