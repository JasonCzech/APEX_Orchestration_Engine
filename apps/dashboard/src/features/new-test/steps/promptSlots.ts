import type { PhaseName } from '@apex/pipeline-events'

export type PromptTabId = 'system' | 'application' | 'environment' | `phase:${PhaseName}`

export const tabKey = (id: PromptTabId) => id.replace(':', '-')
/** Focus target other steps use to open one prompt slot (see focusWizardTarget). */
export const promptSlotTarget = (id: PromptTabId) => `prompt-slot-${tabKey(id)}`
