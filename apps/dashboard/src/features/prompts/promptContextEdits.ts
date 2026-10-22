export interface ContextPrompt {
  namespace: string
  key: string
  prompt_id: string
  version: number
  content: string
}

export interface ContextEdit {
  prompt_id: string
  content: string
}

/** Carry entry content structurally: prompt prose may contain any section heading. */
export function promptContextEdits(contexts: readonly ContextPrompt[]): ContextEdit[] {
  return contexts.map(({ prompt_id, content }) => ({ prompt_id, content }))
}
