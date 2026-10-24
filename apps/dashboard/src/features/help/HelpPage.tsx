import { lazy, Suspense } from 'react'
import { Link, useParams, useSearchParams } from 'react-router'
import { useConsumer, useAuth } from '@/auth/AuthProvider'
import { StateCard } from '@/components/ux/StateCard'
import { HELP_AREAS, HELP_REVIEW_DATE, helpArticles, helpHref, visibleArticles } from './catalog'
import { HelpArticle } from './HelpArticle'
import { HelpSearchResults } from './HelpSearch'
import './help.css'

const ApiReference = lazy(() => import('./ApiReference'))

export function HelpPage() {
  const { articleId = 'welcome' } = useParams()
  const [params, setParams] = useSearchParams()
  const consumer = useConsumer()
  const { state } = useAuth()
  const isAdmin = consumer?.role === 'admin'
  const query = params.get('q') ?? ''
  const article = helpArticles.find(item => item.id === articleId)
  const activeArea = article && (!article.admin || isAdmin) ? article.area : 'getting-started'
  const articles = visibleArticles(isAdmin)
  return <div className="help-workspace">
    <section className="help-search" aria-label="Search documentation">
      <label htmlFor="help-search">Search documentation</label>
      <div><input id="help-search" type="search" value={query} autoComplete="off" placeholder="Try traffic entry, reviews, or API keys" onChange={event => {
        setParams(previous => { const next = new URLSearchParams(previous); if (event.target.value) next.set('q', event.target.value); else next.delete('q'); return next }, { replace: true })
      }} />{query && <button type="button" className="btn btn-secondary" onClick={() => setParams(previous => { const next = new URLSearchParams(previous); next.delete('q'); return next }, { replace: true })}>Clear search</button>}</div>
    </section>
    <nav className="help-areas" aria-label="Documentation areas">
      {HELP_AREAS.filter(area => area.id !== 'administration' || isAdmin).map(area => <Link key={area.id} to={helpHref(articles.find(item => item.area === area.id)!.id)} aria-current={!query.trim() && activeArea === area.id ? 'page' : undefined}>{area.label}</Link>)}
    </nav>
    <div className="help-columns">
      <aside className="help-sidebar">
        <nav aria-label="Help articles">{HELP_AREAS.filter(area => area.id === activeArea).map(area => <div key={area.id}>
          <h2>{area.label}</h2><ul>{articles.filter(item => item.area === area.id).map(item => <li key={item.id}><Link to={helpHref(item.id)} aria-current={!query.trim() && articleId === item.id ? 'page' : undefined}>{item.title}</Link></li>)}</ul>
        </div>)}</nav>
        {activeArea !== 'troubleshooting' && <nav className="help-more" aria-label="More help"><h2>More help</h2><Link to={helpHref('capabilities')}>Capabilities & limitations</Link><Link to={helpHref('support')}>Support & glossary</Link></nav>}
      </aside>
      <div className="help-reading-pane">
        {query.trim() ? <HelpSearchResults query={query.trim()} isAdmin={isAdmin} />
          : !article ? <StateCard kind="not-found" title="Article not found" action={<Link to="/help">Start with concepts & navigation</Link>}>This article is not in the current dashboard build. Search or choose a topic from the navigation.</StateCard>
            : article.admin && !isAdmin ? <StateCard kind="unauthorized" title="Administration guide requires an admin role" action={<Link to={helpHref('welcome', 'roles-and-access')}>Read about roles & access</Link>}>Your current session can read the operator guide and API reference. Ask your administrator about administrative tasks.</StateCard>
              : <><HelpArticle key={article.id} article={article} />{article.id === 'api-reference' && <Suspense fallback={<p role="status">Loading API reference…</p>}><ApiReference /></Suspense>}</>}
      </div>
    </div>
    <footer className="help-version">Documentation bundled with this dashboard · Reviewed {HELP_REVIEW_DATE}{state.status === 'authenticated' && <> · Connected system {state.systemInfo.version}</>}. <Link to={helpHref('capabilities')}>Capabilities & limitations</Link></footer>
  </div>
}
