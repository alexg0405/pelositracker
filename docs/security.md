# Security model

- Passwords are converted to Argon2 hashes at startup. Separate random session
  tokens are stored only as hashes, expire, idle-timeout, and revoke independently.
- Production cookies are Secure, HttpOnly where appropriate, SameSite Strict,
  and scoped to `/`. Unsafe authenticated requests require a matching session,
  CSRF cookie, and `X-CSRF-Token` header.
- Login and API requests have process-local sliding-window limits. Production
  rejects `admin/admin` and rejects multiple web workers.
- Notifications require HTTPS and an explicit Discord/Slack host allowlist.
  Userinfo, fragments, unusual ports, redirects, and non-public DNS results are
  rejected. Webhook secrets are not logged.
- The UI has no inline script/style/event handlers. Third-party JS is vendored
  locally and the response uses a restrictive CSP plus HSTS in production,
  frame denial, MIME sniffing denial, and a restrictive permissions policy.

Sessions and rate-limit state are process-local under the supported one-worker
topology. A distributed deployment requires a shared session/rate-limit store.
