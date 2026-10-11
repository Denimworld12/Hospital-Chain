// node --test: the pieces of the admin login that need no database (server/auth.js).

import { test } from "node:test";
import assert from "node:assert/strict";
import { COOKIE, Lockout, checkNewPassword, parseCookies, sameOrigin, sessionCookie } from "./auth.js";

test("the session cookie is httpOnly, SameSite=Strict, and Secure over HTTPS", () => {
  const c = sessionCookie("tok", { secure: false, maxAge: 60 });
  assert.match(c, new RegExp(`^${COOKIE}=tok; HttpOnly; SameSite=Strict; Path=/; Max-Age=60$`));
  assert.match(sessionCookie("tok", { secure: true }), /; Secure$/);
  assert.equal(parseCookies(`a=1; ${COOKIE}=x%2By; b`)[COOKIE], "x+y");
});

test("a request from another site is refused, one from the dashboard or without Origin is not", () => {
  const req = (origin) => ({ headers: { host: "localhost:8080", ...(origin ? { origin } : {}) } });
  assert.ok(sameOrigin(req()));
  assert.ok(sameOrigin(req("http://localhost:8080")));
  assert.ok(!sameOrigin(req("http://evil.example")));
  assert.ok(!sameOrigin(req("not a url")));
});

test("five failed logins lock an address out for the window, and only that address", () => {
  let now = 0;
  const l = new Lockout({ failures: 5, windowMs: 1000 }, () => now);
  for (let i = 0; i < 5; i++) {
    assert.ok(!l.locked("a"));
    l.fail("a");
  }
  assert.ok(l.locked("a") && !l.locked("b"));
  now = 1001;
  assert.ok(!l.locked("a"), "the window passed");
});

test("the admin account needs a sensible username and a 10-72 character password", () => {
  assert.equal(checkNewPassword("admin", "correct horse"), null);
  assert.match(checkNewPassword("ad", "correct horse"), /username/);
  assert.match(checkNewPassword("admin", "short"), /at least 10/);
  assert.match(checkNewPassword("admin", "x".repeat(73)), /72/);
});
