import { EvidenceTab } from './EvidenceTab'
import './context.css'

/** Cross-run evidence belongs with investigation, while individual packets link back to their run. */
export function EvidencePage() {
  return (
    <section className="ctx-page animate-enter">
      <header className="ctx-toolbar glass-panel">
        <div className="ctx-heading">
          <h2 className="ctx-page-title">Evidence</h2>
          <p className="ctx-toolbar-caption">
            Review findings produced by test runs. Open a run for its complete phase history.
          </p>
        </div>
      </header>
      <EvidenceTab />
    </section>
  )
}
