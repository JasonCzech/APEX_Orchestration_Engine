/**
 * Start (F-01, F-09, F-16): the first thing New test asks.
 * a. What are you testing? The primary system: project, application and tier.
 *    It sets the owning project and scopes the presets offered.
 * b. How do you want to start? From a preset, from a recent test, by resuming
 *    a draft, or blank. Choosing one fills the wizard before anything is typed,
 *    so a preset never overwrites work.
 */
import { useState } from 'react'

import { useDraftsList } from '@/api/hooks/useDrafts'
import { useApplicationsIndex } from '@/api/hooks/useEnvironments'
import { CachedDataWarning } from '@/components/CachedDataWarning'
import { usePipelines } from '@/api/hooks/usePipelines'
import { usePresets } from '@/api/hooks/useTestPresets'
import { useOptionalConsumer } from '@/auth/AuthProvider'
import { PresetPicker } from '@/features/presets/PresetPicker'
import type { Preset } from '@/features/presets/presetModel'
import { useFieldNames } from '@/features/presets/useFieldNames'
import { formatRelative } from '@/utils/time'

import { accessibleProjects, reachableProjects, type TestParticipant } from './testScope'

export interface StartPrimary {
  projectId: string
  applicationId: string
  layer: TestParticipant['layer']
}

type StartOption = 'preset' | 'recent' | 'draft' | 'blank'

const LAYERS: { id: TestParticipant['layer']; label: string }[] = [
  { id: 'frontend', label: 'Frontend' },
  { id: 'middle', label: 'Middle tier' },
  { id: 'backend', label: 'Backend' },
]

