import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router'
import { helpHref, visibleArticles } from './catalog'
import { loadHelpSource, searchHelp, type IndexedArticle } from './content'

export function HelpSearchResults({ query, isAdmin }: { query: string; isAdmin: boolean }) {
  const [index, setIndex] = useState<IndexedArticle[]>([])
  const [loading, setLoading] = useState(true)
  const [failed, setFailed] = useState(0)
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    const articles = visibleArticles(isAdmin)
    void Promise.allSettled(articles.map(async article => ({ article, source: await loadHelpSource(article) }))).then(results => {
      if (cancelled) return
      setIndex(results.flatMap(result => result.status === 'fulfilled' ? [result.value] : []))
      setFailed(results.filter(result => result.status === 'rejected').length)
      setLoading(false)
    })
    return () => { cancelled = true }
  }, [isAdmin, attempt])
  const results = useMemo(() => searchHelp(index.filter(({ article }) => isAdmin || !article.admin), query), [index, query, isAdmin])
  return <section aria-label="Search results" className="help-results">
    <h2>Search results</h2>
    <p role="status">{loading ? 'Searching documentation…' : `${results.length} matching ${results.length === 1 ? 'article' : 'articles'} for “${query}”`}</p>
    {failed > 0 && <p role="alert">{failed} articles could not be searched. <button type="button" className="btn btn-secondary btn-sm" onClick={() => setAttempt(value => value + 1)}>Retry search</button></p>}
    {!loading && !results.length && <p>Try fewer words, an engine name, or a term such as traffic entry, human completion, or permissions. You can also browse the article navigation.</p>}
    <ul>{results.map(result => <li key={result.article.id}><Link to={helpHref(result.article.id, result.section)}>{result.article.title}</Link><p>{result.snippet}</p></li>)}</ul>
    <Link to={`/help/api-reference?operation=${encodeURIComponent(query)}`}>Search domain API operations</Link>
  </section>
}
