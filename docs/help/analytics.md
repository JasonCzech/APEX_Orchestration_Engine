# Analytics, pricing & logs

[Agent analytics](/analytics) describes recorded model usage. [Model pricing](/analytics/models) manages or displays pricing information used for estimates. [Logs](/logs) supports operational investigation. Access is filtered by the current consumer's authorized scope; some management actions require administrative capability.

## Investigate agent usage

Choose the available timeframe and project/model filters. Inspect totals alongside the underlying phase/run contributions and follow accessible run links for evidence. Shorter time windows should be interpreted as subsets of the same recorded history, not independently generated populations.

Token counts and estimated costs describe recorded model calls, including retries where recorded. They do not measure load-engine capacity, infrastructure cost, or every downstream provider charge. A failure may still have incurred model usage.

Demo analytics uses fixed illustrative history and pricing. It makes no model calls and is not an invoice. Connected usage coverage depends on what the backend captured; missing usage is not necessarily zero consumption.

## Model pricing

Inspect model identity, provider, effective pricing entries, units, and history before using a cost estimate. Pricing is input to estimates, not a guarantee of the provider's final bill. Historical usage can reference archived models or historical prices.

Where authorized, maintain prices through the supported management workflow and review its effective period. A pricing update does not change the model already recorded on a historical run. An unavailable price should remain unknown rather than implying a free call.

Model availability, credentials, and admission/readiness are separate from a catalog price entry. A listed model is not proof the configured provider will accept the next request.

## Logs

Use filters to narrow the timeframe, scope, run, phase, or event types supported by the screen. Inspect the full run identity and exact time before correlating an event with a provider incident. Follow run links to the durable workflow state.

An empty result can reflect filters, retention, current-page boundaries, or lack of access. Clear filters and check the requested window before concluding that no event occurred. A failed query is different from an empty result.

Personal timestamp preferences affect absolute-time display. Record the timezone when sharing an incident window. Logs support diagnosis; required deliverable and measurement artifacts remain the evidence for acceptance decisions.

## Escalate a discrepancy

Keep filter values, run/phase IDs, model/provider identity, timestamp/timezone, and the exact observed difference. Compare the full evidence and recorded status rather than relying on a graph alone. Use [Support](support.md#collect-a-support-request) for a reproducible report.
