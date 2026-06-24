import type { ArtifactBytes } from '@/features/artifacts/artifactUrl'

import { getDevDataStore } from './controller'

export function getDevArtifactBytes(url: string): ArtifactBytes | null {
  return getDevDataStore()?.getArtifactBytes(url) ?? null
}

