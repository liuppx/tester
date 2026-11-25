import { execFile } from 'node:child_process';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { createServer, request as httpRequest } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { promisify } from 'node:util';
import { listenLocal, responsesPath } from './responses-fixture';

const runFile = promisify(execFile);

export async function withResponsesCodex(
  fixture: { routerURL: string; key: string; model: string },
  supportsWebSockets: boolean,
  run: (client: {
    send: (prompt: string, threadID?: string) => Promise<{ output: string; threadID?: string }>;
    requests: Array<{ method: string; status: number }>;
  }) => Promise<void>,
) {
  const requests: Array<{ method: string; status: number }> = [];
  const routerResponses = new URL(responsesPath, fixture.routerURL);
  const proxy = createServer((incoming, outgoing) => {
    if (incoming.method !== 'POST' || incoming.url !== responsesPath) {
      outgoing.writeHead(404).end();
      return;
    }
    const forwarded = httpRequest(
      routerResponses,
      { method: incoming.method, headers: incoming.headers },
      (reply) => {
        requests.push({ method: 'POST', status: reply.statusCode ?? 0 });
        outgoing.writeHead(reply.statusCode ?? 502, reply.headers);
        reply.pipe(outgoing);
      },
    );
    forwarded.on('error', () => outgoing.destroy());
    outgoing.on('close', () => forwarded.destroy());
    incoming.pipe(forwarded);
  });
  proxy.on('upgrade', (incoming, socket) => {
    if (incoming.url !== responsesPath) {
      socket.end('HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n');
      return;
    }
    const forwarded = httpRequest(
      routerResponses,
      { method: 'GET', headers: incoming.headers },
      (reply) => {
        const status = reply.statusCode ?? 502;
        requests.push({ method: 'GET', status });
        socket.end(
          `HTTP/1.1 ${status} ${reply.statusMessage ?? ''}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n`,
        );
        reply.resume();
      },
    );
    forwarded.on('upgrade', (_reply, upgradedSocket) => {
      requests.push({ method: 'GET', status: 101 });
      upgradedSocket.destroy();
      socket.destroy();
    });
    forwarded.on('error', () => socket.destroy());
    socket.on('error', () => forwarded.destroy());
    socket.on('close', () => forwarded.destroy());
    forwarded.end();
  });

  const local = await listenLocal(proxy);
  const sessionDir = await mkdtemp(join(tmpdir(), 'tester-codex-responses-'));
  try {
    await writeFile(
      join(sessionDir, 'config.toml'),
      `
model = ${JSON.stringify(fixture.model)}
model_provider = "tester"
approval_policy = "never"
sandbox_mode = "read-only"
check_for_update_on_startup = false

[model_providers.tester]
name = "Tester Router"
base_url = "${local.url}/v1"
env_key = "TESTER_CODEX_ROUTER_KEY"
wire_api = "responses"
supports_websockets = ${supportsWebSockets}
request_max_retries = 0
stream_max_retries = 0
`,
    );
    const send = async (prompt: string, threadID?: string) => {
      const args = ['exec', '--json', '--skip-git-repo-check', '-C', sessionDir];
      if (threadID) args.push('resume', threadID);
      args.push(prompt);
      const command = runFile(process.env.ROUTER_RESPONSES_CODEX_BIN || 'codex', args, {
        cwd: sessionDir,
        env: {
          PATH: process.env.PATH,
          HOME: sessionDir,
          CODEX_HOME: sessionDir,
          TESTER_CODEX_ROUTER_KEY: fixture.key,
        },
        timeout: 35_000,
      });
      command.child.stdin?.end();
      const { stdout } = await command;
      const events = stdout
        .trim()
        .split('\n')
        .map(
          (line) =>
            JSON.parse(line) as {
              type: string;
              thread_id?: string;
              item?: { type: string; text?: string };
            },
        );
      return {
        threadID: events.find((event) => event.type === 'thread.started')?.thread_id,
        output: events
          .filter(
            (event) => event.type === 'item.completed' && event.item?.type === 'agent_message',
          )
          .map((event) => event.item?.text ?? '')
          .join('\n'),
      };
    };
    await run({ send, requests });
  } finally {
    await Promise.all([local.close(), rm(sessionDir, { recursive: true, force: true })]);
  }
}
