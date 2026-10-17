import { StateCard } from './ux/StateCard'
import { Link } from 'react-router'
import './FeaturePlaceholder.css'

/**
 * Unknown-route recovery, distinct from an empty or unconfigured feature.
 */
export function FeaturePlaceholder({ title, route }: { title: string; route: string }) {
  return (
    <section className="feature-placeholder glass-panel animate-enter">
      <StateCard kind="not-found" title={title} action={<Link className="btn btn-primary" to="/">Go to Home</Link>}>
        <p>This page could not be found. Check the address or return to Home.</p>
        <code className="feature-placeholder-route">{route}</code>
      </StateCard>
    </section>
  )
}
