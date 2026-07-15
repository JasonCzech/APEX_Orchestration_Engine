/**
 * Step 3 — Context (all optional): multipart document upload (file input +
 * drag-drop onto the dashed glass panel) and an existing-documents picker
 * scoped to the project. Selected ids persist in the draft; names/sizes for
 * chips come from upload results + the documents list.
 */
import { useEffect, useRef, useState, type DragEvent } from 'react'

import { useDocumentsList, useUploadDocument, type DocumentOut } from '@/api/hooks/useDocuments'

import {
  ACCEPTED_CONTEXT_ATTR,
  ACCEPTED_CONTEXT_LABEL,
  parseStatusBadge,
  validateContextFile,
} from '../contextFiles'
import type { StepProps } from '../NewRunWizard'

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function StatusBadge({ status }: { status: string | null | undefined }) {
  const badge = parseStatusBadge(status)
  return <span className={`wizard-badge wizard-badge--${badge.tone}`}>{badge.label}</span>
}

export function ContextStep({ draft, onChange }: StepProps) {
  const upload = useUploadDocument()
  const documents = useDocumentsList(
    draft.scope.project_id.trim() || undefined,
    undefined,
    draft.scope.app_id,
  )
  const inputRef = useRef<HTMLInputElement | null>(null)
  // Documents uploaded in this session (the list query may lag behind).
  const [uploaded, setUploaded] = useState<DocumentOut[]>([])
  const [dragOver, setDragOver] = useState(false)
  const [uploadErrors, setUploadErrors] = useState<string[]>([])
  const projectRef = useRef(draft.scope.project_id.trim())
  const appRef = useRef(draft.scope.app_id)
  const generationRef = useRef(0)
  useEffect(() => {
    const nextProject = draft.scope.project_id.trim()
    const nextApp = draft.scope.app_id
    if (nextProject !== projectRef.current || nextApp !== appRef.current) {
      projectRef.current = nextProject
      appRef.current = nextApp
      generationRef.current += 1
      setUploaded([])
      setUploadErrors([])
    }
  }, [draft.scope.project_id, draft.scope.app_id])

  const known = new Map<string, DocumentOut>()
  for (const doc of documents.data ?? []) known.set(doc.id, doc)
  for (const doc of uploaded) known.set(doc.id, doc)

  function addDocument(id: string) {
    onChange((prev) =>
      prev.document_ids.includes(id)
        ? prev
        : { ...prev, document_ids: [...prev.document_ids, id] },
    )
  }

  function removeDocument(id: string) {
    onChange((prev) => ({
      ...prev,
      document_ids: prev.document_ids.filter((existing) => existing !== id),
    }))
  }

  async function uploadFiles(files: FileList | File[]) {
    const generation = generationRef.current
    const project = projectRef.current || undefined
    const appId = appRef.current || undefined
    setUploadErrors([])
    const errors: string[] = []
    for (const file of Array.from(files)) {
      const invalid = validateContextFile(file)
      if (invalid) {
        errors.push(invalid)
        continue
      }
      try {
        const doc = await upload.mutateAsync({
          file,
          projectId: project,
          appId,
        })
        if (
          generation === generationRef.current &&
          project === (projectRef.current || undefined) &&
          appId === (appRef.current || undefined)
        ) {
          setUploaded((prev) => [...prev, doc])
          addDocument(doc.id)
        }
      } catch (error) {
        if (
          generation === generationRef.current &&
          project === (projectRef.current || undefined) &&
          appId === (appRef.current || undefined)
        ) {
          errors.push(error instanceof Error ? error.message : `Upload of ${file.name} failed`)
        }
      }
    }
    if (generation === generationRef.current && errors.length > 0) setUploadErrors(errors)
  }

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault()
    setDragOver(false)
    if (event.dataTransfer.files.length > 0) void uploadFiles(event.dataTransfer.files)
  }

  const pickerDocs = (documents.data ?? []).filter((doc) => !draft.document_ids.includes(doc.id))

  return (
    <section className="wizard-step" aria-label="Context">
      <p className="wizard-step-hint">
        Attach specs, runbooks or prior reports as context — or skip; documents are optional.
      </p>

      <div
        className={`glass-panel wizard-dropzone${dragOver ? ' wizard-dropzone--active' : ''}`}
        data-testid="document-dropzone"
        onDragOver={(event) => {
          event.preventDefault()
          setDragOver(true)
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={onDrop}
      >
        <p className="wizard-dropzone-title">Drop files here</p>
        <p className="wizard-caption">or</p>
        <button
          type="button"
          className="btn btn-secondary"
          onClick={() => inputRef.current?.click()}
        >
          Choose files
        </button>
        <input
          ref={inputRef}
          type="file"
          multiple
          accept={ACCEPTED_CONTEXT_ATTR}
          aria-label="Upload documents"
          className="wizard-file-input"
          onChange={(event) => {
            if (event.target.files && event.target.files.length > 0) {
              void uploadFiles(event.target.files)
              event.target.value = ''
            }
          }}
        />
        <p className="wizard-caption">Accepted: {ACCEPTED_CONTEXT_LABEL}</p>
        {upload.isPending && <p className="wizard-caption">Uploading…</p>}
        {uploadErrors.length > 0 && (
          <ul className="wizard-upload-errors" role="alert" data-testid="upload-errors">
            {uploadErrors.map((message) => (
              <li key={message} className="wizard-caption wizard-caption--danger">
                {message}
              </li>
            ))}
          </ul>
        )}
      </div>

      {draft.document_ids.length > 0 && (
        <div className="wizard-field">
          <span className="wizard-label">Attached ({draft.document_ids.length})</span>
          <ul className="wizard-attached-list" data-testid="attached-documents">
            {draft.document_ids.map((id) => {
              const doc = known.get(id)
              return (
                <li key={id} className="wizard-attached">
                  <div className="wizard-attached-head">
                    <span className="wizard-attached-name">
                      {doc ? `${doc.name} · ${formatBytes(doc.size_bytes)}` : id}
                    </span>
                    {doc?.parse_status && <StatusBadge status={doc.parse_status} />}
                    <button
                      type="button"
                      className="wizard-chip-remove"
                      aria-label={`Remove ${doc?.name ?? id}`}
                      onClick={() => removeDocument(id)}
                    >
                      ×
                    </button>
                  </div>
                  {doc?.parse_status === 'parsed' && typeof doc.extracted_chars === 'number' && (
                    <p className="wizard-caption">
                      {doc.extracted_chars.toLocaleString()} characters extracted for context
                    </p>
                  )}
                  {doc?.parse_status === 'failed' && (
                    <p className="wizard-caption wizard-caption--danger">
                      Couldn’t read this file{doc.parse_error ? `: ${doc.parse_error}` : ''}. It
                      won’t be used as context.
                    </p>
                  )}
                  {doc?.parse_status === 'unsupported' && (
                    <p className="wizard-caption wizard-caption--warning">
                      This file type can’t be read for context; it stays attached as a reference
                      only.
                    </p>
                  )}
                  {doc?.text_preview && (
                    <details className="wizard-preview">
                      <summary className="wizard-preview-summary">Preview extracted text</summary>
                      <pre className="wizard-preview-body">{doc.text_preview}</pre>
                    </details>
                  )}
                </li>
              )
            })}
          </ul>
        </div>
      )}

      <div className="wizard-field">
        <span className="wizard-label">Existing documents</span>
        {documents.isError ? (
          <p className="wizard-caption wizard-caption--danger">Documents failed to load</p>
        ) : pickerDocs.length === 0 ? (
          <p className="wizard-caption">
            {documents.isLoading ? 'Loading…' : 'No other documents in this project.'}
          </p>
        ) : (
          <ul className="wizard-doc-list">
            {pickerDocs.map((doc) => (
              <li key={doc.id} className="wizard-doc-row">
                <span className="wizard-doc-name">{doc.name}</span>
                <span className="wizard-caption">
                  {doc.media_type} · {formatBytes(doc.size_bytes)}
                </span>
                {doc.parse_status && <StatusBadge status={doc.parse_status} />}
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => addDocument(doc.id)}
                >
                  Add
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  )
}
