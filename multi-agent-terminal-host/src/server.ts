import crypto from "node:crypto";
import http, { type IncomingMessage, type ServerResponse } from "node:http";

import { WebSocket, WebSocketServer } from "ws";
import { z, ZodError } from "zod";

import {
  clientMessageSchema,
  createTerminalSessionSchema,
  type ServerMessage,
} from "./contracts.ts";
import type { TerminalHostConfig } from "./config.ts";
import { TerminalHostError } from "./errors.ts";
import type { SessionManager } from "./session-manager.ts";

const MAX_REQUEST_BODY_BYTES = 128 * 1024;

function jsonResponse(
  response: ServerResponse,
  statusCode: number,
  payload: unknown,
): void {
  const body = JSON.stringify(payload);
  response.writeHead(statusCode, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
  });
  response.end(body);
}

function errorResponse(response: ServerResponse, error: unknown): void {
  if (error instanceof TerminalHostError) {
    jsonResponse(response, error.statusCode, {
      error: { code: error.code, message: error.message },
    });
    return;
  }
  if (error instanceof ZodError) {
    jsonResponse(response, 400, {
      error: {
        code: "terminal.invalid_request",
        message: "request payload is invalid",
        details: error.issues,
      },
    });
    return;
  }
  const message = error instanceof Error ? error.message : String(error);
  jsonResponse(response, 500, {
    error: { code: "terminal.internal_error", message },
  });
}

function bearerToken(request: IncomingMessage): string | null {
  const authorization = request.headers.authorization;
  if (authorization?.startsWith("Bearer ")) {
    return authorization.slice("Bearer ".length).trim();
  }
  const protocols = request.headers["sec-websocket-protocol"];
  if (typeof protocols !== "string") {
    return null;
  }
  const prefix = "aitools-terminal-token.";
  const encoded = protocols
    .split(",")
    .map((value) => value.trim())
    .find((value) => value.startsWith(prefix));
  return encoded === undefined ? null : encoded.slice(prefix.length);
}

function tokensEqual(actual: string | null, expected: string): boolean {
  if (actual === null) {
    return false;
  }
  const actualBuffer = Buffer.from(actual);
  const expectedBuffer = Buffer.from(expected);
  return (
    actualBuffer.length === expectedBuffer.length &&
    crypto.timingSafeEqual(actualBuffer, expectedBuffer)
  );
}

function assertAuthorized(request: IncomingMessage, config: TerminalHostConfig): void {
  if (!tokensEqual(bearerToken(request), config.authToken)) {
    throw new TerminalHostError(
      "terminal.authentication_required",
      "Terminal Host authentication is required",
      401,
    );
  }
  const origin = request.headers.origin;
  if (origin !== undefined && !config.allowedOrigins.has(origin)) {
    throw new TerminalHostError(
      "terminal.origin_not_allowed",
      "request origin is not allowed",
      403,
    );
  }
}

async function readJson(request: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += buffer.length;
    if (size > MAX_REQUEST_BODY_BYTES) {
      throw new TerminalHostError(
        "terminal.request_too_large",
        "request body is too large",
        413,
      );
    }
    chunks.push(buffer);
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch (error) {
    throw new TerminalHostError(
      "terminal.invalid_json",
      "request body must be valid JSON",
      400,
    );
  }
}

function sessionPath(pathname: string): { sessionId: string; action: string | null } | null {
  const match = /^\/terminal-sessions\/([^/]+)(?:\/(stream|terminate))?$/u.exec(pathname);
  if (match === null) {
    return null;
  }
  const sessionId = match[1];
  if (sessionId === undefined) {
    return null;
  }
  return { sessionId: decodeURIComponent(sessionId), action: match[2] ?? null };
}

function websocketError(error: unknown): Extract<ServerMessage, { type: "error" }> {
  if (error instanceof TerminalHostError) {
    return { type: "error", code: error.code, message: error.message };
  }
  if (error instanceof ZodError) {
    return {
      type: "error",
      code: "terminal.invalid_message",
      message: "websocket message is invalid",
    };
  }
  return {
    type: "error",
    code: "terminal.internal_error",
    message: error instanceof Error ? error.message : String(error),
  };
}

function send(socket: WebSocket, message: ServerMessage): void {
  if (socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(message));
  }
}

export class TerminalHostServer {
  readonly #config: TerminalHostConfig;
  readonly #sessions: SessionManager;
  readonly #server: http.Server;
  readonly #webSockets: WebSocketServer;

