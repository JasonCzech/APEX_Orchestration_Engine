/** Use the same attribution language at authoring, review and durable evidence. */
export function promptSourceLabel(source: { origin?: unknown; ref?: unknown; editor?: unknown }): string {
  const origin = source.origin === 'run_override' ? 'Edited for this run' : source.origin === 'catalog' ? 'Catalog' : typeof source.origin === 'string' ? source.origin.replaceAll('_', ' ') : 'Source not recorded'
  const editor = typeof source.editor === 'string' && source.editor ? ` by ${source.editor}` : ''
  const ref = typeof source.ref === 'string' && source.ref ? ` · ${source.ref}` : ''
  return `${origin}${editor}${ref}`
}
export function promptSourceTone(source: { origin?: unknown }): 'warning' | 'accent' {
  return source.origin === 'run_override' ? 'warning' : 'accent'
}
