import { execFileSync } from 'node:child_process';
import { setTimeout as wait } from 'node:timers/promises';
import { randomUUID } from 'node:crypto';
import { createServer, type Server, type ServerResponse } from 'node:http';
import { Wallet } from 'ethers';
import type { APIRequestContext, APIResponse } from '@playwright/test';
import { apiContext } from '../../../shared/api';
import { activeTestEnv, baseURLFor, requireEnv } from '../../../shared/env';
import { loginWithWallet } from './auth';

export const responsesPath = '/v1/responses';
const upstreamModel = 'gpt-5.4';
type UpstreamChannel = 'primary' | 'backup';
const upstreamKeys: Record<UpstreamChannel, string> = {
  primary: 'sk-tester-responses-primary',
  backup: 'sk-tester-responses-backup',
};
export const firstReply = 'TESTER_RESPONSES_FIRST_REPLY';
export const backupReply = 'TESTER_RESPONSES_BACKUP_REPLY';

interface ResponsesFixtureRows {
  userID: string;
  packageID: string;
  groupID: string;
  channelIDs: string[];
}

interface ResponsesBody {
  model?: string;
  input?: unknown;
  previous_response_id?: string | null;
}

export async function listenLocal(server: Server) {
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      server.off('error', reject);
      resolve();
    });
  });
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('Expected a TCP listener');
  return {
    url: `http://127.0.0.1:${address.port}`,
    close: () =>
      new Promise<void>((resolve, reject) => {
        server.close((error) => (error ? reject(error) : resolve()));
        server.closeAllConnections();
      }),
  };
}

