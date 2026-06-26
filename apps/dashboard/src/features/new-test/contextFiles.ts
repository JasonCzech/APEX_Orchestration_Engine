/**
 * Client-side rules for Story Analysis context uploads. The accepted extension
 * set mirrors the backend extractor (apex.services.text_extraction) so the
 * dashboard rejects unreadable files before the round-trip, and the parse-status
 * badge mapping turns the server's parse_status into friendly UI copy.
 */
import type { DocumentOut } from '@/api/hooks/useDocuments'

/** Extensions the backend can extract text from (dispatch is extension-first). */
export const ACCEPTED_CONTEXT_EXTENSIONS = ['.pdf', '.docx', '.md', '.markdown', '.txt'] as const

/** Value for an <input type="file"> accept attribute. */
export const ACCEPTED_CONTEXT_ATTR = ACCEPTED_CONTEXT_EXTENSIONS.join(',')

/** Human-friendly description for hints and validation copy. */
export const ACCEPTED_CONTEXT_LABEL = 'PDF, Word (.docx), Markdown (.md) or text (.txt)'

export function isAcceptedContextFile(name: string): boolean {
  const lower = name.toLowerCase()
  return ACCEPTED_CONTEXT_EXTENSIONS.some((ext) => lower.endsWith(ext))
}

/** Returns a friendly error message for an unacceptable file, or null when OK. */
export function validateContextFile(file: File): string | null {
  if (!isAcceptedContextFile(file.name)) {
    return `${file.name}: unsupported type — attach ${ACCEPTED_CONTEXT_LABEL}.`
  }
  return null
}

export type ParseTone = 'success' | 'warning' | 'danger' | 'muted'

export interface ParseStatusBadge {
  label: string
  tone: ParseTone
  /** Whether the extracted text will be injected into the run's context. */
  included: boolean
}

/** Map a document's parse_status to badge copy/tone for the wizard. */
export function parseStatusBadge(status: string | null | undefined): ParseStatusBadge {
  switch (status) {
    case 'parsed':
      return { label: 'Parsed', tone: 'success', included: true }
    case 'failed':
      return { label: 'Parse failed', tone: 'danger', included: false }
    case 'unsupported':
      return { label: 'Unsupported', tone: 'warning', included: false }
    default:
      return { label: 'Pending', tone: 'muted', included: false }
  }
}

export interface ContextSummary {
  /** Documents whose extracted text will be sent as context. */
  includedCount: number
  /** Total characters of extracted text across included documents. */
  totalChars: number
  /** Documents that failed to parse or are an unsupported type. */
  unreadableCount: number
}

/** Summarise the parse outcome across a set of resolved documents. */
export function summarizeContext(docs: (DocumentOut | undefined)[]): ContextSummary {
  let includedCount = 0
  let totalChars = 0
  let unreadableCount = 0
  for (const doc of docs) {
    if (!doc) continue
    if (doc.parse_status === 'parsed') {
      includedCount += 1
      totalChars += doc.extracted_chars ?? 0
    } else if (doc.parse_status === 'failed' || doc.parse_status === 'unsupported') {
      unreadableCount += 1
    }
  }
  return { includedCount, totalChars, unreadableCount }
}
