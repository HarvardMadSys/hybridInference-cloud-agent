/**
 * Bridge pi to this deployment's MCP relay.
 *
 * pi 0.73.1 has no MCP client of its own -- its README says so, and the
 * supported route is an extension. This is that extension, and it is the only
 * one the runner loads: the adapter passes `--no-extensions`, which disables
 * auto-discovery (the project's `.pi/extensions/`, the global directory, and
 * anything named in a settings file) and leaves an explicit `-e` path as the
 * sole way in.
 *
 * It is a client, not a proxy. Every call goes to the same relay the other
 * agents use, carrying the same `mcp`-scoped job token, so pi sees exactly the
 * servers its conversation enabled and nothing else -- the relay resolves each
 * alias against that job's own bindings and holds anything the owner marked for
 * confirmation.
 *
 * One entry, at the relay's merged endpoint. That endpoint answers for every
 * connection of the job and namespaces what it lists as `<alias>__<tool>`, so
 * the map this bridge reads holds a single URL: one entry per connection would
 * register each connection's tools once per connection, and the alias this
 * bridge prefixes onto a tool name is the relay's own namespace, not a second
 * one the sandbox invented.
 *
 * Two properties are worth stating because the alternative is silent:
 *
 * - **A tool this extension cannot describe is not offered.** An upstream whose
 *   schema is not an object schema is registered with an empty parameter set
 *   only if it declares no parameters at all; otherwise it is reported and
 *   skipped, because a tool advertised with the wrong arguments is one the
 *   model will call wrongly.
 * - **A relay that cannot be reached fails the run.** Registering nothing would
 *   leave the agent working around tools the task was written around, which
 *   reads as a successful job.
 */

import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";

/** The relay entry, as `{name: url}`. Written by the control plane. */
const MCP_SERVERS_ENV = "AGENT_MCP_SERVERS";

/** The `mcp`-scoped job token, referenced here rather than in any config. */
const MCP_TOKEN_ENV = "AGENT_MCP_TOKEN";

/** The JSON-RPC methods this bridge uses. */
const INITIALIZE = "initialize";
const INITIALIZED = "notifications/initialized";
const SERVER_DISCOVER = "server/discover";
const TOOLS_LIST = "tools/list";
const TOOLS_CALL = "tools/call";

/** `_meta` keys the stateless revision reserves. */
const META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion";
const META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo";
const META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities";

/** The revision this bridge asks for first. */
const PREFERRED_VERSION = "2026-07-28";

/** How long one relay call may take. A tool call can be a repo-wide search. */
const REQUEST_TIMEOUT_MS = 120_000;

/** One upstream tool, as `tools/list` described it. */
interface McpTool {
  name: string;
  description?: string;
  inputSchema?: unknown;
}

/** What pi puts on the wire for one tool result. */
interface PiContent {
  type: string;
  text?: string;
  data?: string;
  mimeType?: string;
}

/**
 * One connection to one relay endpoint.
 *
 * The era is detected rather than assumed, the same way the control plane does
 * it: the current revision removed `initialize`, so a server that speaks it
 * answers `-32601`, and a server that speaks the handshake answers
 * `server/discover` with the same error. Sending the wrong one produces a
 * failure that reads like the server's fault.
 */
class RelayConnection {
  private nextId = 1;
  private sessionId: string | null = null;
  private protocolVersion = "";
  private stateless = false;
  private initialised = false;

  constructor(
    readonly alias: string,
    private readonly url: string,
    private readonly token: string,
  ) {}

