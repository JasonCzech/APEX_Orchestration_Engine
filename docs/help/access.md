# Roles, scopes & API consumers

This administration guide requires an admin session. The server enforces the exact operation and audience policy. Administrative visibility does not imply permission to make every platform-wide change.

## Understand roles and scope

Orchestration uses **viewer**, **operator**, and **admin** roles. Project/application scope narrows access independently of the role. An unscoped admin is the global administrator. An operator without any authorized pipeline scope cannot launch a test merely because the role is operator.

Multi-system runs require access to every participant. A grant on the primary application alone does not authorize all dependency context, artifacts, streams, or actions. Catalog mutations and connection administration have their own audience and policy checks.

Verify a request against current server behavior. Hiding a control is a usability aid; the API remains the authorization boundary. Teach operators the distinction between insufficient role, missing project/application scope, and a failed provider credential.

## Create an API consumer

1. Open [API consumers](/admin/consumers).
2. Use the available creation controls and enter a recognizable name for the person or automation.
3. Choose the least role and project/application scope that supports the intended work.
4. Review the identity and grants before creating it.
5. Handle the one-time key display immediately using the UI's copy/save acknowledgment. Store the key through your organization's credential process.

Only a hash is retained for authentication; the plaintext key cannot be displayed again later. Do not put keys in tickets, source documents, prompt content, or shared browser URLs. Use a separate consumer for automation so its attribution and rotation are independent.

## Rotate or revoke a key

Open the consumer detail and use its rotate control. Capture the replacement key from the one-time display. The old credential stops authenticating when rotation succeeds; there is no grace window. Coordinate client updates and expect affected browser sessions to require the replacement.

Verify the replacement with a read-only system-info request or browser sign-in. A rejected old key should return 401. Replacing a key in Settings validates the browser's credential; it does not rotate a server-side consumer.

Disable a consumer to revoke its access while retaining identity/history where supported. Inspect scope and active usage before changing a shared automation identity. Deleting or disabling an identity can affect scheduled external callers even when no dashboard is open.

## Investigate access failures

- **401**: the credential is absent, invalid, rotated, disabled, or no longer resolvable. Confirm the intended backend and consumer.
- **403**: the identity is known but lacks the required role/scope or policy permission. Check the exact project/application and operation.
- **404/unavailable**: the record may be absent or intentionally unavailable to the caller. Do not infer existence from the response alone.

For a test involving multiple systems, verify all participant grants. For global catalog work, verify global administration rather than broad project access. Recheck an affected session after a grant change.

## Maintain credential infrastructure

The deployment's hash pepper is separate from individual API keys. Rotation of that platform secret requires the supported current/previous-pepper deployment procedure so existing keys can be rehashed on use. Do not replace the pepper as a way to rotate one consumer.

Keep operational credential procedures with deployment owners. See [Deployment & operations](operations.md) for rollout and recovery, and [Authentication & API workflows](api-workflows.md) for safe request examples.
