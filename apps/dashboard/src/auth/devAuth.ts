import type { SystemInfo } from '@/api/apexClient'
import { isDemoDataMode } from '@/config/runtimeConfig'

const DEFAULT_DEV_API_KEY = 'dev-key-local'

export function isDevAuthEnabled(): boolean {
  return import.meta.env.DEV && import.meta.env.VITE_APEX_DEV_AUTH === 'true'
}

export function getDevApiKey(): string {
  return import.meta.env.VITE_APEX_DEV_API_KEY?.trim() || DEFAULT_DEV_API_KEY
}

export function getDevSystemInfo(): SystemInfo {
  if (isDemoDataMode()) {
    return {
      name: 'APEX Orchestration Engine',
      version: 'acc-add-line-demo-2.0',
      environment: 'offline-demo',
      features: { engines: true, documents: true, dummy_data: true },
      limits: { max_test_participants: 32, max_context_packets: 32 },
      consumer: {
        name: 'Demo Presenter',
        role: 'admin',
        scopes: [{ project_id: 'telecom-modernization', app_id: null }],
      },
    }
  }
  return {
    name: 'APEX Orchestration Engine',
    version: 'dev',
    environment: 'development',
    features: { engines: true, documents: true },
    limits: { max_test_participants: 32, max_context_packets: 32 },
    consumer: {
      name: 'Dev Admin',
      role: 'admin',
      scopes: [],
    },
  }
}

export function isBypassAuthEnabled(): boolean {
  return isDemoDataMode() || isDevAuthEnabled()
}
