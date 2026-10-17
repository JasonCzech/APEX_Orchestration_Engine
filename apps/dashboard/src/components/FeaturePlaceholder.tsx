import { Link } from 'react-router'
import './FeaturePlaceholder.css'

/**
 * Unknown-route recovery, distinct from an empty or unconfigured feature.
 */
export function FeaturePlaceholder({ title, route }: { title: string; route: string }) {
  return (
    <section className="feature-placeholder glass-panel animate-enter">
      <div className="dash-empty">
        <h2>{title}</h2>
        <p>This page could not be found. Check the address or return to Home.</p>
        <code className="feature-placeholder-route">{route}</code>
        <Link className="btn btn-primary" to="/">Go to Home</Link>
      </div>
    </section>
  )
}
