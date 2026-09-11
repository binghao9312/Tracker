import { useEffect, useRef } from "react";

export type SocketStatus = "CONNECTING" | "CONNECTED" | "RECONNECTING";

export type ReconnectingSocketOptions = {
  onMessage: (data: string) => void;
  onOpen?: () => void;
  onStatusChange?: (status: SocketStatus) => void;
};

export function useReconnectingSocket(
  path: string | null,
  options: ReconnectingSocketOptions,
): void {
  const optionsRef = useRef(options);
  optionsRef.current = options;

  useEffect(() => {
    if (path === null) {
      return;
    }

    let socket: WebSocket | null = null;
    let reconnectTimer: number | null = null;
    let backoffDelay = 1000;
    let isReconnecting = false;
    let disposed = false;

    const scheduleReconnect = () => {
      if (disposed) return;
      isReconnecting = true;
      optionsRef.current.onStatusChange?.("RECONNECTING");
      if (reconnectTimer !== null) return;
      const delay = backoffDelay;
      backoffDelay = Math.min(backoffDelay * 2, 30000);
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, delay);
    };

    const connect = () => {
      if (disposed) return;
      if (!isReconnecting) {
        optionsRef.current.onStatusChange?.("CONNECTING");
      }
      const url = path.startsWith("ws://") || path.startsWith("wss://")
        ? path
        : `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}${path}`;
      const ws = new WebSocket(url);
      socket = ws;

      ws.onopen = () => {
        if (disposed || socket !== ws) return;
        backoffDelay = 1000;
        isReconnecting = false;
        optionsRef.current.onStatusChange?.("CONNECTED");
        optionsRef.current.onOpen?.();
      };

      ws.onmessage = (event: MessageEvent) => {
        if (disposed || socket !== ws) return;
        const data = typeof event.data === "string" ? event.data : String(event.data);
        optionsRef.current.onMessage(data);
      };

      ws.onerror = () => {
        if (disposed || socket !== ws) return;
        scheduleReconnect();
      };

      ws.onclose = () => {
        if (disposed || socket !== ws) return;
        scheduleReconnect();
      };
    };

    connect();

    return () => {
      disposed = true;
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      if (socket) {
        socket.onopen = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.onclose = null;
        socket.close();
        socket = null;
      }
    };
  }, [path]);
}
