import { useEffect, useState } from 'react'
import {
  Archive,
  CircleCheck,
  Database,
  Gauge,
  HardDrive,
  Save,
  ShieldAlert,
  WifiOff,
} from 'lucide-react'
import { api } from '../api/client'
import type { Settings } from '../api/types'
import { ErrorState, LoadingState, PageHeader, Panel } from '../components/ui'
import { useApi } from '../hooks/useApi'

export default function SettingsPage() {
  const state = useApi(() => api.settings(), [])
  const [draft, setDraft] = useState<Settings>()
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState<string>()
  const [saveError, setSaveError] = useState<Error>()

  useEffect(() => {
    if (state.data) setDraft(state.data)
  }, [state.data])

  if (state.loading && !draft) return <LoadingState label="Loading archive settings" />
  if (state.error && !draft) return <ErrorState error={state.error} retry={state.refresh} />
  if (!draft) return null
  const crfChanged = state.data != null && draft.transcode_crf !== state.data.transcode_crf

  const save = async () => {
    setSaving(true)
    setSaveError(undefined)
    try {
      setDraft(await api.saveSettings(draft))
      setMessage('Settings saved')
      window.setTimeout(() => setMessage(undefined), 4000)
    } catch (error) {
      setSaveError(error instanceof Error ? error : new Error('Could not save settings'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      <PageHeader
        eyebrow="System"
        title="Settings"
        description="Effective deployment settings. Only AV1 quality is runtime-editable here; container and on-device policy remain explicit and read-only."
        actions={<button className="button button-primary" type="button" disabled={saving || !crfChanged} onClick={() => void save()}><Save size={16} /> {saving ? 'Saving…' : 'Save AV1 quality'}</button>}
      />
      {message && <div className="notice notice-success"><CircleCheck size={16} /> {message}</div>}
      {saveError && <div className="notice notice-error">{saveError.message}</div>}

      <div className="settings-layout">
        <Panel kicker="Retention" title="Archive storage">
          <div className="settings-fields">
            <label className="field">
              <span>Bulk archive path</span>
              <div className="input-icon"><Archive size={16} /><input value={draft.archive_path} disabled /></div>
              <small>Configured by the container mount and shown read-only here.</small>
            </label>
            <label className="toggle-field">
              <div><HardDrive /><span><strong>Retain original logs</strong><small>Keep rlogs, qlogs, and other non-video source artifacts immutable.</small></span></div>
              <input type="checkbox" checked={draft.raw_log_retention_enabled} disabled />
            </label>
            <label className="toggle-field">
              <div><HardDrive /><span><strong>Retain original camera video</strong><small>When off, source HEVC/TS is pruned only after its verified AV1 replacement is cataloged.</small></span></div>
              <input type="checkbox" checked={draft.raw_video_retention_enabled} disabled />
            </label>
          </div>
        </Panel>

        <Panel kicker="AV1" title="Transcode worker">
          <div className="settings-fields two-column-fields">
            <label className="field">
              <span>Encoder</span>
              <select value={draft.transcode_codec} disabled>
                <option value="av1">AV1 · encoder fixed by deployment</option>
              </select>
            </label>
            <label className="field">
              <span>CRF</span>
              <input type="number" min={20} max={63} value={draft.transcode_crf} onChange={(event) => setDraft({ ...draft, transcode_crf: Number(event.target.value) })} />
            </label>
            <label className="field">
              <span>Worker concurrency</span>
              <input type="number" value={draft.worker_concurrency} disabled />
              <small>Fixed by the worker deployment; this browser cannot change process concurrency.</small>
            </label>
          </div>
        </Panel>

        <Panel kicker="Transport" title="Upload policy">
          <div className="settings-fields">
            <label className="toggle-field">
              <div><WifiOff /><span><strong>Allow metered-network uploads</strong><small>When off, files remain protected in the on-device spool.</small></span></div>
              <input type="checkbox" checked={draft.metered_uploads_allowed} disabled />
            </label>
            <small className="field-note">
              Informational server value only. Metered-network behavior is enforced by the comma agent configuration and is not remotely changed by this page.
            </small>
            <label className="field">
              <span>Display timezone</span>
              <input value={draft.timezone} disabled />
              <small>UTC remains authoritative in the API; times are rendered locally in the browser.</small>
            </label>
          </div>
        </Panel>

        <Panel kicker="Safety" title="Guardrails">
          <div className="guardrail-list">
            <div><ShieldAlert /><span><strong>Logs are never pruned</strong><small>Only source camera video is eligible, after a verified AV1 derivative exists for every reference.</small></span></div>
            <div><Database /><span><strong>SQLite stays local</strong><small>Mutable catalog state never runs on the SMB archive mount.</small></span></div>
            <div><Gauge /><span><strong>Bounded worker</strong><small>One AV1 task at a time prevents server contention.</small></span></div>
          </div>
        </Panel>
      </div>
    </>
  )
}
