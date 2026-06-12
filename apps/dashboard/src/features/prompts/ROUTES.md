# Prompt catalog — route wiring (D5, prompts agent)

No `src/routes/router.tsx` changes: the router already lazy-loads the four
export names below from `@/features/prompts/pages`; `pages.tsx` now re-exports
the real screens instead of the D0 placeholders.

| Export | File | Route |
|---|---|---|
| `PromptsPage` | `PromptsPage.tsx` | `/prompts?ns=&q=&archived=1` |
| `PromptDetailPage` | `PromptDetailPage.tsx` | `/prompts/:ns/:name?tab=content\|versions` |
| `PromptVersionPage` | `PromptVersionPage.tsx` | `/prompts/:ns/:name/versions/:v?diff=<other_version_id>` (`:v` is the **version id**, not the number) |
| `PromptPlaygroundPage` | `PromptPlaygroundPage.tsx` | `/prompts/:ns/:name/playground` |

## Slash-key URL encoding (the `:name` decision)

Prompt keys contain slashes (`story_analysis/system` in namespace `phase`) and
`router.tsx`'s pattern is the non-splat `/prompts/:ns/:name`. Names with raw
slashes would therefore break matching, so the **full key is
percent-encoded into the single `:name` segment with `encodeURIComponent`**
(`/prompts/phase/story_analysis%2Fsystem`). React Router keeps `%2F` inside
one segment when matching and decodes params exactly once, so readers take
`useParams().name` as-is and must NOT decode again (a key containing a
literal `%` would double-decode/throw).

All links must be built through `promptPaths.ts` (`promptPath`,
`promptVersionPath`, `promptPlaygroundPath`); params are read through
`usePromptRouteParams()`. Round-trip covered by tests in
`__tests__/PromptsPage.test.tsx`.

## id vs (namespace, key)

The REST surface addresses prompts by catalog id; the routes address them by
`(ns, key)`. `usePrompt(ns, key)` (in `src/api/hooks/usePrompts.ts`) bridges
with a namespace-scoped list lookup followed by `GET /v1/prompts/{id}`, cached
on `queryKeys.prompts.detail(ns, key)`. Version/versions/mutation hooks take
the resolved id and stay disabled until the detail lands.

## queryKeys (append-only)

Added `queryKeys.prompts.listWith(filters)` (`['prompts','list','filtered',
{...}]`) for the browser list — disjoint from D4's `listNamespace` (`{ns}`
object element at index 2) and from `detail`'s `[prompts, ns, name]` shape.

## Dependencies

`@codemirror/merge` added (workspace install from the repo root). It resolved
against the existing `@codemirror/view` 6.43 tree with no duplicate instances;
the diff is the `unifiedMergeView` extension mounted on the existing
`@uiw/react-codemirror` surface (`PromptDiff.tsx`), themed via `prompts.css`.
The hand-rolled LCS fallback was NOT needed; `lineDiff.ts` only powers the
"+a −d lines vs active" indicator in new-version mode.

## Role gating

Mutating affordances ([New prompt], [New version], [Archive|Unarchive],
[Set active], playground [Run test]) hide below `operator` via `RequireRole` /
`useConsumer`; the server enforces regardless (`src/apex/routers/prompts.py`).

## Follow-ups (noted, not shipped)

- Live playground streaming: the 202 card links to `/runs/{thread_id}`;
  no polling/streaming on the playground page itself.
