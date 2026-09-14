/**
 * /context?tab=documents|summaries — the reusable knowledge library. Tab
 * selection lives in the search params so deep links and refreshes keep their
 * place; invalid/missing values fall back to documents. Evidence now has its
 * own investigation route.
 */
import { Navigate, useSearchParams } from 'react-router'

import { CONTEXT_TABS, isContextTab, type ContextTab } from './contextLogic'
import { DocumentsTab } from './DocumentsTab'
import { SummariesTab } from './SummariesTab'
import { useSummaryLifecycle } from './useSummaryLifecycle'
import './context.css'

const TAB_LABELS: Record<ContextTab, string> = {
  documents: 'Documents',
  summaries: 'Summaries',
}

export function ContextPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const summaries = useSummaryLifecycle()
  const rawTab = searchParams.get('tab')
  const tab: ContextTab = isContextTab(rawTab) ? rawTab : 'documents'

  if (rawTab === 'evidence') return <Navigate to="/evidence" replace />

  function selectTab(next: ContextTab) {
    const params = new URLSearchParams(searchParams)
    params.set('tab', next)
    setSearchParams(params)
  }

  return (
    <section className="ctx-page animate-enter">
      <header className="ctx-toolbar glass-panel">
        <div className="ctx-heading">
          <h2 className="ctx-page-title">Knowledge &amp; Documents</h2>
          <p className="ctx-toolbar-caption">Reusable source material for planning and running tests.</p>
        </div>
        <div className="ctx-tabs" role="tablist" aria-label="Knowledge sections">
          {CONTEXT_TABS.map((entry) => (
            <button
              key={entry}
              type="button"
              role="tab"
              aria-selected={tab === entry}
              className={`ctx-tab${tab === entry ? ' active' : ''}`}
              onClick={() => selectTab(entry)}
            >
              {TAB_LABELS[entry]}
            </button>
          ))}
        </div>
      </header>

      {tab === 'documents' ? <DocumentsTab /> : <SummariesTab lifecycle={summaries} />}
    </section>
  )
}
