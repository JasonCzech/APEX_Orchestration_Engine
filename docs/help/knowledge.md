# Knowledge & documents

[Knowledge & documents](/context) manages source material used to inform agent work. Sources are inputs. [Evidence](/evidence) records outputs and execution-related material. A document being readable does not establish that its statements are verified measurements.

You need read access to the source audience to browse it, and mutation permission to add or change scoped content. Document processing and retention depend on the deployment's configured services and storage.

## Prepare useful source material

Choose architecture notes, requirements, test data descriptions, environment guidance, and existing investigations that directly support the test objective. Use stable titles and identify the source revision or observation date. Keep credentials in connection-managed storage, outside uploads and prompt text.

For quantitative requirements, include the signal, unit, percentile or aggregation, window, comparator, target, and evidence source. Mark unknown information explicitly rather than filling it with plausible example numbers. Queue requirements also need depth/age limits and a recovery window.

## Add and select knowledge

1. Open Knowledge & documents and use the available creation/upload controls for the intended scope.
2. Follow file type and size limits shown by the form. Inspect any processing or summarization state before relying on the document.
3. Open the source or summary to check readability, content, ownership, and provenance.
4. In [New test → Knowledge](/runs/new?step=knowledge), choose the relevant documents and supported context summaries.
5. In Review, confirm that selected material still applies to the complete multi-system scope.

Sources from participating projects/applications remain individually authorized and identified. Changing scope can invalidate selections. If a required source is pending, failed, or unavailable, resolve it before launch rather than assuming its contents were included.

## Distinguish source text and summaries

A summary is a representation of source material, not the original source or proof of correctness. Inspect the underlying material when a decision depends on an exact requirement or result. Selected material is resolved into context through the launch workflow and is subject to configured count and size limits.

An input limit failure needs a smaller, more relevant selection or a supported immutable reference. Do not assume required evidence will be silently truncated. Record gaps in the request or deliverable so reviewers know what remains unresolved.

## Use cycle sources

Where cycles and a Confluence connection are enabled, the cycle scope workspace can browse a configured space, preview readable revisioned page text, and capture an exact source revision. Inspect title, revision, checksum, and selection before capture. Unsupported macros or attachments can remain explicit blockers. Capture does not imply that publication back to Confluence is supported.

Follow [Manage test cycles](cycles.md) for source freshness and dependency review. For inaccessible sources, first verify project/application scope and provider configuration. For processing failures, retain the document ID and error and follow [Troubleshooting](troubleshooting.md).
