# Provider support

| Provider | Status | Role | Authentication | Fail-closed behavior |
|---|---|---|---|---|
| Polymarket Gamma/CLOB | Supported public feed | Discovery, metadata, bulk books, WebSocket depth | No key for public data | Gaps/resolution/status/depth uncertainty force resnapshot or `WATCH` |
| Polymarket sports state | Informational, unverified | Score/status display | No key | Cannot independently enable a sport model |
| The Odds API v4 | Supported optional paid feed | Reference source-family prices | User-supplied `THE_ODDS_API_KEY` | Missing key/provider IDs/timestamps remove support; quota headers remain diagnostic |
| Action Network | Experimental and undocumented | Optional references | Explicit feature flag | Disabled by default; schema errors do not gain trust |
| Pinnacle guest/Arcadia | Unsupported experimental adapter | Optional references | Explicit authorized key and feature flag | No shared key; disabled by default |

Provider availability is not statistical independence. Aliases and duplicated
aggregator origins are collapsed to one source family. Credentials stay in the
runtime environment and must never be committed.
