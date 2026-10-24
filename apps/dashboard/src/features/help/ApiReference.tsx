import { useMemo } from 'react'
import { useSearchParams } from 'react-router'
import spec from '../../../../../docs/api/apex-v1.openapi.json'
import specUrl from '../../../../../docs/api/apex-v1.openapi.json?url'
import { CopyHelpText } from './HelpArticle'

interface Operation {
  operationId?: string
  summary?: string
  description?: string
  tags?: string[]
  parameters?: unknown[]
  requestBody?: unknown
  responses?: unknown
  security?: unknown
  deprecated?: boolean
}
const METHODS = new Set(['get', 'post', 'put', 'patch', 'delete', 'head', 'options'])
const operations = Object.entries(spec.paths).flatMap(([path, item]) => Object.entries(item).flatMap(([method, value]) => {
  if (!METHODS.has(method)) return []
  return [{ path, method: method.toUpperCase(), ...value as Operation }]
}))
const tags = [...new Set(operations.flatMap(operation => operation.tags ?? []))].sort()
const schemas: Record<string, unknown> = spec.components.schemas

export default function ApiReference() {
  const [params, setParams] = useSearchParams()
  const query = params.get('operation') ?? ''
  const requestedTag = params.get('tag') ?? ''
  const tag = tags.includes(requestedTag) ? requestedTag : ''
  const schema = params.get('schema') ?? ''
  const filtered = useMemo(() => {
    const terms = query.toLowerCase().trim().split(/\s+/).filter(Boolean)
    return operations.filter(operation => (!tag || operation.tags?.includes(tag)) && terms.every(term => `${operation.method} ${operation.path} ${operation.operationId} ${operation.summary} ${operation.description} ${operation.tags}`.toLowerCase().includes(term)))
  }, [query, tag])
  function update(key: string, value: string) {
    setParams(previous => { const next = new URLSearchParams(previous); if (value) next.set(key, value); else next.delete(key); return next }, { replace: true })
  }
  return <section className="help-api" aria-label="Generated domain API reference">
    <h2>Browse domain API operations</h2>
    <p>OpenAPI {spec.openapi} · Contract {spec.info.version} · {operations.length} operations. This reference describes the bundled contract; deployment capabilities and your scope determine which operations you can use.</p>
    <a className="btn btn-secondary" href={specUrl} download="apex-v1.openapi.json">Download OpenAPI contract</a>
    <div className="help-api-filters">
      <label>Search API operations<input type="search" value={query} onChange={event => update('operation', event.target.value)} placeholder="Path, operation ID, or description" /></label>
      <label>API group<select value={tag} onChange={event => update('tag', event.target.value)}><option value="">All groups</option>{tags.map(value => <option key={value}>{value}</option>)}</select></label>
    </div>
    {requestedTag && !tag && <p role="status">The API group “{requestedTag}” is not in this contract. Showing all groups.</p>}
    <p role="status">{filtered.length} matching operations</p>
    {!filtered.length && <p>No matching operations. Clear the search or choose All groups.</p>}
    <ul className="help-operations">{filtered.map(operation => <li key={`${operation.method}:${operation.path}`}>
      <details open={query === operation.operationId || undefined}>
        <summary><span className="help-method">{operation.method}</span> <code>{operation.path}</code><span>{operation.summary ?? operation.operationId}{operation.deprecated ? ' (deprecated)' : ''}</span></summary>
        <p>{operation.description}</p>
        <p>Operation ID: <code>{operation.operationId}</code></p>
        <CopyHelpText text={new URL(`/help/api-reference?operation=${encodeURIComponent(operation.operationId ?? operation.path)}`, window.location.origin).href} label="Copy operation link" />
        <h3>Request, responses & authentication</h3>
        <p>Schema references use the names in the schema lookup below. Parameters state whether they are required; each response entry gives its HTTP status and body contract.</p>
        <pre tabIndex={0}>{JSON.stringify({ parameters: operation.parameters ?? [], ...(operation.requestBody ? { requestBody: operation.requestBody } : {}), responses: operation.responses, security: operation.security ?? 'Use x-api-key. Server-side role, scope, and operation policies apply.' }, null, 2)}</pre>
      </details>
    </li>)}</ul>
    <section aria-label="API schema lookup" className="help-schema">
      <h2>Schema lookup</h2>
      <label>Schema name<select value={schema} onChange={event => update('schema', event.target.value)}><option value="">Choose a schema…</option>{Object.keys(schemas).sort().map(name => <option key={name}>{name}</option>)}</select></label>
      {schema && (Object.hasOwn(schemas, schema) ? <><h3>{schema}</h3><pre tabIndex={0}>{JSON.stringify(schemas[schema], null, 2)}</pre></> : <p role="status">That schema is not in this contract. Choose a schema from the list.</p>)}
    </section>
  </section>
}
