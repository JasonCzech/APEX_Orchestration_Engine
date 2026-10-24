# Preferences & session

Open [Settings](/settings) to configure personal display preferences, inspect runtime connection information, and manage the browser's current API key. Preferences apply immediately to this browser origin. They are not shared automatically with another host, browser, or APEX Load session.

## Appearance and readability

Choose System (Auto) or a supported theme: Light, Dark, Solarized Dark, Solarized Light, or Monokai Dimmed. System follows the device's preferred appearance. Theme changes preserve mounted editors and drafts.

Use the available display controls for reduced motion/device behavior, table row defaults, evidence-editor wrapping, and local versus UTC timestamps. The Tests library uses the personal row default; specialized tables can retain their own supported limits.

Changing timestamp preference changes presentation, not recorded event times. Include the selected timezone when reporting an incident. Use browser zoom when needed; navigation adapts to compact layouts.

## Replace the browser API key

In a connected session, the key is masked. Enter a replacement in Settings and use the validation action. The candidate is checked against the configured backend before the current key is replaced. Wait for explicit success; a failed validation should leave the current session usable.

A key identifies the authenticated consumer and its scopes. Replacing the browser key is different from rotating the server-side credential through API consumers. For server-side rotation, see [Roles, scopes & API consumers](access.md#rotate-or-revoke-a-key).

Use Sign out to clear the active browser authentication and session data managed by the app. Do not put keys in shared URLs, screenshots, articles, or prompts. A one-time key display must be handled through its explicit save/acknowledgment controls before leaving.

## Inspect connections and version

Settings shows runtime API origins, system/version/environment information, and the current consumer where available. Origins come from deployment configuration. A personal preference cannot change a deployed API host.

The connection indicator shows platform reachability. A degraded or unreachable state calls for checking backend health and proxy configuration. It does not establish that a particular execution/analysis provider is healthy. See [Troubleshooting](troubleshooting.md#connection-or-stream-is-lost).

## Offline demo session

In the offline demo, Settings shows a simulated session instead of API-key replacement. **Reset demo** restores canonical demo data and clears presenter changes. It does not reset an external system because the demo makes no integration calls.

Do not confuse Reset demo with connected-run cleanup. In a connected deployment, abort, provider cleanup, archive, and retention are separate operations with their own confirmation and permissions.
