import { useEffect, useState } from 'react'
import {
  Archive,
  CalendarDays,
  Camera,
  CheckCircle2,
  Clock3,
  Database,
  FileCode2,
  Film,
  LayoutGrid,
  List,
  MapPin,
  Search,
  SlidersHorizontal,
} from 'lucide-react'
import { Link } from 'react-router'
import { api } from '../api/client'
import type { DriveReadiness } from '../api/types'
import { DriveProgressBars, DriveRow, driveStatusLabel } from '../components/RecordRows'
import { EmptyState, ErrorState, LoadingState, PageHeader, Panel, StatusBadge } from '../components/ui'
import { useApi } from '../hooks/useApi'
import { formatBytes, formatDistance, formatDurationUs, formatLocalDate } from '../utils'

export default function DrivesPage() {
  const pageSize = 50
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [readiness, setReadiness] = useState<'' | DriveReadiness>('')
  const [view, setView] = useState<'list' | 'grid'>('list')
  const [offset, setOffset] = useState(0)
  useEffect(() => {
    const timeout = window.setTimeout(() => {
      setDebouncedSearch(search.trim())
      setOffset(0)
    }, 250)
    return () => window.clearTimeout(timeout)
  }, [search])
  const state = useApi(
    () => api.drives(
      debouncedSearch || undefined,
      readiness || undefined,
      pageSize,
      offset,
    ),
    [debouncedSearch, readiness, offset],
  )
  const page = state.data
  const drives = page?.items ?? []
  const currentPage = Math.floor((page?.offset ?? offset) / pageSize) + 1
  const pageCount = Math.max(1, Math.ceil((page?.total ?? 0) / pageSize))

  return (
    <>
      <PageHeader
        eyebrow="Historical archive"
        title="Drives"
        description="Playback always comes from the verified server backup—never from storage still on the comma."
      />
      <Panel className="catalog-toolbar-panel">
        <div className="catalog-toolbar">
          <label className="search-field search-field-wide">
            <Search size={17} />
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              maxLength={256}
              placeholder="Search route, device, vehicle, or commit"
            />
          </label>
          <label className="select-field">
            <SlidersHorizontal size={16} />
            <select
              value={readiness}
              onChange={(event) => {
                setReadiness(event.target.value as '' | DriveReadiness)
                setOffset(0)
              }}
            >
              <option value="">All backup states</option>
              <option value="ready">Fully ready</option>
              <option value="processing">Processing</option>
              <option value="partial">Partial</option>
              <option value="importing">Importing / uploading</option>
              <option value="failed">Failed</option>
            </select>
          </label>
          <div className="view-switch" aria-label="Drive view">
            <button type="button" className={view === 'list' ? 'active' : ''} onClick={() => setView('list')} aria-label="List view"><List size={17} /></button>
            <button type="button" className={view === 'grid' ? 'active' : ''} onClick={() => setView('grid')} aria-label="Grid view"><LayoutGrid size={17} /></button>
          </div>
        </div>
        <div className="catalog-summary">
          <span><Archive size={15} /> {(page?.total ?? 0).toLocaleString()} drives</span>
          <span><Clock3 size={15} /> {formatDurationUs(page?.summary.duration_us ?? 0)} recorded</span>
          <span><CheckCircle2 size={15} /> {(page?.summary.by_readiness.ready ?? 0).toLocaleString()} fully backed up</span>
          <span><Database size={15} /> {formatBytes(page?.summary.stored_bytes ?? 0)} stored</span>
        </div>
      </Panel>

      {state.loading && !state.data ? (
        <LoadingState label="Searching the drive catalog" />
      ) : state.error && !state.data ? (
        <ErrorState error={state.error} retry={state.refresh} />
      ) : !drives.length ? (
        <Panel>
          <EmptyState
            title="No matching drives"
            description={search || readiness ? 'Try a broader search or remove the backup-state filter.' : 'Uploaded routes will appear here after cataloging.'}
            icon={<Archive />}
          />
        </Panel>
      ) : view === 'list' ? (
        <Panel className="drive-catalog-list">
          <div className="drive-list">
            {drives.map((drive) => <DriveRow drive={drive} key={drive.id} />)}
          </div>
        </Panel>
      ) : (
        <div className="drive-grid">
          {drives.map((drive) => {
            return (
              <Link to={`/drives/${encodeURIComponent(drive.id)}`} className="drive-card" key={drive.id}>
                <div className="drive-thumb">
                  {drive.thumbnail_url ? <img src={drive.thumbnail_url} alt="" /> : <Film size={38} />}
                  <div className="backup-source"><Archive size={13} /> SERVER COPY</div>
                  <span>{formatDurationUs(drive.duration_us)}</span>
                </div>
                <div className="drive-card-content">
                  <div className="drive-card-title">
                    <div><strong>{formatLocalDate(drive.started_at)}</strong><span className="mono">{drive.route_name}</span></div>
                    <StatusBadge
                      state={drive.readiness === 'ready' ? 'healthy' : drive.readiness === 'partial' ? 'warning' : 'running'}
                      label={driveStatusLabel(drive)}
                    />
                  </div>
                  <div className="drive-card-route">
                    <MapPin size={15} />
                    <span>{drive.location_start ?? 'Unknown start'} <i>→</i> {drive.location_end ?? 'Unknown end'}</span>
                  </div>
                  <div className="drive-card-stats">
                    <span><CalendarDays size={14} /> {formatDistance(drive.distance_m)}</span>
                    <span><Camera size={14} /> {drive.cameras.filter((camera) => camera.available).length} cameras</span>
                    <span><FileCode2 size={14} /> {drive.telemetry_ready ? 'indexed' : 'pending'}</span>
                  </div>
                  <DriveProgressBars drive={drive} className="drive-card-progress" />
                  <div className="drive-card-footer">
                    <span>{drive.vehicle}</span>
                    <strong>{formatBytes(drive.derived_bytes)} AV1</strong>
                  </div>
                </div>
              </Link>
            )
          })}
        </div>
      )}
      {page && page.total > 0 && (
        <nav className="catalog-pagination" aria-label="Drive catalog pages">
          <span>
            Showing {page.offset + 1}–{Math.min(page.offset + page.items.length, page.total)} of {page.total.toLocaleString()}
          </span>
          <div>
            <button
              type="button"
              className="button button-secondary"
              disabled={page.offset === 0 || state.loading}
              onClick={() => setOffset(Math.max(0, page.offset - page.limit))}
            >
              Previous
            </button>
            <strong>Page {currentPage} of {pageCount}</strong>
            <button
              type="button"
              className="button button-secondary"
              disabled={page.offset + page.items.length >= page.total || state.loading}
              onClick={() => setOffset(page.offset + page.limit)}
            >
              Next
            </button>
          </div>
        </nav>
      )}
    </>
  )
}
