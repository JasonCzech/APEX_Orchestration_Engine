# Tickets & saved queries

Use [Tickets](/work-items) to find source requirements and tracker items. Use the **Tickets** wizard step to select items that inform a particular test. You need access to the source project and its configured work-tracking connection. Creating items and saving queries additionally require mutation permission for that scope.

## Find requirements

1. Choose the relevant project/connection context shown by the screen.
2. Use **Find by description** to describe the items you need, then **Preview ticket query**.
3. Inspect the provider and translated query. A suggested query is a draft; check project filters, item types, state conditions, and scope before running it.
4. Edit the provider query if necessary, or use manual query mode.
5. Choose **Run query**, inspect results, and open items to verify their description and source identity.

The query language follows the provider. Jira Align expressions and Jira Cloud JQL are different dialects. Do not paste one into the other without translating it. Provider errors remain errors; an empty result does not establish that requirements are absent from the source system.

## Select tickets for a test

In the wizard's Tickets step, select the specific returned items that belong to the test objective. Review their IDs and provider. The workflow retains its single work-tracking connection boundary; do not assume selections from unrelated connections can be combined.

At launch, selected source items are resolved into context for the test. Changing scope ownership can invalidate project-bound selections. Return to Tickets and confirm them after a scope change. A successful query preview does not itself select items or launch a test.

## Save and reuse a query

With appropriate permission, save the current query with a useful name and description. Open [Saved queries](/work-items/saved) to inspect or run it again. Running a saved query uses the source's current data; it does not preserve the previous result set.

Query-console links can contain provider/query parameters so the same query can be reopened. Check them before sharing, and avoid placing sensitive business data in a URL. A shared link still requires the recipient's own authorization.

## Work with defects

Jira Align is the supported requirements-import/evaluation path. Jira Cloud remains available for defects. Use the create-item controls available in your scope, review the intended destination and content, and retain the resulting provider identity.

After an uncertain create response, check the saved operation state and the tracker before retrying. Repeatedly clicking Create can produce ambiguous outcomes with external systems. Existing ADO records are historical; Azure DevOps is retired for new provider calls.

If no provider binding is configured, ask an administrator to inspect [Connections](connections.md). If access is refused, distinguish provider credentials from your Orchestration role/scope. Record the exact error and connection identity for [support](support.md#collect-a-support-request).
