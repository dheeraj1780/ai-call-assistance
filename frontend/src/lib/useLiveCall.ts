import { useCallback, useEffect, useRef, useState } from "react";

import { currentAccessToken, refreshSession, wsUrl } from "./api";
import { applyEvent, fromSnapshot, liveApi, type LiveMessage, type LiveState } from "./live";

export type Connection = "connecting" | "live" | "reconnecting" | "offline";

/**
 * Snapshot + WebSocket with automatic reconnect and resume (last seq + epoch).
 * Closing this page or losing the network only affects what the browser shows;
 * the phone call itself is not affected.
 */
export function useLiveCall(callId: string) {
  const [state, setState] = useState<LiveState | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [connection, setConnection] = useState<Connection>("connecting");
  const stateRef = useRef<LiveState | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(0);
  const closedRef = useRef(false);

  const commit = useCallback((next: LiveState) => {
    stateRef.current = next;
    setState(next);
  }, []);

  const loadSnapshot = useCallback(async () => {
    try {
      const snap = await liveApi.snapshot(callId);
      commit(fromSnapshot(snap));
      setError(null);
    } catch (e) {
      setError(e);
    }
  }, [callId, commit]);

  useEffect(() => {
    closedRef.current = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const connect = async () => {
      if (closedRef.current) return;
      if (!stateRef.current) await loadSnapshot();
      if (!currentAccessToken()) await refreshSession();
      const token = currentAccessToken();
      if (!token || closedRef.current) {
        setConnection("offline");
        return;
      }
      const socket = new WebSocket(wsUrl(`/api/v1/calls/${callId}/live/ws`));
      socketRef.current = socket;
      socket.onopen = () => {
        socket.send(
          JSON.stringify({
            type: "auth",
            token,
            last_seq: stateRef.current?.seq ?? null,
            epoch: stateRef.current?.epoch ?? null,
          }),
        );
      };
      socket.onmessage = (event) => {
        const msg = JSON.parse(String(event.data)) as LiveMessage;
        if (msg.type === "hello") {
          retryRef.current = 0;
          setConnection("live");
          return;
        }
        if (msg.type === "pong") return;
        if (msg.type === "resync") {
          void loadSnapshot();
          return;
        }
        if (stateRef.current) commit(applyEvent(stateRef.current, msg));
      };
      socket.onclose = (event) => {
        socketRef.current = null;
        if (closedRef.current) return;
        if (event.code === 4401) void refreshSession();
        setConnection("reconnecting");
        retryRef.current += 1;
        const delay = Math.min(10_000, 500 * 2 ** retryRef.current);
        timer = setTimeout(() => void connect(), delay);
      };
    };
    void connect();
    const ping = setInterval(() => {
      if (socketRef.current?.readyState === WebSocket.OPEN) socketRef.current.send(JSON.stringify({ type: "ping" }));
    }, 25_000);

    return () => {
      closedRef.current = true;
      clearInterval(ping);
      if (timer) clearTimeout(timer);
      socketRef.current?.close();
    };
  }, [callId, commit, loadSnapshot]);

  const patch = useCallback(
    (fn: (s: LiveState) => LiveState) => {
      if (stateRef.current) commit(fn(stateRef.current));
    },
    [commit],
  );

  return { state, error, connection, reload: loadSnapshot, patch };
}
