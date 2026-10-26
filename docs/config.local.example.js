// Development only. The published index.html allows the page to call 'self' and the production API host
// (Content-Security-Policy connect-src). To test against a local backend, run `python serve_web.py` and open
// http://127.0.0.1:8000/ : the backend serves this UI itself, so an empty apiBase calls the same origin.
// Copy this file over config.js on your machine only; never commit that change.
window.PRENATAL_CONFIG = { apiBase: '' };
