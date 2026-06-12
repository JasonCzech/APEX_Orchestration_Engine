import { FeaturePlaceholder } from '@/components/FeaturePlaceholder'

export function PromptsPage() {
  return <FeaturePlaceholder title="Prompts" route="/prompts" />
}

export function PromptDetailPage() {
  return <FeaturePlaceholder title="Prompt" route="/prompts/:ns/:name" />
}

export function PromptVersionPage() {
  return <FeaturePlaceholder title="Prompt Version" route="/prompts/:ns/:name/versions/:v" />
}

export function PromptPlaygroundPage() {
  return <FeaturePlaceholder title="Prompt Playground" route="/prompts/:ns/:name/playground" />
}
