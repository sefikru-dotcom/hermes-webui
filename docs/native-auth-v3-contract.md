# Native auth v3 vertical slice

This slice is deliberately limited to pairing and device credentials. Native chat is
not enabled by this change; legacy browser routes remain cookie/CSRF based.

## Contract

1. `POST /api/v1/auth/device-pairings` creates a short-lived pairing and returns the
   display `code`, `pair_id`, and the poll secret to the native client.
2. `GET /api/v1/auth/device-pairings/status?pair_id=...` returns only `pending`,
   `approved`, or `claimed` metadata. It never accepts `poll_secret` in a query
   string or header and never creates/returns a credential.
3. `POST /api/v1/auth/device-pairings/{pair_id}/exchange` accepts exactly
   `{"poll_secret":"..."}`. An approved pairing is atomically consumed once and
   returns `access_token`, `token_type` (`Bearer`), `expires_in`, `device_id`, and
   `scopes`. Refresh tokens are intentionally BLOCKED in this vertical slice.
4. Pairings and credential hashes are persisted in the configured WebUI state
   directory (`native_auth.db`) using SQLite transactions. The issued device ID is
   server-generated and stable for the credential lifecycle.
5. Bearer parsing is centralized and strict: exactly one `Authorization` header,
   exact `Bearer <opaque-token>` grammar, bounded token alphabet/length. Tokens are
   stored only as SHA-256 hashes and expire or revoke closed.
6. `POST /api/v1/auth/devices/{device_id}/revoke` requires a valid bearer bound to
   the same device. The legacy revoke aliases are retained for compatibility but
   do not authorize native chat.
7. Native pairing requires authentication to be enabled. When password, passkey,
   OIDC, or trusted-header auth is not configured, pairing creation and browser
   approval fail closed with HTTP `503` and the stable error
   `native_auth_unavailable`. Existing cookie verification is not removed: approval
   still requires a verified browser session when auth is enabled. This vertical
   slice grants no privileged native scopes; a native caller cannot submit or choose
   scope. A future principal/profile binding must be added before any privileged
   scope is enabled.

No native chat dispatcher, refresh lifecycle, or deployment is part of this slice.
