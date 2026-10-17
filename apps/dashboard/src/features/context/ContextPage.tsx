/**
 * /context?tab=documents|summaries — the reusable knowledge library. Tab
 * selection lives in the search params so deep links and refreshes keep their
 * place; invalid/missing values fall back to documents. Evidence now has its
 * own investigation route.
 */
import { TabList } from '@/components/ux/TabList'
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
          <p className="ctx-toolbar-caption">Reusable source material for planning and running tests.</p>
        </div>
        <TabList id="knowledge-sections" label="Knowledge sections" items={CONTEXT_TABS.map(id => ({ id, label: TAB_LABELS[id] }))} value={tab} onChange={selectTab} />
      </header>

      {CONTEXT_TABS.map(entry => <div key={entry} id={`knowledge-sections-panel-${entry}`} role="tabpanel" aria-labelledby={`knowledge-sections-tab-${entry}`} hidden={tab !== entry}>{tab === entry && (entry === 'documents' ? <DocumentsTab /> : <SummariesTab lifecycle={summaries} />)}</div>)}
    </section>
  )
}
