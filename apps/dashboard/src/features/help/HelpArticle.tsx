import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent, type ReactNode } from 'react'
import Markdown, { type Components } from 'react-markdown'
import { Link, useLocation } from 'react-router'
import { StateCard } from '@/components/ux/StateCard'
import { HELP_OWNER, HELP_REVIEW_DATE, helpHref, type HelpArticle as Article } from './catalog'
import { headings, loadHelpSource, resolveHelpLink } from './content'

export function CopyHelpText({ text, label }: { text: string; label: string }) {
  const [feedback, setFeedback] = useState('')
  return <span className="help-copy">
    <button type="button" className="btn btn-secondary btn-sm" onClick={async () => {
      try {
        if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable')
        await navigator.clipboard.writeText(text)
        setFeedback('Copied')
      } catch { setFeedback('Copy unavailable. Select the text or copy the browser address.') }
    }}>{label}</button>
    <span role="status">{feedback}</span>
  </span>
}

function textContent(children: ReactNode): string {
  if (typeof children === 'string') return children
  if (Array.isArray(children)) return children.map(textContent).join('')
  if (children && typeof children === 'object' && 'props' in children) return textContent((children.props as { children?: ReactNode }).children)
  return ''
}

function ArticleBody({ article, source }: { article: Article; source: string }) {
  const location = useLocation()
  const outline = useMemo(() => headings(source), [source])
  const ref = useRef<HTMLDivElement>(null)
  const [anchorRequest, setAnchorRequest] = useState(0)
  let section = ''
  // The shell's skip link targets the main landmark, outside this article.
  const shellAnchor = location.hash === '#main-content'
  try { section = shellAnchor ? '' : decodeURIComponent(location.hash.slice(1)) } catch { /* Explain malformed anchors below. */ }
  const missing = Boolean(location.hash && !shellAnchor && !outline.some(heading => heading.id === section))
  const requestAnchor = useCallback((event: MouseEvent<HTMLAnchorElement>) => {
    if (event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) setAnchorRequest(value => value + 1)
  }, [])
  useEffect(() => {
    const container = ref.current
    if (!container || !section) return
    const updateOffset = () => {
      const main = container.closest('main')
      const toolbar = main?.querySelector('.main-topbar')
      // Sticky positioning includes the scroll container's inset, not just the
      // header's height. Leave a readable gap even at zoom/wrapped layouts.
      const bottom = toolbar && main ? toolbar.getBoundingClientRect().bottom - main.getBoundingClientRect().top : 80
      container.style.setProperty('--help-anchor-offset', `${bottom + 24}px`)
      const target = Array.from(container.querySelectorAll<HTMLElement>('[id]')).find(node => node.id === section)
      // Reflow can make the sticky header taller while a section has focus.
      // Keep that destination visible without moving a reader focused elsewhere.
      if (target && document.activeElement === target && toolbar && target.getBoundingClientRect().top < toolbar.getBoundingClientRect().bottom + 24) {
        target.scrollIntoView?.({ block: 'start', behavior: 'instant' })
      }
    }
    updateOffset()
    const observer = typeof ResizeObserver === 'undefined' ? undefined : new ResizeObserver(updateOffset)
    const toolbar = container.closest('main')?.querySelector('.main-topbar')
    if (toolbar) observer?.observe(toolbar)
    const frame = requestAnimationFrame(() => {
      const target = Array.from(container.querySelectorAll<HTMLElement>('[id]')).find(node => node.id === section)
      target?.scrollIntoView?.({ block: 'start', behavior: 'instant' })
      target?.focus({ preventScroll: true })
    })
    return () => { cancelAnimationFrame(frame); observer?.disconnect() }
  }, [section, source, anchorRequest])
  // Keep renderer component identities stable across fragment/query changes so
  // headings, example selections, and copy feedback retain their DOM/state.
  const markdownComponents = useMemo<Components>(() => ({
    h1: () => null,
    h2: ({ node, children }) => <h3 id={outline.find(h => h.line === node?.position?.start.line)?.id} tabIndex={-1}>{children}</h3>,
    h3: ({ node, children }) => <h4 id={outline.find(h => h.line === node?.position?.start.line)?.id} tabIndex={-1}>{children}</h4>,
    a: ({ href = '', children }) => {
      const resolved = resolveHelpLink(href, article)
      if (!resolved) return <span>{children}</span>
      return resolved.startsWith('/') ? <Link to={resolved} onClick={resolved.includes('#') ? requestAnchor : undefined}>{children}</Link> : <a href={resolved} target="_blank" rel="noopener noreferrer">{children}</a>
    },
    img: ({ src, alt }) => src?.startsWith('/help/images/') && !src.includes('..') ? <img src={src} alt={alt ?? ''} loading="lazy" /> : null,
    pre: ({ children }) => <div className="help-example"><CopyHelpText text={textContent(children)} label="Copy example" /><pre tabIndex={0}>{children}</pre></div>,
  }), [article, outline, requestAnchor])
  return <div ref={ref}>
    {missing && <p role="status">That section is not in this build. Choose a section from On this page.</p>}
    {outline.length > 0 && <nav className="help-outline" aria-label="On this page">
      <strong>On this page</strong>
      <ul>{outline.map(heading => <li key={heading.id} className={heading.level === 3 ? 'help-outline-nested' : undefined}>
        <Link to={helpHref(article.id, heading.id)} onClick={requestAnchor} aria-current={section === heading.id ? 'location' : undefined}>{heading.text}</Link>
      </li>)}</ul>
    </nav>}
    <article className="help-prose" aria-label={article.title}>
      <Markdown skipHtml components={markdownComponents}>{source}</Markdown>
    </article>
  </div>
}

export function HelpArticle({ article }: { article: Article }) {
  const [state, setState] = useState<{ source?: string; failed?: boolean }>({})
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    let cancelled = false
    setState({})
    void loadHelpSource(article).then(source => { if (!cancelled) setState({ source }) }, () => { if (!cancelled) setState({ failed: true }) })
    return () => { cancelled = true }
  }, [article, attempt])
  const location = useLocation()
  return <>
    <header className="help-article-header">
      <div><p className="help-eyebrow">APEX Orchestration guide</p><h2>{article.title}</h2><p>{article.description}</p></div>
      <CopyHelpText key={location.pathname + location.hash} text={new URL(location.pathname + location.hash, window.location.origin).href} label="Copy article link" />
    </header>
    {state.failed ? <StateCard kind="stale" title="Article could not be loaded" action={<button className="btn btn-secondary" onClick={() => setAttempt(value => value + 1)}>Retry article</button>}>Retry the article or choose another topic.</StateCard>
      : state.source === undefined ? <p role="status">Loading article…</p> : <ArticleBody article={article} source={state.source} />}
    <p className="help-maintenance">Maintained by {HELP_OWNER} · Reviewed {HELP_REVIEW_DATE}. <Link to={helpHref('support', 'documentation-scope')}>Review scope & version</Link></p>
  </>
}
