import './FeaturePlaceholder.css'

/**
 * D0 stub rendered by every routed screen: a glass panel with an APEX Load
 * style empty state naming the route. Real screens land in D1+.
 */
export function FeaturePlaceholder({ title, route }: { title: string; route: string }) {
  return (
    <section className="feature-placeholder glass-panel animate-enter">
      <div className="dash-empty">
        <h2>{title}</h2>
        <p>This screen is scaffolded and lands in a later dashboard milestone.</p>
        <code className="feature-placeholder-route">{route}</code>
      </div>
    </section>
  )
}
