import { useCallback, useEffect, useState } from "react";

// The admin's session (server/auth.js): "loading", "setup" (first run: no account yet), "login" or "in".
// Any request that comes back 401 fires UNAUTHORIZED, and the gate shows the login screen again.
export const UNAUTHORIZED = "medchain:unauthorized";

export function notAuthorized(res) {
  if (res.status === 401) window.dispatchEvent(new Event(UNAUTHORIZED));
  return res.status === 401;
}

async function post(path, body) {
  const res = await fetch(`/api/auth/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, ...data };
}

export function useAuth() {
  const [state, setState] = useState({ status: "loading", username: null, error: "" });

  const refresh = useCallback(async () => {
    try {
      const res = await fetch("/api/auth/me");
      const me = await res.json();
      setState({ status: me.authenticated ? "in" : me.setup_required ? "setup" : "login", username: me.username, error: "" });
    } catch {
      setState((s) => ({ ...s, status: s.status === "loading" ? "login" : s.status, error: "Could not reach the dashboard server." }));
    }
  }, []);

  useEffect(() => {
    refresh();
    window.addEventListener(UNAUTHORIZED, refresh);
    return () => window.removeEventListener(UNAUTHORIZED, refresh);
  }, [refresh]);

  const submit = async (action, username, password) => {
    const out = await post(action, { username, password });
    if (out.ok) setState({ status: "in", username: out.username, error: "" });
    return out;
  };

  return {
    ...state,
    login: (u, p) => submit("login", u, p),
    setup: (u, p) => submit("setup", u, p),
    logout: async () => {
      await post("logout");
      setState({ status: "login", username: null, error: "" });
    },
  };
}
