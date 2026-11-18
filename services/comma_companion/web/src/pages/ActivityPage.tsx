import { Activity, CheckCircle2, CircleAlert, Clock3, Filter, Search } from 'lucide-react'
import { useMemo, useState } from 'react'
import { api } from '../api/client'
import { EmptyState, ErrorState, LoadingState, PageHeader, Panel, StatusBadge } from '../components/ui'
import { useApi } from '../hooks/useApi'
import { formatLocalDate } from '../utils'

export default function ActivityPage() {
  const state = useApi(() => api.activity(), [])
  const [search, setSearch] = useState('')
  const items = useMemo(() => {
    const query = search.trim().toLowerCase()
    return (state.data ?? []).filter((item) => !query || `${item.actor} ${item.action} ${item.target} ${item.detail}`.toLowerCase().includes(query))
  }, [state.data, search])

  return (
    <>
      <PageHeader eyebrow="Audit trail" title="Activity" description="Upload, worker, simulation, authentication, and remote-control events in one immutable sequence." />
      <Panel className="activity-toolbar">
        <label className="search-field search-field-wide">
          <Search size={17} />
          <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search actor, action, or target" />
        </label>
        <button className="button button-secondary" type="button"><Filter size={15} /> Filters</button>
      </Panel>
      <Panel className="activity-panel">
        {state.loading && !state.data ? (
          <LoadingState label="Loading audit events" />
        ) : state.error && !state.data ? (
          <ErrorState error={state.error} retry={state.refresh} />
        ) : !items.length ? (
          <EmptyState title="No matching activity" description="New archive and control events will appear here." icon={<Activity />} />
        ) : (
          <div className="activity-list">
            {items.map((item) => (
              <article className="activity-row" key={item.id}>
                <div className={`activity-icon status-${item.state}`}>
                  {item.state === 'healthy' ? <CheckCircle2 /> : item.state === 'running' ? <Clock3 /> : <CircleAlert />}
                </div>
                <time dateTime={item.created_at}>{formatLocalDate(item.created_at)}</time>
                <div>
                  <strong>{item.action}</strong>
                  <span>{item.target}</span>
                </div>
                <div>
                  <strong>{item.actor}</strong>
                  <span>{item.detail}</span>
                </div>
                <StatusBadge state={item.state} />
              </article>
            ))}
          </div>
        )}
      </Panel>
    </>
  )
}
