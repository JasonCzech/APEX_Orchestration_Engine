/* eslint-disable react-refresh/only-export-components */
import type { ReactNode } from 'react'

import type { Client } from '@langchain/langgraph-sdk'

import type { ArtifactBytes } from '@/features/artifacts/artifactUrl'

export const DEV_DATA_STORAGE_KEY = 'apex.devData.enabled'

export interface DevDataModeContextValue {
  available: boolean
  enabled: boolean
  setEnabled: (enabled: boolean) => void
  reset: () => void
}

export type DevArtifactBytes = ArtifactBytes
export type DevDataStore = never

const noop = (): void => {}
const disabled: DevDataModeContextValue = {
  available: false,
  enabled: false,
  setEnabled: noop,
  reset: noop,
}

export function useDevDataMode(): DevDataModeContextValue {
  return disabled
}

export function DevDataProvider({ children }: { children: ReactNode }) {
  return children
}

export async function handleDevApexRequest(_request: Request): Promise<Response | null> {
  void _request
  return null
}

export function getDevApexFetch(): undefined {
  return undefined
}

export function getDevArtifactBytes(_url: string): ArtifactBytes | null {
  void _url
  return null
}

export function isDevDataAvailable(): boolean {
  return false
}

export function isDevDataEnabled(): boolean {
  return false
}

export function resetDevDataStore(): void {}

export function setDevDataEnabled(_enabled: boolean): void {
  void _enabled
}

export function subscribeDevDataMode(_listener: () => void): () => void {
  void _listener
  return noop
}

export function createDevLangGraphClient(): Pick<Client, 'assistants' | 'threads' | 'runs'> | null {
  return null
}

export function createDevDataStore(): never {
  throw new Error('dev-data is not available in production builds')
}
