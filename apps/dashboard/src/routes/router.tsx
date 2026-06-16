import { createBrowserRouter, type RouteObject } from 'react-router'

import { AppShell } from '@/components/layout/AppShell'

import type { RouteHandle } from './handle'
import { RouteErrorBoundary } from './RouteErrorBoundary'

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

/**
 * The plan's reconciled route table (Part 2 — Information architecture).
 * Every screen lazy-loads a D0 placeholder; real screens land in D1+.
 * Exported so tests can mount the same tree on a memory router.
 */
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
          {
            index: true,
            handle: handle('Pipeline Operation Dashboard'),
            lazy: lazyPage(() => import('@/features/home/pages'), 'HomePage'),
          },
          {
            path: 'approvals',
            handle: handle('Approvals'),
            lazy: lazyPage(approvals, 'ApprovalsInboxPage'),
          },
          {
            path: 'approvals/:threadId/:interruptId',
            handle: handle('Approval'),
            lazy: lazyPage(approvals, 'ApprovalDetailPage'),
          },
          { path: 'runs', handle: handle('Test History'), lazy: lazyPage(runs, 'RunsListPage') },
          {
            path: 'runs/new',
            handle: handle('New Test'),
            lazy: lazyPage(runs, 'NewRunWizardPage'),
          },
          {
            path: 'runs/compare',
            handle: handle('Compare Runs'),
            lazy: lazyPage(runs, 'RunsComparePage'),
          },
          {
            path: 'runs/:threadId',
            handle: handle('Run Detail'),
            lazy: lazyPage(runs, 'RunDetailPage'),
          },
          {
            path: 'runs/:threadId/phases/:phase',
            handle: handle('Phase Detail'),
            lazy: lazyPage(runs, 'PhaseDetailPage'),
          },
          {
            path: 'runs/:threadId/timeline',
            handle: handle('Run Timeline'),
            lazy: lazyPage(runs, 'RunTimelinePage'),
          },
          {
            path: 'runs/:threadId/artifacts/:name',
            handle: handle('Artifact Viewer'),
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
            handle: handle('Prompt Version'),
            lazy: lazyPage(prompts, 'PromptVersionPage'),
          },
          {
            path: 'prompts/:ns/:name/playground',
            handle: handle('Prompt Playground'),
            lazy: lazyPage(prompts, 'PromptPlaygroundPage'),
          },
          {
            path: 'golden-configs',
            handle: handle('Golden Configs'),
            lazy: lazyPage(goldenConfigs, 'GoldenConfigsPage'),
          },
          {
            path: 'golden-configs/:assistantId',
            handle: handle('Golden Config'),
            lazy: lazyPage(goldenConfigs, 'GoldenConfigDetailPage'),
          },
          {
            path: 'work-items',
            handle: handle('Tickets/Defects'),
            lazy: lazyPage(workItems, 'WorkItemsPage'),
          },
          {
            path: 'work-items/saved',
            handle: handle('Saved Queries'),
            lazy: lazyPage(workItems, 'SavedQueriesPage'),
          },
          {
            path: 'work-items/:provider/:itemId',
            handle: handle('Work Item'),
            lazy: lazyPage(workItems, 'WorkItemDetailPage'),
          },
          {
            path: 'environments',
            handle: handle('Environment Configurations'),
            lazy: lazyPage(environments, 'EnvironmentsPage'),
          },
          {
            path: 'environments/:id',
            handle: handle('Environment'),
            lazy: lazyPage(environments, 'EnvironmentDetailPage'),
          },
          {
            path: 'context',
            handle: handle('Context'),
            lazy: lazyPage(() => import('@/features/context/pages'), 'ContextPage'),
          },
          {
            path: 'analytics',
            handle: handle('Analytics'),
            lazy: lazyPage(() => import('@/features/analytics/pages'), 'AnalyticsPage'),
          },
          {
            path: 'logs',
            handle: handle('Logs'),
            lazy: lazyPage(() => import('@/features/logs/pages'), 'LogsPage'),
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
            handle: handle('Consumers'),
            lazy: lazyPage(admin, 'ConsumersPage'),
          },
          {
            path: 'admin/consumers/:id',
            handle: handle('Consumer'),
            lazy: lazyPage(admin, 'ConsumerDetailPage'),
          },
          {
            path: 'admin/system',
            handle: handle('System'),
            lazy: lazyPage(admin, 'AdminSystemPage'),
          },
          {
            path: 'settings',
            handle: handle('Settings'),
            lazy: lazyPage(() => import('@/features/settings/pages'), 'SettingsPage'),
          },
          {
            path: '*',
            handle: handle('Not Found'),
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
