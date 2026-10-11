// The dashboard's one admin account and its sessions. Everything except the login screen itself
// (the built UI and /api/auth/*) needs a session: the snapshot WebSocket, Start / Pause, the risk
// assessment relay and the admin pages.
//
// - One admin account, created on first run from DASHBOARD_ADMIN_PASSWORD (compose maps
//   MEDCHAIN_ADMIN_PASSWORD to it). Unset, there is no account and the login screen says so. The password is stored as a bcrypt hash (cost 12), never in the clear.
// - A session is a random 32-byte token in an httpOnly, SameSite=Strict cookie; Postgres keeps only
//   its SHA-256 and expiry (SESSION_HOURS, default 12). Logout deletes it.
// - Five failed logins from one address lock that address out for 5 minutes.
// - A state-changing request whose Origin is another site is refused (SameSite already keeps the
//   cookie off it; this is the second lock).
//
// These tables are the dashboard's own; it still never writes the training tables.

import crypto from "node:crypto";
import bcrypt from "bcryptjs";

export const COOKIE = "medchain_session";
const SESSION_HOURS = Number(process.env.SESSION_HOURS || 12);
const BCRYPT_COST = 12;
export const MIN_PASSWORD = 10;
const LOCKOUT = { failures: 5, windowMs: 5 * 60 * 1000 };
// compared against for an unknown username, so a wrong name and a wrong password take the same time
const NO_SUCH_USER = bcrypt.hashSync(crypto.randomBytes(16).toString("hex"), BCRYPT_COST);

const SCHEMA = `
CREATE TABLE IF NOT EXISTS admin_users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    one_admin     BOOLEAN NOT NULL DEFAULT true UNIQUE CHECK (one_admin)  -- exactly one account
);
CREATE TABLE IF NOT EXISTS admin_sessions (
    token_sha256 TEXT PRIMARY KEY,
    username     TEXT NOT NULL REFERENCES admin_users (username) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL
);`;

const sha256 = (s) => crypto.createHash("sha256").update(s).digest("hex");

export function parseCookies(header = "") {
  const out = {};
  for (const part of header.split(";")) {
    const i = part.indexOf("=");
    if (i > 0) out[part.slice(0, i).trim()] = decodeURIComponent(part.slice(i + 1).trim());
  }
  return out;
}

export function sessionCookie(token, { secure = false, maxAge = SESSION_HOURS * 3600 } = {}) {
  return `${COOKIE}=${token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=${maxAge}${secure ? "; Secure" : ""}`;
}

// A browser sends Origin on cross-site POSTs; when it is there it must be this dashboard.
export function sameOrigin(req) {
  const origin = req.headers.origin;
  if (!origin) return true;
  try {
    return new URL(origin).host === req.headers.host;
  } catch {
    return false;
  }
}

export function checkNewPassword(username, password) {
  if (!/^[A-Za-z0-9._-]{3,40}$/.test(username || "")) return "The username is 3 to 40 letters, digits, dots, dashes or underscores.";
  if (typeof password !== "string" || password.length < MIN_PASSWORD) return `The password needs at least ${MIN_PASSWORD} characters.`;
  if (password.length > 72) return "The password can be at most 72 characters (bcrypt's limit).";
  return null;
}

// Failed logins per client address (in memory: a restart forgets them, which is fine for a lockout).
export class Lockout {
  constructor({ failures, windowMs } = LOCKOUT, now = () => Date.now()) {
    Object.assign(this, { failures, windowMs, now, seen: new Map() });
  }

  locked(who) {
    const times = (this.seen.get(who) || []).filter((t) => this.now() - t < this.windowMs);
    this.seen.set(who, times);
    return times.length >= this.failures;
  }

  fail(who) {
    this.seen.set(who, [...(this.seen.get(who) || []), this.now()]);
  }

  clear(who) {
    this.seen.delete(who);
  }
}

export class Auth {
  constructor(pool, { adminUser = process.env.DASHBOARD_ADMIN_USER || "admin",
                      adminPassword = process.env.DASHBOARD_ADMIN_PASSWORD || "" } = {}) {
    Object.assign(this, { pool, adminUser, adminPassword, ready: null, lockout: new Lockout() });
  }

  // Create the tables, and make the one account match DASHBOARD_ADMIN_PASSWORD, once Postgres answers. A changed
  // password (or username) replaces the old one and ends every existing session.
  async init() {
    this.ready ||= (async () => {
      await this.pool.query(SCHEMA);
      if (this.adminPassword) {
        const problem = checkNewPassword(this.adminUser, this.adminPassword);
        if (problem) throw new Error(`DASHBOARD_ADMIN_PASSWORD: ${problem}`);
        const { rows } = await this.pool.query(`SELECT username, password_hash FROM admin_users`);
        const same = rows[0]?.username === this.adminUser && (await bcrypt.compare(this.adminPassword, rows[0].password_hash));
        if (!same) {
          const hash = await bcrypt.hash(this.adminPassword, BCRYPT_COST);
          await this.pool.query(`DELETE FROM admin_sessions`);
          await this.pool.query(
            `INSERT INTO admin_users (username, password_hash) VALUES ($1, $2)
             ON CONFLICT (one_admin) DO UPDATE SET username = EXCLUDED.username, password_hash = EXCLUDED.password_hash`,
            [this.adminUser, hash],
          );
        }
      }
    })().catch((err) => {
      this.ready = null; // try again on the next request
      throw err;
    });
    return this.ready;
  }

  async accountExists() {
    await this.init();
    const { rows } = await this.pool.query(`SELECT count(*)::int AS n FROM admin_users`);
    return rows[0].n > 0;
  }

  async login(username, password, who) {
    await this.init();
    if (this.lockout.locked(who)) {
      return { ok: false, status: 429, detail: "Too many failed logins from here: wait 5 minutes and try again." };
    }
    const { rows } = await this.pool.query(`SELECT password_hash FROM admin_users WHERE username = $1`, [username || ""]);
    const good = await bcrypt.compare(String(password || ""), rows[0]?.password_hash || NO_SUCH_USER);
    if (!rows.length || !good) {
      this.lockout.fail(who);
      return { ok: false, status: 401, detail: "Wrong username or password." };
    }
    this.lockout.clear(who);
    return { ok: true, ...(await this.newSession(username)) };
  }

  async newSession(username) {
    const token = crypto.randomBytes(32).toString("base64url");
    await this.pool.query(
      `INSERT INTO admin_sessions (token_sha256, username, expires_at)
       VALUES ($1, $2, now() + make_interval(hours => $3))`,
      [sha256(token), username, SESSION_HOURS],
    );
    await this.pool.query(`DELETE FROM admin_sessions WHERE expires_at < now()`);
    return { token, username };
  }

  // The logged-in username for this request's cookie, or null.
  async user(req) {
    const token = parseCookies(req.headers.cookie)[COOKIE];
    if (!token) return null;
    await this.init();
    const { rows } = await this.pool.query(
      `SELECT username FROM admin_sessions WHERE token_sha256 = $1 AND expires_at > now()`,
      [sha256(token)],
    );
    return rows[0]?.username || null;
  }

  async logout(req) {
    const token = parseCookies(req.headers.cookie)[COOKIE];
    if (token) await this.pool.query(`DELETE FROM admin_sessions WHERE token_sha256 = $1`, [sha256(token)]);
  }
}
