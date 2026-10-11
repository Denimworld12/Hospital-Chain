// node --test: the admin login (server/auth.js), with an in-memory stand-in for Postgres.

import { test } from "node:test";
import assert from "node:assert/strict";
import { Auth, COOKIE, Lockout, checkNewPassword, parseCookies, sameOrigin, sessionCookie } from "./auth.js";

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

// admin_users as Postgres would hold it, for the queries Auth sends
function fakePool() {
  const users = new Map();
  return {
    users,
    async query(sql, params = []) {
      if (sql.includes("INSERT INTO admin_users")) {
        if (users.size) return { rowCount: 0, rows: [] };
        users.set(params[0], params[1]);
        return { rowCount: 1, rows: [] };
      }
      if (sql.includes("count(*)")) return { rows: [{ n: users.size }] };
      return { rowCount: 0, rows: [] };
    },
  };
}

test("the admin account comes only from DASHBOARD_ADMIN_PASSWORD: unset, there is none", async () => {
  const none = new Auth(fakePool(), { adminUser: "admin", adminPassword: "" });
  assert.equal(await none.accountExists(), false);
  const pool = fakePool();
  const set = new Auth(pool, { adminUser: "admin", adminPassword: "a-long-enough-password" });
  assert.equal(await set.accountExists(), true);
  assert.deepEqual([...pool.users.keys()], ["admin"]);
});