export function StartPanel({ onPreset, onRecent, onDraft, onBlank, busy = false }: {
  onPreset: (preset: Preset, primary: StartPrimary | null) => void
  onRecent: (threadId: string) => Promise<void>
  onDraft: (draftId: string) => void
  onBlank: (primary: StartPrimary | null) => void
  /** A choice is being loaded (e.g. a draft); every start action waits. */
  busy?: boolean
}) {
  const consumer = useOptionalConsumer()
  const names = useFieldNames()
  const applications = useApplicationsIndex()
  const presets = usePresets()
  const [projectId, setProjectId] = useState('')
  const [applicationId, setApplicationId] = useState('')
  const [layer, setLayer] = useState<TestParticipant['layer']>('backend')
  const [option, setOption] = useState<StartOption | null>(null)
  const [copying, setCopying] = useState<string | null>(null)
  const [copyError, setCopyError] = useState<string | null>(null)
  const visibleApps = (applications.data ?? []).filter(app => !app.archived_at)
  const projects = reachableProjects(visibleApps, consumer)
    .sort((a, b) => names.project(a).localeCompare(names.project(b)))
  const projectApps = visibleApps.filter(app => accessibleProjects(app, consumer).includes(projectId))
  const primary: StartPrimary | null = projectId && applicationId ? { projectId, applicationId, layer } : null
  const recent = usePipelines({ project: projectId || undefined, limit: 50 })
  const drafts = useDraftsList(projectId || undefined)
  const recentTests = (recent.data?.items ?? []).filter(test => {
    if (!applicationId) return true
    const participants = (test.test_scope as { participants?: { application_id?: string }[] } | null | undefined)?.participants
    return test.app_id === applicationId || Boolean(participants?.some(p => p.application_id === applicationId))
  }).slice(0, 10)
  const usablePresets = (presets.data ?? []).filter(preset => !preset.archived)
  const presetCount = applicationId
    ? usablePresets.filter(p => p.applicationId === applicationId || p.applicationId === null).length
    : usablePresets.length
  const chosen = option ?? (presetCount > 0 ? 'preset' : 'blank')
  const options: { id: StartOption; label: string; caption: string }[] = [
    { id: 'preset', label: 'From a preset', caption: presets.isPending ? 'Loading presets…' : `${presetCount} ${presetCount === 1 ? 'preset' : 'presets'}${applicationId ? ` for ${names.applicationName(applicationId)} or any application` : ''}` },
    { id: 'recent', label: 'From a recent test', caption: recent.isPending ? 'Loading tests…' : `${recentTests.length} recent ${recentTests.length === 1 ? 'test' : 'tests'}` },
    { id: 'draft', label: 'Resume a draft', caption: drafts.isPending ? 'Loading drafts…' : `${drafts.data?.length ?? 0} ${drafts.data?.length === 1 ? 'draft' : 'drafts'}` },
    { id: 'blank', label: 'Blank test', caption: 'Fill in every step yourself' },
  ]

  async function copyRecent(threadId: string) {
    setCopying(threadId)
    setCopyError(null)
    try {
      await onRecent(threadId)
    } catch (failure) {
      setCopyError(failure instanceof Error ? failure.message : 'Could not copy this test setup.')
    } finally {
      setCopying(null)
    }
  }

  return (
    <section className="glass-panel wizard-start" aria-labelledby="wizard-start-title">
      <h2 id="wizard-start-title" className="wizard-section-title">Start a new test</h2>
      <fieldset className="wizard-start-primary">
        <legend className="wizard-start-heading">1. What are you testing?</legend>
        <p className="wizard-caption">Choose the primary system under test. Its project owns the test; presets are offered for its application.</p>
        <div className="wizard-start-fields">
          <label className="ux-field">Project
            <select className="field-select" value={projectId} onChange={event => { setProjectId(event.target.value); setApplicationId('') }}>
              <option value="">Select project…</option>
              {projects.map(id => <option key={id} value={id}>{names.project(id)}</option>)}
            </select>
          </label>
          <label className="ux-field">Application
            <select className="field-select" value={applicationId} disabled={!projectId} onChange={event => setApplicationId(event.target.value)}>
              <option value="">Select application…</option>
              {projectApps.map(app => <option key={app.id} value={app.id}>{app.name}</option>)}
            </select>
          </label>
          <div className="ux-field" role="radiogroup" aria-label="Tier">
            <span>Tier</span>
            <div className="wizard-start-tiers">
              {LAYERS.map(item => (
                <label key={item.id} className="wizard-role-chip">
                  <input type="radio" name="start-tier" checked={layer === item.id} onChange={() => setLayer(item.id)} />{item.label}
                </label>
              ))}
            </div>
          </div>
        </div>
      </fieldset>

      <fieldset className="wizard-start-options">
        <legend className="wizard-start-heading">2. How do you want to start?</legend>
        <div className="wizard-start-cards" role="radiogroup" aria-label="How to start">
          {options.map(item => (
            <label key={item.id} className={`glass-panel wizard-start-card${chosen === item.id ? ' wizard-start-card--selected' : ''}`}>
              <input type="radio" name="start-option" className="sr-only" checked={chosen === item.id} onChange={() => setOption(item.id)} />
              <span className="wizard-engine-name">{item.label}</span>
              <span className="wizard-caption">{item.caption}</span>
            </label>
          ))}
        </div>

        {chosen === 'preset' && (
          presets.isError
            ? <p className="wizard-caption wizard-caption--danger" role="alert">Presets failed to load. <button type="button" className="btn btn-secondary btn-sm" onClick={presets.refetch}>Retry</button></p>
            : <PresetPicker presets={usablePresets} applicationId={applicationId || null} names={names} onChoose={preset => onPreset(preset, primary)}
                emptyHint={applicationId ? `No presets for ${names.applicationName(applicationId)} yet. Start blank and save your setup as a preset from Review.` : 'No presets yet.'} />
        )}
        {chosen === 'recent' && (
          <div className="wizard-start-list">
            {copyError && <p className="wizard-caption wizard-caption--danger" role="alert">{copyError}</p>}
            {recentTests.length === 0
              ? <p className="wizard-caption">{recent.isPending ? 'Loading tests…' : 'No recent tests for this selection.'}</p>
              : <ul className="preset-list">{recentTests.map(test => (
                  <li key={test.thread_id} className="preset-row">
                    <div className="preset-row-main"><span className="preset-row-name">{test.title || 'Untitled test'}</span>
                      {test.updated_at && <span className="preset-muted">{formatRelative(test.updated_at)}</span>}</div>
                    <div className="preset-row-actions">
                      <button type="button" className="btn btn-secondary btn-sm" disabled={copying !== null} aria-label={`Use the setup of ${test.title || 'Untitled test'}`} onClick={() => void copyRecent(test.thread_id)}>
                        {copying === test.thread_id ? 'Copying setup…' : 'Use this setup'}
                      </button>
                    </div>
                  </li>
                ))}</ul>}
          </div>
        )}
        {chosen === 'draft' && (
          <div className="wizard-start-list">
            {drafts.isError && drafts.data && <CachedDataWarning error={drafts.error} onRetry={() => void drafts.refetch()} />}
            {drafts.isError && !drafts.data
              ? <p className="tonal-card danger" role="alert">Saved drafts unavailable: {drafts.error.message} <button type="button" className="btn btn-ghost btn-sm" onClick={() => void drafts.refetch()}>Retry</button></p>
              : (drafts.data ?? []).length === 0
              ? <p className="wizard-caption">{drafts.isPending ? 'Loading drafts…' : 'No saved drafts.'}</p>
              : <ul className="preset-list">{(drafts.data ?? []).map(draft => (
                  <li key={draft.id} className="preset-row">
                    <div className="preset-row-main"><span className="preset-row-name">{draft.title || 'Untitled draft'}</span>
                      <span className="preset-muted">{draft.project_id ? names.project(draft.project_id) : 'No project'}{draft.updated_at ? ` · ${formatRelative(draft.updated_at)}` : ''}</span></div>
                    <div className="preset-row-actions">
                      <button type="button" className="btn btn-secondary btn-sm" disabled={busy} aria-label={`Resume ${draft.title || 'Untitled draft'}`} onClick={() => onDraft(draft.id)}>Resume</button>
                    </div>
                  </li>
                ))}</ul>}
          </div>
        )}
        {chosen === 'blank' && (
          <div className="wizard-start-list">
            <p className="wizard-caption">{primary
              ? `Starts with ${names.applicationName(primary.applicationId)} as the ${LAYERS.find(l => l.id === primary.layer)!.label.toLowerCase()} primary system.`
              : 'Starts with nothing selected.'}</p>
            <button type="button" className="btn btn-primary" disabled={busy} onClick={() => onBlank(primary)}>Start blank test</button>
          </div>
        )}
      </fieldset>
    </section>
  )
}