  constructor(config: TerminalHostConfig, sessions: SessionManager) {
    this.#config = config;
    this.#sessions = sessions;
    this.#server = http.createServer((request, response) => {
      void this.#handleHttp(request, response);
    });
    this.#webSockets = new WebSocketServer({
      noServer: true,
      handleProtocols: (protocols) =>
        protocols.has("aitools-terminal.v1") ? "aitools-terminal.v1" : false,
    });
    this.#server.on("upgrade", (request, socket, head) => {
      try {
        assertAuthorized(request, this.#config);
        const url = new URL(request.url ?? "/", "http://localhost");
        const path = sessionPath(url.pathname);
        if (path?.action !== "stream") {
          throw new TerminalHostError(
            "terminal.websocket_path_not_found",
            "websocket endpoint was not found",
            404,
          );
        }
        const clientId = url.searchParams.get("client_id")?.trim();
        if (!clientId || !z.string().uuid().safeParse(clientId).success) {
          throw new TerminalHostError(
            "terminal.client_id_required",
            "client_id must be a UUID",
            400,
          );
        }
        this.#sessions.getLive(path.sessionId);
        this.#webSockets.handleUpgrade(request, socket, head, (webSocket) => {
          this.#attachWebSocket(webSocket, path.sessionId, clientId);
        });
      } catch {
        socket.write("HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n");
        socket.destroy();
      }
    });
  }

  async start(): Promise<void> {
    await new Promise<void>((resolve, reject) => {
      const onError = (error: Error): void => {
        this.#server.off("listening", onListening);
        reject(error);
      };
      const onListening = (): void => {
        this.#server.off("error", onError);
        resolve();
      };
      this.#server.once("error", onError);
      this.#server.once("listening", onListening);
      this.#server.listen(this.#config.port, this.#config.host);
    });
  }

  async close(): Promise<void> {
    for (const client of this.#webSockets.clients) {
      client.close(1001, "Terminal Host is stopping");
    }
    this.#webSockets.close();
    this.#sessions.close();
    await new Promise<void>((resolve, reject) => {
      this.#server.close((error) => (error === undefined ? resolve() : reject(error)));
    });
  }

  async #handleHttp(request: IncomingMessage, response: ServerResponse): Promise<void> {
    try {
      const url = new URL(request.url ?? "/", "http://localhost");
      if (request.method === "GET" && (url.pathname === "/health" || url.pathname === "/ready")) {
        jsonResponse(response, 200, { status: "ok" });
        return;
      }
      assertAuthorized(request, this.#config);
      if (request.method === "GET" && url.pathname === "/terminal-sessions") {
        const delegationId = url.searchParams.get("delegation_id") ?? undefined;
        jsonResponse(response, 200, { sessions: this.#sessions.list(delegationId) });
        return;
      }
      if (request.method === "POST" && url.pathname === "/terminal-sessions") {
        const payload = createTerminalSessionSchema.parse(await readJson(request));
        jsonResponse(response, 201, { session: await this.#sessions.create(payload) });
        return;
      }
      const path = sessionPath(url.pathname);
      if (request.method === "GET" && path?.action === null) {
        jsonResponse(response, 200, { session: this.#sessions.get(path.sessionId) });
        return;
      }
      if (request.method === "POST" && path?.action === "terminate") {
        jsonResponse(response, 200, { session: this.#sessions.terminate(path.sessionId) });
        return;
      }
      jsonResponse(response, 404, {
        error: { code: "terminal.route_not_found", message: "route was not found" },
      });
    } catch (error) {
      errorResponse(response, error);
    }
  }

  #attachWebSocket(socket: WebSocket, sessionId: string, clientId: string): void {
    const session = this.#sessions.getLive(sessionId);
    let ready = false;
    const pending: ServerMessage[] = [];
    const unsubscribe = session.subscribe((message) => {
      if (ready) {
        send(socket, message);
      } else {
        pending.push(message);
      }
    });

    void session
      .snapshot()
      .then((snapshot) => {
        send(socket, snapshot);
        ready = true;
        for (const message of pending) {
          if (message.type !== "output" || message.sequence > snapshot.sequence) {
            send(socket, message);
          }
        }
        pending.length = 0;
      })
      .catch((error: unknown) => send(socket, websocketError(error)));

    socket.on("message", (data, isBinary) => {
      try {
        if (isBinary) {
          throw new TerminalHostError(
            "terminal.binary_message_not_supported",
            "binary websocket messages are not supported",
            400,
          );
        }
        const message = clientMessageSchema.parse(JSON.parse(data.toString("utf8")));
        if (message.type === "lease.acquire") {
          const lease = session.acquireLease(clientId);
          send(socket, {
            type: "lease.granted",
            lease_token: lease.leaseToken,
            expires_at: lease.expiresAt,
          });
        } else if (message.type === "lease.renew") {
          send(socket, {
            type: "lease.granted",
            lease_token: message.lease_token,
            expires_at: session.renewLease(clientId, message.lease_token),
          });
        } else if (message.type === "lease.release") {
          session.releaseLease(clientId, message.lease_token);
          send(socket, { type: "lease.released" });
        } else if (message.type === "input") {
          session.write(clientId, message.lease_token, message.data);
        } else if (message.type === "resize") {
          session.resize(clientId, message.lease_token, message.cols, message.rows);
        } else {
          socket.close(1000, "detached");
        }
      } catch (error) {
        send(socket, websocketError(error));
      }
    });
    socket.once("close", unsubscribe);
  }
}