  /** Complete whichever handshake this endpoint speaks. */
  async connect(): Promise<void> {
    if (this.initialised) return;
    try {
      const handshake = await this.request(INITIALIZE, {
        protocolVersion: PREFERRED_VERSION,
        capabilities: { roots: { listChanged: false } },
        clientInfo: { name: "pi-mcp-bridge", version: "1" },
      });
      const agreed = String((handshake as { protocolVersion?: string })?.protocolVersion ?? "");
      if (!agreed) throw new Error("the server's initialize reply named no protocol version");
      this.protocolVersion = agreed;
      await this.notify(INITIALIZED);
    } catch (error) {
      const code = (error as { code?: number }).code;
      if (code !== -32601 && code !== -32602) throw error;
      // No handshake: read the capabilities the stateless revision requires a
      // server to publish instead, and carry the version per request from here.
      const discovered = (await this.request(SERVER_DISCOVER, {})) as {
        supportedVersions?: unknown;
      };
      const supported = Array.isArray(discovered?.supportedVersions)
        ? discovered.supportedVersions.map(String)
        : [];
      if (!supported.includes(PREFERRED_VERSION)) {
        throw new Error(
          `the server speaks ${supported.join(", ") || "no declared version"}, which this bridge cannot drive`,
        );
      }
      this.protocolVersion = PREFERRED_VERSION;
      this.stateless = true;
    }
    this.initialised = true;
  }

  /** Every tool this endpoint offers, across pages. */
  async listTools(): Promise<McpTool[]> {
    await this.connect();
    const tools: McpTool[] = [];
    let cursor: string | undefined;
    // Bounded: a server that answers the same cursor forever would otherwise
    // make a session start never finish.
    for (let page = 0; page < 64; page += 1) {
      const result = (await this.request(TOOLS_LIST, cursor ? { cursor } : {})) as {
        tools?: unknown;
        nextCursor?: unknown;
      };
      const batch = Array.isArray(result?.tools) ? result.tools : [];
      for (const item of batch) {
        if (item && typeof item === "object" && typeof (item as McpTool).name === "string") {
          tools.push(item as McpTool);
        }
      }
      const next = typeof result?.nextCursor === "string" ? result.nextCursor : "";
      if (!next) break;
      cursor = next;
    }
    return tools;
  }

