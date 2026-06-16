import type { SystemInfo } from '@/api/apexClient'

const DEFAULT_DEV_API_KEY = 'dev-key-local'

export function isDevAuthEnabled(): boolean {
  return import.meta.env.DEV && import.meta.env.VITE_APEX_DEV_AUTH === 'true'
}

export function getDevApiKey(): string {
  return import.meta.env.VITE_APEX_DEV_API_KEY?.trim() || DEFAULT_DEV_API_KEY
}

export function getDevSystemInfo(): SystemInfo {
  return {
    name: 'APEX Orchestration Engine',
    version: 'dev',
    environment: 'development',
    features: { engines: true, documents: true },
    consumer: {
      name: 'Dev Admin',
      role: 'admin',
      scopes: [],
    },
  }
}
