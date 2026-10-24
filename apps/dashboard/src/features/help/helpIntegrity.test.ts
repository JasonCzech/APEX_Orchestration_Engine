import { access, readdir } from 'node:fs/promises'
import { resolve } from 'node:path'
import { matchRoutes, type RouteObject } from 'react-router'
import { describe, expect, it } from 'vitest'
import { appRoutes } from '@/routes/router'
import { HELP_AREAS, helpArticles, visibleArticles } from './catalog'
import { headings, loadHelpSource, resolveHelpLink, searchHelp } from './content'
import { contextualHelp } from './contextualHelp'

const docs = resolve(process.cwd(), '../../docs/help')
const index = await Promise.all(helpArticles.map(async article => ({ article, source: await loadHelpSource(article) })))

describe('documentation integrity', () => {
  it('bundles each cataloged source exactly once and covers all documentation areas', async () => {
    expect(new Set(helpArticles.map(article => article.id)).size).toBe(helpArticles.length)
    expect((await readdir(docs)).filter(path => path.endsWith('.md')).sort()).toEqual(helpArticles.map(article => `${article.id}.md`).sort())
    for (const area of HELP_AREAS) expect(helpArticles.some(article => article.area === area.id)).toBe(true)
    for (const { article, source } of index) {
      expect(source.startsWith(`# ${article.title}\n`), article.id).toBe(true)
      expect(headings(source).length, article.id).toBeGreaterThan(1)
      expect(new Set(headings(source).map(heading => heading.id)).size).toBe(headings(source).length)
    }
  })

  it('resolves every authored article, section, application link, and local screenshot', async () => {
    for (const { article, source } of index) {
      for (const match of source.matchAll(/(!?)\[[^\]]*\]\(([^)]+)\)/g)) {
        const href = match[2]!
        if (match[1]) {
          expect(href.startsWith('/help/images/'), `${article.id}: ${href}`).toBe(true)
          await access(resolve(process.cwd(), 'public', href.slice(1)))
          continue
        }
        const resolved = resolveHelpLink(href, article)
        expect(resolved, `${article.id}: ${href}`).not.toBeNull()
        if (!resolved?.startsWith('/')) continue
        const url = new URL(resolved, 'https://apex.example.test')
        if (url.pathname.startsWith('/help/')) {
          const target = index.find(item => item.article.id === decodeURIComponent(url.pathname.slice(6)))
          expect(target, `${article.id}: ${href}`).toBeDefined()
          if (url.hash) expect(headings(target!.source).map(heading => heading.id), `${article.id}: ${href}`).toContain(decodeURIComponent(url.hash.slice(1)))
        }
        const matches = matchRoutes(appRoutes, url.pathname)
        expect(matches?.at(-1)?.route.path, `${article.id}: ${href}`).not.toBe('*')
        expect(matches, `${article.id}: ${href}`).not.toBeNull()
      }
    }
  })

  it('provides valid contextual help for every major route and every wizard step', () => {
    const flatten = (routes: RouteObject[]): RouteObject[] => routes.flatMap(route => [route, ...flatten(route.children ?? [])])
    const paths = flatten(appRoutes).flatMap(route => route.index ? ['/'] : route.path && !route.path.includes('*') && !route.path.startsWith('help') ? [`/${route.path.replace(/^\/+/, '').replace(/:[^/]+/g, 'sample')}`] : [])
    for (const path of paths) {
      const href = contextualHelp(path)
      expect(href, path).not.toBeNull()
      const url = new URL(href!, 'https://apex.example.test')
      const target = index.find(item => item.article.id === url.pathname.slice(6))
      expect(target, path).toBeDefined()
      if (url.hash) expect(headings(target!.source).map(heading => heading.id), path).toContain(url.hash.slice(1))
    }
    for (const step of ['scope', 'config', 'tickets', 'knowledge', 'prompts', 'review', 'work-items', 'context']) {
      const href = contextualHelp('/runs/new', `?step=${step}`)!
      const url = new URL(href, 'https://apex.example.test')
      const target = index.find(item => item.article.id === url.pathname.slice(6))
      expect(target, step).toBeDefined()
      if (url.hash) expect(headings(target!.source).map(heading => heading.id)).toContain(url.hash.slice(1))
    }
    expect(contextualHelp('/help')).toBeNull()
    expect(contextualHelp('/help/scope')).toBeNull()
    expect(contextualHelp('/unrecognized')).toBeNull()
    expect(contextualHelp('/runs/new', '?step=config')).toBe('/help/configuration')
    expect(contextualHelp('/runs/new/', '?step=config')).toBe('/help/configuration')
    expect(contextualHelp('/runs/sample/artifacts/result')).toBe('/help/results')
  })

  it('indexes full text and keywords with valid result anchors', () => {
    for (const { article } of index) expect(searchHelp(index, article.title).some(result => result.article.id === article.id)).toBe(true)
    const results = searchHelp(index, 'frozen input identity')
    expect(results.some(result => result.article.id === 'analysis')).toBe(true)
    expect(searchHelp(index, 'xyz-no-such-topic-918273')).toEqual([])
    expect(searchHelp(index, '   ')).toEqual([])
    for (const result of searchHelp(index, 'recovery')) {
      if (result.section) expect(headings(index.find(item => item.article.id === result.article.id)!.source).map(heading => heading.id)).toContain(result.section)
    }
    expect(visibleArticles(false).every(article => !article.admin)).toBe(true)
  })
})

describe('help content rules', () => {
  it('uses stable duplicate heading IDs and ignores fenced examples', () => {
    expect(headings('# Title\n## Café & scope\n```md\n## Not a heading\n```\n## Café & scope\n### Evidence').map(heading => heading.id)).toEqual(['cafe-scope', 'cafe-scope-1', 'evidence'])
  })
  it('rejects unsupported links and remaps article-relative links', () => {
    const article = helpArticles[0]!
    for (const href of ['javascript:alert(1)', 'data:text/html,bad', '//evil.example', 'https://user:pass@example.test', '../unknown.md', 'missing.md', 'file:///etc/passwd']) expect(resolveHelpLink(href, article)).toBeNull()
    expect(resolveHelpLink('phases.md#complete-human-work', article)).toBe('/help/phases#complete-human-work')
    expect(resolveHelpLink('#roles-and-access', article)).toBe('/help/welcome#roles-and-access')
  })
  it('returns the section containing the body match rather than the first section', () => {
    const article = helpArticles[0]!
    expect(searchHelp([{ article, source: '## First\nIntroduction\n## Second\nNeedle appears here' }], 'needle')[0]?.section).toBe('second')
  })
  it.each(['constructor', 'toString', '__proto__', 'unknown'])('falls back safely for an unsupported wizard step %s', step => {
    expect(contextualHelp('/runs/new', `?step=${step}`)).toBe('/help/scope')
  })
})
