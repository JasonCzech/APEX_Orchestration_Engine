/**
 * Scripted stream views for the D2 live-UI tests, typed to the local
 * structural contract (liveTypes.ts) — tests never import the streaming
 * module's internals; RunDetail-level tests mock
 * '@/streaming/usePipelineStream' with these shapes instead.
 */
import type { LiveEngineSample, LiveStreamViewLike, LiveToolCall } from '../liveTypes'

export function streamView(overrides: Partial<LiveStreamViewLike> = {}): LiveStreamViewLike {
  return {
    status: 'live',
    phaseProgress: {},
    toolCalls: [],
    engineStats: { samples: [], latest: null },
    pendingGateHint: null,
    ...overrides,
  }
}

export function idleStreamView(): LiveStreamViewLike {
  return streamView({ status: 'idle' })
}

export function toolCall(
  id: string,
  tool: string,
  status: 'ok' | 'error' = 'ok',
  phase = 'execution',
  at?: string,
): LiveToolCall {
  return { id, phase, tool, status, ...(at ? { at } : {}) }
}

export function engineSamples(count: number, startTick = 0): LiveEngineSample[] {
  return Array.from({ length: count }, (_, index) => {
    const tick = startTick + index
    return {
      at: new Date(Date.UTC(2026, 5, 1, 10, 5, tick)).toISOString(),
      status: 'running',
      progress_pct: Math.min(100, tick),
      live_stats: {
        vusers: 50,
        tps: 30 + tick * 0.5,
        error_rate: 0.0042,
        p95_ms: 210 + tick,
      },
    }
  })
}
