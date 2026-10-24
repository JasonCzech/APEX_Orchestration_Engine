import { helpHref } from './catalog'

// Specific routes precede their parent. Query-based wizard steps retain their
// own help destination without coupling the shell to the wizard's large module.
const pageHelp: readonly [string, string, string?][] = [
  ['/runs/compare', 'results', 'compare-runs'],
  ['/runs', 'phases'], ['/approvals', 'phases', 'record-a-review-decision'],
  ['/cycles', 'cycles'], ['/prompts', 'prompts'], ['/test-presets', 'drafts-presets'],
  ['/golden-configs', 'drafts-presets'], ['/work-items', 'tickets'],
  ['/environments', 'catalogs'], ['/context', 'knowledge'], ['/evidence', 'results'],
  ['/analytics/models', 'analytics', 'model-pricing'], ['/analytics', 'analytics'], ['/logs', 'analytics', 'logs'],
  ['/apex-load', 'apex-load'], ['/admin/connections', 'connections'],
  ['/admin/consumers', 'access'], ['/admin/system', 'operations'], ['/settings', 'settings'],
]
const wizardHelp: Record<string, [string, string?]> = {
  scope: ['scope'], config: ['configuration'], tickets: ['tickets'], 'work-items': ['tickets'],
  knowledge: ['knowledge'], context: ['knowledge'], prompts: ['prompts'], review: ['drafts-presets', 'launch-readiness'],
}

export function contextualHelp(pathname: string, search = ''): string | null {
  pathname = pathname.replace(/\/+$/, '') || '/'
  if (pathname === '/runs/new') {
    const step = new URLSearchParams(search).get('step') ?? 'scope'
    const [article, section] = Object.hasOwn(wizardHelp, step) ? wizardHelp[step]! : wizardHelp.scope!
    return helpHref(article, section)
  }
  if (pathname === '/') return helpHref('welcome')
  if (/^\/runs\/[^/]+\/artifacts\//.test(pathname) || /^\/runs\/[^/]+\/timeline$/.test(pathname)) return helpHref('results')
  const match = pageHelp.find(([path]) => pathname === path || pathname.startsWith(`${path}/`))
  return match ? helpHref(match[1], match[2]) : null
}
