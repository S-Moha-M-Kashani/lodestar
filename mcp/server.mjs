#!/usr/bin/env node
// mcp/server.mjs — Lodestar as an MCP server, over stdio.
//
// One process, spoken to in newline-delimited JSON-RPC 2.0 on stdin and
// stdout. That is the whole of the stdio transport: a message per line, no
// framing headers (those belong to LSP, not to this), and **nothing but
// protocol may ever be written to stdout** — a stray `console.log` lands in
// the middle of the stream and the client drops the connection. Every
// diagnostic here goes to stderr for that reason.
//
// **Hand-rolled, and that is the point.** `server.js` has had one npm
// dependency in its life, added deliberately because Postgres has no driver in
// the standard library. The MCP SDK would be the second, for a protocol whose
// server half — initialize, tools/list, tools/call — is the eighty lines
// below. The rule that keeps this honest is the same one the rest of the
// backend follows: if this file starts needing session resumption, sampling,
// elicitation or server-initiated requests, the SDK has earned its call and
// this note should be replaced with that decision rather than argued around.
//
// **A failing tool is an answer, not a transport error.** MCP draws the line
// there deliberately: a JSON-RPC error means the call could not be made, while
// `isError: true` with the reason in the text is a call that was made and
// failed — which is the one the model can read and act on. So "no card C-999
// on this board" comes back as text the caller can fix, and only a malformed
// request gets a protocol error.
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createInterface } from 'node:readline';

import { loadEnv } from './board.mjs';
import { TOOLS, TOOL_BY_NAME } from './tools.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

/** The protocol versions this server knows how to be. A client naming one of
 *  them is answered in its own version; a client naming anything else is
 *  answered in the newest, which is what the spec asks for — the client then
 *  decides whether it can live with that rather than the connection dying over
 *  a string. */
const VERSIONS = ['2025-06-18', '2025-03-26', '2024-11-05'];

const say = (msg) => process.stdout.write(JSON.stringify(msg) + '\n');
const reply = (id, result) => say({ jsonrpc: '2.0', id, result });
const fail = (id, code, message) => say({ jsonrpc: '2.0', id, error: { code, message } });

/** The tool list a client is offered. Built from the table rather than written
 *  out here, so a tool cannot exist and be unreachable, or be advertised and
 *  not exist. */
const advertised = () => TOOLS.map(({ name, description, inputSchema }) =>
  ({ name, description, inputSchema }));

async function callTool(params) {
  const tool = TOOL_BY_NAME.get(params?.name);
  if (!tool) {
    return { content: [{ type: 'text', text:
      `No tool called ${params?.name}. This server offers: `
      + TOOLS.map((t) => t.name).join(', ') + '.' }], isError: true };
  }
  try {
    return await tool.run(params.arguments || {});
  } catch (err) {
    // The reason, as a sentence. `board.mjs` already words the two failures a
    // person can do something about — a board that is not running, and a
    // credential it refused — so they arrive here ready to read.
    return { content: [{ type: 'text', text: err.message }], isError: true };
  }
}

async function handle(msg) {
  const { id, method, params } = msg;
  // A notification carries no id and is never answered — replying to one is a
  // protocol violation that some clients drop the connection over.
  const isNotification = id === undefined || id === null;

  switch (method) {
    case 'initialize': {
      const asked = params?.protocolVersion;
      return reply(id, {
        protocolVersion: VERSIONS.includes(asked) ? asked : VERSIONS[0],
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: 'lodestar', version: '1.0.0' },
        instructions:
          'Lodestar is a local-first life board. Cards have a type '
          + '(question/problem/task/idea/dream/habit) and a life-area category. '
          + 'Call `describe_board` first: the category ids and tags are this '
          + "board's own and cannot be guessed. Cards deleted here go to the "
          + 'Trash and can be restored; the board itself is the only place a '
          + 'card can be destroyed for good.',
      });
    }
    case 'notifications/initialized':
    case 'notifications/cancelled':
      return;
    case 'ping':
      return isNotification ? undefined : reply(id, {});
    case 'tools/list':
      return reply(id, { tools: advertised() });
    case 'tools/call':
      return reply(id, await callTool(params));
    // Answered empty rather than with an error: a client that asks for
    // resources or prompts and is told "method not found" logs a failure on
    // every connection, and this server has neither.
    case 'resources/list':
      return reply(id, { resources: [] });
    case 'prompts/list':
      return reply(id, { prompts: [] });
    default:
      if (isNotification) return;
      return fail(id, -32601, `Method not found: ${method}`);
  }
}

await loadEnv(ROOT);

const lines = createInterface({ input: process.stdin });
for await (const line of lines) {
  const trimmed = line.trim();
  if (!trimmed) continue;
  let msg;
  try {
    msg = JSON.parse(trimmed);
  } catch {
    fail(null, -32700, 'Parse error');
    continue;
  }
  try {
    await handle(msg);
  } catch (err) {
    // The loop outlives any one message: a server that exits on a bad request
    // takes the client's whole session with it.
    process.stderr.write(`lodestar-mcp: ${err.stack || err.message}\n`);
    if (msg?.id !== undefined && msg?.id !== null) fail(msg.id, -32603, 'Internal error');
  }
}
