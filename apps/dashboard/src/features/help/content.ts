import { helpArticles, helpHref, type HelpArticle } from './catalog'

const sources = import.meta.glob<string>('../../../../../docs/help/*.md', { query: '?raw', import: 'default' })
const pending = new Map<string, Promise<string>>()

export function loadHelpSource(article: HelpArticle): Promise<string> {
  let promise = pending.get(article.id)
  if (!promise) {
    const loader = sources[`../../../../../docs/help/${article.id}.md`]
    promise = loader ? loader() : Promise.reject(new Error('Article is not bundled in this build.'))
    pending.set(article.id, promise)
    void promise.catch(() => pending.delete(article.id))
  }
  return promise
}

export function headings(source: string) {
  const counts = new Map<string, number>()
  let fence = ''
  return source.split('\n').flatMap((line, index) => {
    const marker = /^\s*(`{3,}|~{3,})/.exec(line)
    if (marker) { const character = marker[1]!.charAt(0); fence = fence ? (character === fence ? '' : fence) : character; return [] }
    if (fence) return []
    const match = /^(#{2,3}) (.+?)\s*#*\s*$/.exec(line)
    if (!match) return []
    const text = match[2]!.replace(/[*`]/g, '')
    const base = text.toLowerCase().normalize('NFKD').replace(/[\u0300-\u036f]/g, '').replace(/[^a-z0-9\s-]/g, '').trim().replace(/\s+/g, '-')
    const count = counts.get(base) ?? 0
    counts.set(base, count + 1)
    return [{ id: `${base}${count ? `-${count}` : ''}`, text, level: match[1]!.length, line: index + 1 }]
  })
}

/** Resolve repository article links into SPA URLs; never execute raw HTML. */
export function resolveHelpLink(href: string, current: HelpArticle): string | null {
  if (href.startsWith('#')) return helpHref(current.id) + href
  const relative = /^([a-z0-9-]+)\.md(#[^\s]*)?$/.exec(href)
  if (relative) return helpArticles.some(article => article.id === relative[1]) ? helpHref(relative[1]!) + (relative[2] ?? '') : null
  if (href.startsWith('/') && !href.startsWith('//') && !/[\\\s]/.test(href)) return href
  try {
    const url = new URL(href)
    if (url.protocol === 'https:' && !url.username && !url.password) return url.href
  } catch { /* Unsupported destinations render as text. */ }
  return null
}

export interface IndexedArticle { article: HelpArticle; source: string }
export function searchHelp(index: IndexedArticle[], query: string) {
  const terms = query.toLowerCase().trim().split(/\s+/).filter(Boolean)
  if (!terms.length) return []
  return index.flatMap(({ article, source }) => {
    const body = source.replace(/!\[[^\]]*\]\([^)]*\)/g, '').replace(/\[([^\]]+)\]\([^)]*\)/g, '$1').replace(/[#*`]/g, '').replace(/\s+/g, ' ')
    const haystack = `${article.title} ${article.keywords} ${body}`.toLowerCase()
    if (!terms.every(term => haystack.includes(term))) return []
    const position = Math.max(0, body.toLowerCase().indexOf(terms[0]!))
    const outline = headings(source)
    const lines = source.split('\n')
    const section = outline.find((heading, index) => lines.slice(heading.line - 1, outline[index + 1] ? outline[index + 1]!.line - 1 : undefined).join('\n').toLowerCase().includes(terms[0]!))
    return [{ article, snippet: `${position > 40 ? '…' : ''}${body.slice(Math.max(0, position - 40), position + 170)}…`, section: section?.id, rank: terms.filter(term => article.title.toLowerCase().includes(term)).length }]
  }).sort((a, b) => b.rank - a.rank || a.article.title.localeCompare(b.article.title))
}
