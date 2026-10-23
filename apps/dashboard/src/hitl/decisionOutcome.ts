import type { GateInterrupt } from '@/api/hooks/useThreadState'
import type { GateAction } from './gateMachine'

/** Past-tense receipt wording for an accepted gate decision. */
export function decisionOutcomeLabel(action: GateAction | 'superseded', kind?: GateInterrupt['kind']): string {
  switch (action) {
    case 'approve': return kind === 'prompt_review'
      ? 'Approved prompts and started the phase'
      : 'Accepted phase output and continued the run'
    case 'skip_phase': return 'Skipped the phase and continued the run'
    case 'abort': return 'Aborted the run'
    case 'revise': return 'Requested changes; the phase is running again'
    case 'complete': return 'Completed the phase and continued the run'
    case 'superseded': return 'Decision was completed elsewhere'
    default: return 'Decision recorded'
  }
}
