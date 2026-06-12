/**
 * /context?tab=summaries|documents|evidence (plan Part 2 route table) — the
 * context library. Tab selection lives in the search params so deep links and
 * refreshes keep their place; invalid/missing values fall back to summaries.
 */
import { useSearchParams } from 'react-router'

import { CONTEXT_TABS, isContextTab, type ContextTab } from './contextLogic'
import { DocumentsTab } from './DocumentsTab'
import { EvidenceTab } from './EvidenceTab'
import { SummariesTab } from './SummariesTab'
import './context.css'

const TAB_LABELS: Record<ContextTab, string> = {
  summaries: 'Summaries',
  documents: 'Documents',
  evidence: 'Evidence',
}

export function ContextPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const rawTab = searchParams.get('tab')
  const tab: ContextTab = isContextTab(rawTab) ? rawTab : 'summaries'

  function selectTab(next: ContextTab) {
    const params = new URLSearchParams(searchParams)
    params.set('tab', next)
    setSearchParams(params)
  }

  return (
    <section className="ctx-page animate-enter">
      <header className="ctx-toolbar glass-panel">
        <div className="ctx-tabs" role="tablist" aria-label="Context sections">
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

      {tab === 'summaries' ? <SummariesTab /> : tab === 'documents' ? <DocumentsTab /> : <EvidenceTab />}
    </section>
  )
}