  /** Call one tool and return what pi should show the model. */
  async callTool(
    name: string,
    args: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<{ content: PiContent[]; isError: boolean; unsupported: string[] }> {
    await this.connect();
    const result = (await this.request(TOOLS_CALL, { name, arguments: args }, signal)) as {
      content?: unknown;
      isError?: unknown;
      resultType?: unknown;
    };
    if (result?.resultType === "input_required") {
      // The stateless revision lets a server ask mid-call. Answering needs a
      // user form, which this bridge has nowhere to put, so the model is told
      // rather than an approval being invented on its behalf.
      return {
        content: [
          {
            type: "text",
            text: `The ${name} server needs input from the user before it can answer.`,
          },
        ],
        isError: true,
        unsupported: [],
      };
    }
    return renderResult(result);
  }

  /** One JSON-RPC request, with the transport's own framing. */
  private async request(
    method: string,
    params: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<unknown> {
    const id = this.nextId++;
    if (this.stateless) {
      params = { ...params, _meta: this.meta() };
    }
    const response = await this.post({ jsonrpc: "2.0", id, method, params }, signal);
    const message = await this.readReply(response, id);
    if (message.error) {
      const error = message.error as { code: number; message: string };
      const failure = new Error(`${this.alias}: ${error.message}`) as Error & { code: number };
      failure.code = error.code;
      throw failure;
    }
    return message.result;
  }

  /** One notification: a method with no id, which expects no reply. */
  private async notify(method: string, params?: Record<string, unknown>): Promise<void> {
    const message: Record<string, unknown> = { jsonrpc: "2.0", method };
    if (params) message.params = params;
    const response = await this.post(message, undefined);
    await response.text().catch(() => "");
  }

  /** The per-request metadata the stateless revision requires on every call. */
  private meta(): Record<string, unknown> {
    return {
      [META_PROTOCOL_VERSION]: this.protocolVersion,
      [META_CLIENT_INFO]: { name: "pi-mcp-bridge", version: "1" },
      [META_CLIENT_CAPABILITIES]: { roots: { listChanged: false } },
    };
  }

  private async post(body: Record<string, unknown>, signal?: AbortSignal): Promise<Response> {
    const headers: Record<string, string> = {
      "content-type": "application/json",
      accept: "application/json, text/event-stream",
      authorization: `Bearer ${this.token}`,
    };
    if (this.sessionId) headers["mcp-session-id"] = this.sessionId;
    if (this.protocolVersion && !this.stateless) {
      headers["mcp-protocol-version"] = this.protocolVersion;
    }
    if (this.stateless && typeof body.method === "string") {
      headers["mcp-method"] = body.method;
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    if (signal) signal.addEventListener("abort", () => controller.abort(), { once: true });
    try {
      const response = await fetch(this.url, {
        method: "POST",
        headers,
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      const session = response.headers.get("mcp-session-id");
      if (session) this.sessionId = session;
      const version = response.headers.get("mcp-protocol-version");
      if (version && !this.stateless) this.protocolVersion = version;
      if (!response.ok) {
        throw new Error(
          response.status === 401 || response.status === 403
            ? `${this.alias}: the relay refused this job's token`
            : `${this.alias}: the relay answered ${response.status}`,
        );
      }
      return response;
    } finally {
      clearTimeout(timer);
    }
  }

  /**
   * Read the reply for one request id.
   *
   * A Streamable HTTP answer is either one JSON object or a request-scoped SSE
   * stream, and the reply may be preceded by progress notifications. Scanning
   * for the id rather than assuming the first message is the answer is what
   * makes the second case work at all.
   */
  private async readReply(
    response: Response,
    id: number,
  ): Promise<{ result?: unknown; error?: unknown }> {
    const contentType = response.headers.get("content-type") ?? "";
    if (!contentType.includes("text/event-stream")) {
      const document = (await response.json()) as unknown;
      return pickReply(document, id);
    }
    const body = response.body;
    if (!body) throw new Error(`${this.alias}: the relay sent an empty stream`);
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        for (const event of takeEvents(buffer)) {
          buffer = event.rest;
          if (!event.data) continue;
          const reply = pickReply(JSON.parse(event.data), id);
          if (reply.result !== undefined || reply.error !== undefined) return reply;
        }
      }
    } finally {
      reader.cancel().catch(() => {});
    }
    throw new Error(`${this.alias}: the stream ended without answering ${id}`);
  }
}

/** Pull one storable reply out of a single JSON-RPC message or a batch. */
function pickReply(
  document: unknown,
  id: number,
): { result?: unknown; error?: unknown } {
  const messages = Array.isArray(document) ? document : [document];
  for (const message of messages) {
    if (!message || typeof message !== "object") continue;
    const candidate = message as { id?: unknown; result?: unknown; error?: unknown };
    if (String(candidate.id) !== String(id)) continue;
    return { result: candidate.result, error: candidate.error };
  }
  return {};
}

/** Split complete SSE frames off a buffer, returning the remainder. */
function takeEvents(buffer: string): Array<{ data: string; rest: string }> {
  const events: Array<{ data: string; rest: string }> = [];
  let remaining = buffer;
  for (;;) {
    const match = /\r\n\r\n|\n\n|\r\r/.exec(remaining);
    if (!match) return events;
    const frame = remaining.slice(0, match.index);
    remaining = remaining.slice(match.index + match[0].length);
    const data = frame
      .split(/\r\n|\n|\r/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).replace(/^ /, ""))
      .join("\n");
    events.push({ data, rest: remaining });
  }
}

/** Map an MCP tool result onto the content pi renders. */
function renderResult(result: unknown): {
  content: PiContent[];
  isError: boolean;
  unsupported: string[];
} {
  const payload = (result ?? {}) as { content?: unknown; isError?: unknown };
  const blocks = Array.isArray(payload.content) ? payload.content : [];
  const content: PiContent[] = [];
  const unsupported: string[] = [];
  for (const block of blocks) {
    if (!block || typeof block !== "object") continue;
    const item = block as Record<string, unknown>;
    const type = String(item.type ?? "");
    if (type === "text") {
      content.push({ type: "text", text: String(item.text ?? "") });
    } else if (type === "image" || type === "audio") {
      content.push({
        type,
        data: String(item.data ?? ""),
        mimeType: String(item.mimeType ?? ""),
      });
    } else if (type === "resource") {
      const resource = (item.resource ?? {}) as Record<string, unknown>;
      if (typeof resource.text === "string") {
        content.push({ type: "text", text: resource.text });
      } else {
        // Named rather than dropped: an empty result would tell the model the
        // call succeeded with nothing in it.
        unsupported.push(String(resource.mimeType ?? type));
      }
    } else {
      unsupported.push(type || "unknown");
    }
  }
  if (unsupported.length > 0) {
    content.push({
      type: "text",
      text: `(${unsupported.length} content block(s) this agent cannot display: ${unsupported.join(", ")})`,
    });
  }
  return { content, isError: Boolean(payload.isError), unsupported };
}

/** Make one component of a tool name safe and bounded. */
function sanitize(value: string): string {
  const cleaned = value.replace(/[^A-Za-z0-9_-]/g, "_").replace(/^_+|_+$/g, "");
  return cleaned || "tool";
}

/**
 * The name pi sees, namespaced by the relay entry.
 *
 * At the merged endpoint the tool name already carries the connection's alias,
 * so the composed name reads `<entry>__<alias>__<tool>`. The prefix is kept
 * rather than skipped for the one entry this platform publishes: a name that
 * only sometimes carries a server component is a name nothing downstream can
 * take apart, and pi's registry is the only place a collision would show up.
 */
function presented(alias: string, tool: string): string {
  const combined = `${sanitize(alias)}__${sanitize(tool)}`;
  return combined.length <= 64 ? combined : combined.slice(0, 64);
}

/** Whether a declared schema describes an object of properties. */
function parametersOf(schema: unknown): Record<string, unknown> {
  if (schema && typeof schema === "object" && !Array.isArray(schema)) {
    return schema as Record<string, unknown>;
  }
  return { type: "object", properties: {} };
}

/**
 * Register every tool this job's connections offer.
 *
 * Load-time, not session-time: a failure has to stop the run rather than leave
 * the agent to discover the absence one call at a time.
 */
export default async function bridge(pi: ExtensionAPI): Promise<void> {
  const raw = process.env[MCP_SERVERS_ENV] || "{}";
  let endpoints: Record<string, string>;
  try {
    endpoints = JSON.parse(raw) as Record<string, string>;
  } catch {
    throw new Error(`${MCP_SERVERS_ENV} is not valid JSON; refusing to start without tools`);
  }
  const aliases = Object.keys(endpoints ?? {});
  if (aliases.length === 0) return;

  const token = process.env[MCP_TOKEN_ENV] || "";
  if (!token) {
    throw new Error(
      `${MCP_SERVERS_ENV} names ${aliases.length} connection(s) but ${MCP_TOKEN_ENV} is empty`,
    );
  }

  for (const alias of aliases) {
    const connection = new RelayConnection(alias, String(endpoints[alias]), token);
    const tools = await connection.listTools();
    for (const tool of tools) {
      pi.registerTool({
        name: presented(alias, tool.name),
        label: `${alias}: ${tool.name}`,
        description: tool.description || `${tool.name} on the ${alias} MCP server`,
        // The upstream's own schema, passed through: pi validates with it, so
        // translating it into a second vocabulary could only lose information.
        parameters: parametersOf(tool.inputSchema),
        async execute(_toolCallId, params, signal) {
          const outcome = await connection.callTool(
            tool.name,
            (params ?? {}) as Record<string, unknown>,
            signal,
          );
          return {
            content: outcome.content,
            isError: outcome.isError,
            details: { server: alias, tool: tool.name, unsupported: outcome.unsupported },
          };
        },
      });
    }
  }
}
