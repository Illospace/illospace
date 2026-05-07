type Handler = (data: any) => void;
type TokenProvider = () => Promise<string> | string;

const MAX_RECONNECT_ATTEMPTS = 20;

export class WsClient {
  private socket: WebSocket | null = null;
  private handlers = new Map<string, Set<Handler>>();
  private reconnectDelay = 1000;
  private reconnectAttempts = 0;
  private _reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private url = '';
  private tokenProvider: TokenProvider | null = null;
  private _wasConnected = false;
  private _intentionalClose = false;
  private _reconnectCallbacks = new Set<() => void>();

  connect(url: string, tokenProvider: TokenProvider) {
    this.url = url;
    this.tokenProvider = tokenProvider;
    this._intentionalClose = false;
    // Prevent duplicate connections
    if (this.socket && this.socket.readyState <= WebSocket.OPEN) {
      this.socket.close();
    }
    this.socket = new WebSocket(url);
    const socket = this.socket;

    socket.onopen = async () => {
      this.reconnectDelay = 1000;
      this.reconnectAttempts = 0;
      try {
        const token = await tokenProvider();
        if (this.socket !== socket) return;
        if (socket.readyState !== WebSocket.OPEN) return;
        socket.send(JSON.stringify({ type: 'auth', token }));
      } catch (err) {
        console.warn('[WsClient] Failed to acquire WebSocket token:', err);
        socket.close();
        return;
      }
      // Fire reconnect callbacks (re-fetch state after reconnect)
      if (this._wasConnected) {
        this._reconnectCallbacks.forEach((fn) => fn());
      }
      this._wasConnected = true;
    };

    socket.onmessage = (evt) => {
      let msg: any;
      try {
        msg = JSON.parse(evt.data);
      } catch (err) {
        console.warn('[WsClient] Failed to parse message:', err, evt.data);
        return;
      }
      if (msg.type === 'ping') return;
      this.handlers.get(msg.type)?.forEach((fn) => fn(msg));
    };

    socket.onerror = (err) => {
      console.warn('[WsClient] WebSocket error:', err);
    };

    socket.onclose = () => {
      if (this._intentionalClose) return;
      if (this.reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
        console.warn(
          `[WsClient] Max reconnect attempts (${MAX_RECONNECT_ATTEMPTS}) reached, giving up`,
        );
        return;
      }
      this._reconnectTimer = setTimeout(() => {
        this._reconnectTimer = null;
        this.reconnectAttempts++;
        this.reconnectDelay = Math.min(this.reconnectDelay * 2, 30000);
        if (this.tokenProvider) {
          this.connect(this.url, this.tokenProvider);
        }
      }, this.reconnectDelay);
    };
  }

  on(event: string, handler: Handler): () => void {
    let set = this.handlers.get(event);
    if (!set) {
      set = new Set();
      this.handlers.set(event, set);
    }
    set.add(handler);
    return () => {
      const current = this.handlers.get(event);
      if (current) current.delete(handler);
    };
  }

  /** Register a callback for when the WS reconnects (re-fetch state). */
  onReconnect(fn: () => void): () => void {
    this._reconnectCallbacks.add(fn);
    return () => this._reconnectCallbacks.delete(fn);
  }

  send(event: string, data: Record<string, unknown> = {}) {
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify({ type: event, ...data }));
    }
  }

  disconnect() {
    this._intentionalClose = true;
    this._wasConnected = false;
    if (this._reconnectTimer !== null) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    this.socket?.close();
    this.socket = null;
  }
}
