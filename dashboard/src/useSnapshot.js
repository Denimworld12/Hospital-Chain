import { useEffect, useRef, useState } from "react";

// Live snapshot from the observer server's WebSocket, with automatic reconnect.
export function useSnapshot() {
  const [snapshot, setSnapshot] = useState(null);
  const [connection, setConnection] = useState("connecting");
  const retry = useRef(0);

  useEffect(() => {
    let ws;
    let timer;
    let closed = false;
    const connect = () => {
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${window.location.host}/ws`);
      ws.onopen = () => {
        retry.current = 0;
        setConnection("live");
      };
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === "snapshot") setSnapshot(msg.data);
        } catch {
          /* ignore malformed frames */
        }
      };
      ws.onclose = () => {
        if (closed) return;
        setConnection("reconnecting");
        const delay = Math.min(5000, 500 * 2 ** retry.current++);
        timer = setTimeout(connect, delay);
      };
      ws.onerror = () => ws.close();
    };
    connect();
    return () => {
      closed = true;
      clearTimeout(timer);
      ws?.close();
    };
  }, []);

  return { snapshot, connection };
}

export async function control(action, body = {}) {
  const res = await fetch(`/api/round/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, ...data };
}
