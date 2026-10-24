import { createBrowserRouter, type RouteObject } from 'react-router'

import { AppShell } from '@/components/layout/AppShell'

import type { RouteHandle } from './handle'
import { RouteErrorBoundary } from './RouteErrorBoundary'
import { LegacyPresetRedirect } from './LegacyPresetRedirect'

type PageModule<K extends string> = Record<K, React.ComponentType>

/** Lazy-load one named export from a feature pages module as the route Component. */
function lazyPage<K extends string>(
  loader: () => Promise<PageModule<K>>,
  name: K,
): NonNullable<RouteObject['lazy']> {
  return async () => {
    const module = await loader()
    return { Component: module[name] }
  }
}

function handle(title: string): RouteHandle {
  return { title }
}

const runs = () => import('@/features/runs/pages')
const approvals = () => import('@/features/approvals/pages')
const prompts = () => import('@/features/prompts/pages')
const workItems = () => import('@/features/work-items/pages')
const environments = () => import('@/features/environments/pages')
const goldenConfigs = () => import('@/features/golden-configs/pages')
const admin = () => import('@/features/admin/pages')
const apexLoad = () => import('@/features/apex-load/pages')

/** Shared route tree for the browser and integration tests. */
export const appRoutes: RouteObject[] = [
  {
    path: '/',
    element: <AppShell />,
    errorElement: <RouteErrorBoundary />,
    // Initial matches lazy-load their placeholder modules; render nothing
    // rather than warning until the first chunk resolves.
    hydrateFallbackElement: <></>,
    children: [
      {
        errorElement: <RouteErrorBoundary />,
        children: [
          { path: 'help', handle: handle('Help & documentation'), lazy: lazyPage(() => import('@/features/help/pages'), 'HelpPage') },
          { path: 'help/:articleId', handle: handle('Help & documentation'), lazy: lazyPage(() => import('@/features/help/pages'), 'HelpPage') },
          {
            index: true,
            handle: handle('Test operations dashboard'),
            lazy: lazyPage(() => import('@/features/home/pages'), 'HomePage'),
          },
          {
            path: 'approvals',
            handle: handle('Tests'),
            lazy: lazyPage(approvals, 'ApprovalsInboxPage'),
          },
          {
            path: 'approvals/:threadId/:interruptId',
            handle: handle('Review'),
            lazy: lazyPage(approvals, 'ApprovalDetailPage'),
          },
          { path: 'cycles', handle: handle('Test cycles'), lazy: lazyPage(() => import('@/features/cycles/pages'), 'CyclesPage') },
          { path: 'cycles/:cycleId', handle: handle('Cycle workspace'), lazy: lazyPage(() => import('@/features/cycles/pages'), 'CyclePage') },
          { path: 'runs', handle: handle('Tests'), lazy: lazyPage(runs, 'RunsListPage') },
          {
            path: 'runs/new',
            handle: handle('New test'),
            lazy: lazyPage(runs, 'NewRunWizardPage'),
          },
          {
            path: 'runs/compare',
            handle: handle('Compare runs'),
            lazy: lazyPage(runs, 'RunsComparePage'),
          },
          {
            path: 'runs/:threadId',
            handle: handle('Run detail'),
            lazy: lazyPage(runs, 'RunDetailPage'),
          },
          {
            path: 'runs/:threadId/reviews/:interruptId',
            handle: handle('Review'),
            lazy: lazyPage(approvals, 'ApprovalDetailPage'),
          },
          {
            path: 'runs/:threadId/phases/:phase',
            handle: handle('Run detail'),
            lazy: lazyPage(runs, 'PhaseDetailPage'),
          },
          {
            path: 'runs/:threadId/timeline',
            handle: handle('Run detail'),
            lazy: lazyPage(runs, 'RunTimelinePage'),
          },
          {
            path: 'runs/:threadId/artifacts/:name',
            handle: handle('Artifact viewer'),
            lazy: lazyPage(runs, 'ArtifactViewerPage'),
          },
          { path: 'prompts', handle: handle('Prompts'), lazy: lazyPage(prompts, 'PromptsPage') },
          {
            path: 'prompts/:ns/:name',
            handle: handle('Prompt'),
            lazy: lazyPage(prompts, 'PromptDetailPage'),
          },
          {
            path: 'prompts/:ns/:name/versions/:v',
            handle: handle('Prompt version'),
            lazy: lazyPage(prompts, 'PromptVersionPage'),
          },
          {
            path: 'prompts/:ns/:name/playground',
            handle: handle('Template preview'),
            lazy: lazyPage(prompts, 'PromptPlaygroundPage'),
          },
          { path: 'golden-configs/*', element: <LegacyPresetRedirect /> },
          {
            path: 'test-presets',
            handle: handle('Test presets'),
            lazy: lazyPage(goldenConfigs, 'GoldenConfigsPage'),
          },
          {
            path: 'test-presets/team/:presetId',
            handle: handle('Test preset'),
            lazy: lazyPage(goldenConfigs, 'TeamPresetDetailPage'),
          },
          {
            path: 'test-presets/:assistantId',
            handle: handle('Test preset'),
            lazy: lazyPage(goldenConfigs, 'GoldenConfigDetailPage'),
          },
          {
            path: 'work-items',
            handle: handle('Tickets'),
            lazy: lazyPage(workItems, 'WorkItemsPage'),
          },
          {
            path: 'work-items/saved',
            handle: handle('Saved queries'),
            lazy: lazyPage(workItems, 'SavedQueriesPage'),
          },
          {
            path: 'work-items/:provider/:itemId',
            handle: handle('Ticket'),
            lazy: lazyPage(workItems, 'WorkItemDetailPage'),
          },
          {
            path: 'environments',
            handle: handle('Applications & environments'),
            lazy: lazyPage(environments, 'EnvironmentsPage'),
          },
          {
            path: 'environments/:id',
            handle: handle('Environment'),
            lazy: lazyPage(environments, 'EnvironmentDetailPage'),
          },
          {
            path: 'context',
            handle: handle('Knowledge & documents'),
            lazy: lazyPage(() => import('@/features/context/pages'), 'ContextPage'),
          },
          {
            path: 'evidence',
            handle: handle('Evidence'),
            lazy: lazyPage(() => import('@/features/context/pages'), 'EvidencePage'),
          },
          {
            path: 'analytics/models',
            handle: handle('Model pricing'),
            lazy: lazyPage(() => import('@/features/analytics/ModelPricingPage'), 'ModelPricingPage'),
          },
          {
            path: 'analytics',
            handle: handle('Agent analytics'),
            lazy: lazyPage(() => import('@/features/analytics/pages'), 'AnalyticsPage'),
          },
          {
            path: 'logs',
            handle: handle('Logs'),
            lazy: lazyPage(() => import('@/features/logs/pages'), 'LogsPage'),
          },
          {
            path: 'apex-load',
            handle: handle('APEX Load'),
            lazy: lazyPage(apexLoad, 'ApexLoadWorkspacePage'),
          },
          {
            path: 'admin/connections',
            handle: handle('Connections'),
            lazy: lazyPage(admin, 'ConnectionsPage'),
          },
          {
            path: 'admin/connections/:id',
            handle: handle('Connection'),
            lazy: lazyPage(admin, 'ConnectionDetailPage'),
          },
          {
            path: 'admin/consumers',
            handle: handle('API consumers'),
            lazy: lazyPage(admin, 'ConsumersPage'),
          },
          {
            path: 'admin/consumers/:id',
            handle: handle('Consumer'),
            lazy: lazyPage(admin, 'ConsumerDetailPage'),
          },
          {
            path: 'admin/system',
            handle: handle('System status'),
            lazy: lazyPage(admin, 'AdminSystemPage'),
          },
          {
            path: 'settings',
            handle: handle('Settings'),
            lazy: lazyPage(() => import('@/features/settings/pages'), 'SettingsPage'),
          },
          {
            path: '*',
            handle: handle('Not found'),
            lazy: lazyPage(() => import('@/features/not-found/pages'), 'NotFoundPage'),
          },
        ],
      },
    ],
  },
]

export function createAppRouter() {
  return createBrowserRouter(appRoutes)
}
