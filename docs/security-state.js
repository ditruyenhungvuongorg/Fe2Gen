(function (root) {
  'use strict';
  // Decisions of the page that protect a shared hospital computer, kept free of the DOM so Node can test them.

  // Idle logout: 'active', 'warn' (the last warnMs before the limit) or 'expired'. While a request is in
  // flight (busy) the doctor is waiting for the server, which counts as activity.
  function idleStep({now, lastActivity, idleMs, warnMs = 60000, busy = false}) {
    if (!(idleMs > 0)) return {state: 'active', remainingMs: Infinity, lastActivity};
    const last = busy ? now : lastActivity;
    const remainingMs = Math.max(0, last + idleMs - now);
    return {state: remainingMs === 0 ? 'expired' : remainingMs <= warnMs ? 'warn' : 'active', remainingMs, lastActivity: last};
  }

  root.SecurityState = {idleStep};
  if (typeof module !== 'undefined' && module.exports) module.exports = root.SecurityState;
})(globalThis);
