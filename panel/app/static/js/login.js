// Sign-in page: fetches a fresh CSRF token before posting, when the page has
// been open a while.
//
// The token is only as good as the session cookie it came with, and the server
// refuses that cookie once it is SESSION_LIFETIME_HOURS old. A sign-in page
// left open overnight - where the panel's own sign-out leaves you - would post
// a token nothing can be checked against. If the fetch fails the form posts
// anyway, and the server answers with a fresh sign-in page.

(function () {
  "use strict";

  var script = document.currentScript;
  var form = document.getElementById("login-form");
  if (!form) return;

  // Short enough for any session lifetime, long enough that signing in from a
  // page just opened never waits on the extra request.
  var STALE_AFTER_MS = 10 * 60 * 1000;
  var loadedAt = Date.now();

  form.addEventListener("submit", function (event) {
    if (Date.now() - loadedAt < STALE_AFTER_MS) return;
    event.preventDefault();
    fetch(script.dataset.tokenUrl, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (body) {
        if (body && body.csrf_token) form.elements.csrf_token.value = body.csrf_token;
      })
      .catch(function () {})
      .then(function () { form.submit(); });
  });
})();