function writeResponse(response: ServerResponse, model: string, id: string, text: string) {
  const item = {
    type: 'message',
    id: `msg_${id}`,
    status: 'completed',
    role: 'assistant',
    content: [{ type: 'output_text', text, annotations: [] }],
  };
  const result = {
    id: `resp_${id}`,
    object: 'response',
    created_at: 1,
    status: 'completed',
    model,
    output: [item],
    usage: { input_tokens: 4, output_tokens: 2, total_tokens: 6 },
  };
  response.writeHead(200, { 'content-type': 'text/event-stream' });
  for (const event of [
    { type: 'response.created', response: { ...result, status: 'in_progress', output: [] } },
    { type: 'response.output_item.added', output_index: 0, item: { ...item, content: [] } },
    { type: 'response.output_text.delta', output_index: 0, content_index: 0, delta: text },
    { type: 'response.output_item.done', output_index: 0, item },
    { type: 'response.completed', response: result },
  ]) {
    response.write(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
  }
  response.end('data: [DONE]\n\n');
}

async function startUpstream() {
  const calls: Array<{ channel: UpstreamChannel; body: ResponsesBody; responseID: string }> = [];
  let primaryExhausted = false;
  let rejectedBackupItem: string | undefined;
  const server = createServer(async (request, response) => {
    if (request.method === 'GET' && request.url === '/v1/models') {
      response.writeHead(200, { 'content-type': 'application/json' }).end(
        JSON.stringify({
          object: 'list',
          data: [{ id: upstreamModel }],
        }),
      );
      return;
    }
    if (request.method !== 'POST' || request.url !== responsesPath) {
      response.writeHead(404).end();
      return;
    }
    const chunks: Buffer[] = [];
    let body: ResponsesBody;
    try {
      for await (const chunk of request) chunks.push(Buffer.from(chunk));
      body = JSON.parse(Buffer.concat(chunks).toString()) as ResponsesBody;
    } catch {
      response.writeHead(400).end();
      return;
    }
    const auth = request.headers.authorization;
    const channel =
      auth === `Bearer ${upstreamKeys.primary}`
        ? 'primary'
        : auth === `Bearer ${upstreamKeys.backup}`
          ? 'backup'
          : undefined;
    if (!channel || body.model !== upstreamModel) {
      response.writeHead(400).end('Unexpected upstream channel or model');
      return;
    }

    const id = randomUUID().replaceAll('-', '');
    calls.push({ channel, body, responseID: `resp_${id}` });
    if (channel === 'primary' && primaryExhausted) {
      response.writeHead(429, { 'content-type': 'application/json' }).end(
        JSON.stringify({
          error: {
            type: 'insufficient_quota',
            code: 'insufficient_quota',
            message: 'Test upstream quota exhausted',
          },
        }),
      );
      return;
    }
    if (
      channel === 'backup' &&
      rejectedBackupItem &&
      Array.isArray(body.input) &&
      body.input.some((item) => item?.id === rejectedBackupItem)
    ) {
      response.writeHead(400, { 'content-type': 'application/json' }).end(
        JSON.stringify({
          error: {
            type: 'invalid_request_error',
            message: `Invalid 'input .id': '${rejectedBackupItem}'. Expected an ID that begins with 'ctc'.`,
          },
        }),
      );
      return;
    }
    writeResponse(response, upstreamModel, id, channel === 'primary' ? firstReply : backupReply);
  });
  const listener = await listenLocal(server);
  return {
    ...listener,
    calls,
    rejectBackupItem: (id: string) => {
      rejectedBackupItem = id;
    },
    exhaustPrimary: () => {
      primaryExhausted = true;
    },
  };
}

async function successful<T>(response: APIResponse, label: string): Promise<T> {
  const body = (await response.json()) as { success?: boolean; data: T; message?: string };
  if (!response.ok() || !body.success) {
    throw new Error(`${label}: HTTP ${response.status()} ${body.message ?? ''}`);
  }
  return body.data;
}

async function waitForTask(admin: APIRequestContext, taskID: string) {
  const deadline = Date.now() + 15_000;
  do {
    const task = await successful<{ status: string; error_message?: string }>(
      await admin.get(`/api/v1/admin/tasks/${taskID}`),
      'Read channel task',
    );
    if (task.status === 'succeeded') return;
    if (task.status === 'failed' || task.status === 'canceled') {
      throw new Error(`Channel task ${task.status}: ${task.error_message ?? ''}`);
    }
    await wait(250);
  } while (Date.now() < deadline);
  throw new Error('Channel task did not complete within 15 seconds');
}

export async function withResponsesFixture(
  run: (fixture: {
    routerURL: string;
    model: string;
    key: string;
    upstream: Awaited<ReturnType<typeof startUpstream>>;
  }) => Promise<void>,
) {
  const routerURL = baseURLFor('router');
  if (!routerURL || (activeTestEnv !== 'test' && activeTestEnv !== 'local')) {
    throw new Error('Responses E2E requires TEST_ENV=test or local, and ROUTER_BASE_URL');
  }
  const env = process.env;
  const allowedURL = env['ROUTER_RESPONSES_E2E_ALLOWED_ROUTER_URL'] ?? '';
  const databaseEngine = env['ROUTER_RESPONSES_E2E_DB_ENGINE'];
  const databaseHost = env['ROUTER_RESPONSES_E2E_DB_HOST'];
  const databasePort = env['ROUTER_RESPONSES_E2E_DB_PORT'];
  const databaseName = env['ROUTER_RESPONSES_E2E_DB_NAME'] ?? '';
  const databaseUser = env['ROUTER_RESPONSES_E2E_DB_USER'];
  const databasePassword = env['ROUTER_RESPONSES_E2E_DB_PASSWORD'];
  if (
    env['ROUTER_RESPONSES_E2E_ENABLED'] !== 'true' ||
    env['ROUTER_RESPONSES_E2E_DATABASE_DISPOSABLE'] !== 'true' ||
    routerURL.replace(/\/+$/, '') !== allowedURL.replace(/\/+$/, '') ||
    !['mysql', 'postgres'].includes(databaseEngine ?? '') ||
    !['127.0.0.1', 'localhost', '::1'].includes(databaseHost ?? '') ||
    !/^tester_router_responses_[a-f0-9]{8}$/.test(databaseName) ||
    !/^\d+$/.test(databasePort ?? '') ||
    !databaseUser ||
    !databasePassword
  ) {
    throw new Error(
      'Responses E2E requires explicit enablement and its exact disposable Router URL',
    );
  }
  const target = new URL(routerURL);
  if (
    !['127.0.0.1', 'localhost'].includes(target.hostname) ||
    target.protocol !== 'http:' ||
    target.pathname !== '/' ||
    target.username ||
    target.password ||
    target.search ||
    target.hash
  ) {
    throw new Error('Responses E2E requires the HTTP origin of a local test Router');
  }

  const adminWallet = await loginWithWallet(routerURL, requireEnv('ROUTER_ADMIN_PRIVATE_KEY'));
  const userWallet = await loginWithWallet(routerURL, Wallet.createRandom().privateKey);
  const admin = await apiContext(routerURL, { Authorization: `Bearer ${adminWallet.token}` });
  const user = await apiContext(routerURL, { Authorization: `Bearer ${userWallet.token}` });
  const upstream = await startUpstream();
  const name = `tester-responses-${randomUUID().slice(0, 8)}`;
  const model = `gpt-5.4-${name}`;
  let tokenID: string | undefined;
  const fixtureRows: ResponsesFixtureRows = {
    userID: userWallet.userId,
    packageID: '',
    groupID: '',
    channelIDs: [],
  };

  try {
    const bindings = [];
    for (const [channelName, priority] of [
      ['primary', 200],
      ['backup', 100],
    ] as const) {
      const channel = await successful<{ id: string }>(
        await admin.post('/api/v1/admin/channel/', {
          data: {
            name: `${name}-${channelName}`,
            key: upstreamKeys[channelName],
            base_url: upstream.url,
            protocol: 'openai',
            status: 1,
            priority,
          },
        }),
        'Create Responses channel',
      );
      fixtureRows.channelIDs.push(channel.id);
      const channelPath = `/api/v1/admin/channel/${channel.id}`;
      const { task } = await successful<{ task: { id: string } }>(
        await admin.post(`${channelPath}/refresh`, { data: { action: 'models' } }),
        'Refresh Responses channel models',
      );
      await waitForTask(admin, task.id);
      await successful(
        await admin.put(`${channelPath}/models`, {
          data: {
            channel_models: [
              {
                model: upstreamModel,
                upstream_model: upstreamModel,
                provider: 'openai',
                type: 'text',
                endpoints: [responsesPath],
                selected: true,
                input_price: 1,
                output_price: 1,
                price_unit: 'per_1k_tokens',
                currency: 'CNY',
              },
            ],
          },
        }),
        'Enable Responses channel model',
      );
      await successful(
        await admin.put(`${channelPath}/endpoints`, {
          data: { model: upstreamModel, endpoint: responsesPath, enabled: true },
        }),
        'Enable Responses channel endpoint',
      );
      const { tasks } = await successful<{ tasks: Array<{ id: string }> }>(
        await admin.post(`${channelPath}/tests`, {
          data: {
            target_configs: [{ model: upstreamModel, endpoint: responsesPath, is_stream: true }],
          },
        }),
        'Verify Responses channel endpoint',
      );
      if (tasks.length !== 1) throw new Error('Expected one Responses verification task');
      await waitForTask(admin, tasks[0]!.id);
      await successful(
        await admin.put(`${channelPath}/models/publish`, {
          data: { model: upstreamModel, publish_enabled: true, published_model: model },
        }),
        'Publish Responses channel model',
      );
      bindings.push({
        model,
        channel_id: channel.id,
        upstream_model: upstreamModel,
        enabled: true,
        priority,
      });
    }

    const group = await successful<{ id: string }>(
      await admin.post('/api/v1/admin/group/', {
        data: {
          name,
          channel_ids: bindings.map((binding) => binding.channel_id),
          models: bindings,
        },
      }),
      'Create Responses group',
    );
    fixtureRows.groupID = group.id;
    const servicePackage = await successful<{ id: string }>(
      await admin.post('/api/v1/admin/package/', {
        data: {
          name,
          group_id: group.id,
          package_type: 'yyc_quota',
          quota_metric: 'yyc',
          period_type: 'package_total',
          period_limit: 1000000000,
          daily_quota_limit: 1000000000,
          duration_days: 1,
          sale_price: 0,
          enabled: true,
          source: 'manual',
        },
      }),
      'Create Responses package',
    );
    fixtureRows.packageID = servicePackage.id;
    await successful(
      await admin.post(`/api/v1/admin/package/${servicePackage.id}/assign`, {
        data: { user_id: userWallet.userId, start_at: 0 },
      }),
      'Assign Responses package',
    );

    const token = await successful<{ id: string; key: string }>(
      await user.post('/api/v1/public/token/', {
        data: {
          name,
          models: model,
          unlimited_quota: true,
          unlimited_request_count: true,
          expired_time: -1,
        },
      }),
      'Create Responses API key',
    );
    tokenID = token.id;
    upstream.calls.length = 0;
    await run({ routerURL, key: token.key, model, upstream });
  } finally {
    try {
      if (tokenID) {
        await successful(
          await user.delete(`/api/v1/public/token/${tokenID}/`),
          'Delete Responses API key',
        );
      }
      cleanupResponsesSubscription(fixtureRows.userID, fixtureRows.packageID, env);
      if (fixtureRows.packageID) {
        await successful(
          await admin.delete(`/api/v1/admin/package/${fixtureRows.packageID}`),
          'Delete Responses package',
        );
      }
      if (fixtureRows.groupID) {
        await successful(
          await admin.delete(`/api/v1/admin/group/${fixtureRows.groupID}`),
          'Delete Responses group',
        );
      }
      for (const channelID of fixtureRows.channelIDs) {
        await successful(
          await admin.delete(`/api/v1/admin/channel/${channelID}`),
          'Delete Responses channel',
        );
      }
    } finally {
      await Promise.all([admin.dispose(), user.dispose(), upstream.close()]);
    }
  }
}

function cleanupResponsesSubscription(userID: string, packageID: string, env: NodeJS.ProcessEnv) {
  if (!packageID) return;
  const engine = env['ROUTER_RESPONSES_E2E_DB_ENGINE'];
  const host = env['ROUTER_RESPONSES_E2E_DB_HOST']!;
  const port = env['ROUTER_RESPONSES_E2E_DB_PORT']!;
  const database = env['ROUTER_RESPONSES_E2E_DB_NAME']!;
  const username = env['ROUTER_RESPONSES_E2E_DB_USER']!;
  const password = env['ROUTER_RESPONSES_E2E_DB_PASSWORD']!;
  if (!/^[a-zA-Z0-9-]+$/.test(userID) || !/^[a-zA-Z0-9-]+$/.test(packageID)) {
    throw new Error('Invalid disposable Responses subscription identity');
  }
  const query = `UPDATE user_package_subscriptions SET status = 4 WHERE user_id = '${userID}' AND package_id = '${packageID}' AND status = 1`;
  if (engine === 'postgres') {
    execFileSync(
      'psql',
      ['-X', '-q', '-h', host, '-p', port, '-U', username, '-d', database, '-c', query],
      {
        env: { ...process.env, PGPASSWORD: password },
        stdio: 'pipe',
      },
    );
    return;
  }
  execFileSync(
    'mysql',
    [
      '--batch',
      '--skip-column-names',
      '-h',
      host,
      '-P',
      port,
      '-u',
      username,
      database,
      '-e',
      query,
    ],
    {
      env: { ...process.env, MYSQL_PWD: password },
      stdio: 'pipe',
    },
  );
}
