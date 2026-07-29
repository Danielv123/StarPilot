import {
  BadgeCheck,
  Binary,
  BrainCircuit,
  Clock3,
  FileKey2,
  GitCommitHorizontal,
  ShieldCheck,
  Sparkles,
} from 'lucide-react'
import { api } from '../api/client'
import { EmptyState, ErrorState, LoadingState, PageHeader, Panel, StatusBadge } from '../components/ui'
import { useApi } from '../hooks/useApi'
import { formatDurationUs, formatLocalDate } from '../utils'

export default function ModelsPage() {
  const state = useApi(() => api.models(), [])
  if (state.loading && !state.data) return <LoadingState label="Loading model registry" />
  if (state.error && !state.data) return <ErrorState error={state.error} retry={state.refresh} title="Model worker is unavailable" />
  const models = state.data ?? []

  return (
    <>
      <PageHeader
        eyebrow="Dynamics workbench"
        title="Models"
        description="Registered vehicle response models, including blocked historical artifacts and the exact provenance behind eligible counterfactuals."
      />
      {!models.length ? (
        <Panel>
          <EmptyState title="No replay models registered" description="Register an allow-listed model artifact to enable Tune mode." icon={<BrainCircuit />} />
        </Panel>
      ) : (
        <div className="model-grid">
          {models.map((model) => (
            <Panel className="model-card" key={model.id}>
              <div className="model-card-heading">
                <div className="model-icon"><BrainCircuit /></div>
                <div>
                  <div className="eyebrow">{model.vehicle}</div>
                  <h2>{model.name}</h2>
                  <span>Version {model.version}</span>
                </div>
                <StatusBadge
                  state={model.state}
                  label={
                    model.eligible
                      ? 'eligible for replay'
                      : model.enabled
                        ? 'blocked'
                        : 'historical · disabled'
                  }
                />
              </div>
              <p className="model-description">{model.description}</p>
              {!model.eligible && (
                <div className="notice notice-warning model-eligibility">
                  <strong>This model cannot be selected or run.</strong>
                  {model.eligibility_reasons.map((reason) => (
                    <small key={reason}>{reason}</small>
                  ))}
                </div>
              )}
              <div className="model-facts">
                <div><Clock3 /><span>History required</span><strong>{formatDurationUs(model.history_us, true)}</strong></div>
                <div><Sparkles /><span>Validated horizon</span><strong>{formatDurationUs(model.horizon_us, true)}</strong></div>
                <div><Binary /><span>Replay mode</span><strong>{model.mode.replaceAll('_', ' ')}</strong></div>
                <div><FileKey2 /><span>SHA-256</span><strong className="mono" title={model.hash}>{model.hash.slice(0, 16)}…</strong></div>
              </div>
              <div className="model-card-footer">
                <span>
                  <BadgeCheck size={15} />
                  {model.eligible ? 'Integrity and causal provenance verified' : 'Registry history only'}
                </span>
                <span>{model.last_used_at ? `Last used ${formatLocalDate(model.last_used_at)}` : 'Not yet used'}</span>
              </div>
            </Panel>
          ))}
        </div>
      )}

      <Panel kicker="Interpretation" title="What a replay means">
        <div className="interpretation-grid">
          <div><ShieldCheck /><strong>Offline only</strong><p>Candidate parameters are simulated against an archived window. They are never written to the car.</p></div>
          <div><GitCommitHorizontal /><strong>Reproducible</strong><p>Every result pins the model hash, StarPilot commit, extractor version, rlog hashes, and complete request.</p></div>
          <div><BrainCircuit /><strong>Estimate, not ground truth</strong><p>Ensemble disagreement and invalid conditions remain visible beside every candidate trace.</p></div>
        </div>
      </Panel>
    </>
  )
}
