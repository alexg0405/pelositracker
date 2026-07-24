// Shared CSRF-aware fetch wrapper. Loaded before both index.js and watch.js so
// every authenticated mutating request (POST/PUT/PATCH/DELETE) from either page
// carries the X-CSRF-Token header that the server (verify_auth) requires. Reads
// the readable csrf_token cookie (or its __Host- prefixed production form).
// Idempotent: it will not double-wrap if this script is included more than once.
(() => {
  if (window.__csrfFetchInstalled) return;
  window.__csrfFetchInstalled = true;
  const nativeFetch = window.fetch.bind(window);
  window.fetch = (resource, options = {}) => {
    const method = String(options.method || "GET").toUpperCase();
    if (["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
      const cookie = document.cookie.split("; ")
        .find(value => value.startsWith("csrf_token=") || value.startsWith("__Host-csrf_token="));
      if (cookie) {
        const headers = new Headers(options.headers || {});
        headers.set("X-CSRF-Token", decodeURIComponent(cookie.split("=").slice(1).join("=")));
        options = {...options, headers};
      }
    }
    return nativeFetch(resource, options);
  };
})();
