/* eslint-disable playwright/no-skipped-test -- This mutating relay suite only runs against explicit isolated test fixtures. */
import { createServer, request as httpRequest, type Server } from 'node:http';
import { execFileSync } from 'node:child_process';
import { publishTestChannel } from '../helpers/test-channel';
import { Wallet } from 'ethers';
import { localizedTitle } from '../../../shared/title';
import { activeTestEnv } from '../../../shared/env';
import { test, expect, baseURLFor, envFor } from '../fixtures';
import { apiContext } from '../../../shared/api';
import { loginWithWallet } from '../helpers/auth';

const USER_TOKEN = '/api/v1/public/token';
const AVAILABLE_MODELS = '/api/v1/public/user/models/available';
const CHAT_COMPLETIONS = '/api/v1/public/chat/completions';
const ADMIN_CHANNELS = '/api/v1/admin/channel';
const ADMIN_GROUPS = '/api/v1/admin/group';
const ADMIN_PACKAGES = '/api/v1/admin/package';
const ADMIN_OPTIONS = '/api/v1/admin/option';
const USER_LOGS = '/api/v1/public/log';

interface Envelope<T> {
  success?: boolean;
  message?: string;
  data?: T;
}

interface AvailableModels extends Envelope<string[]> {
  sources?: Array<{ group_id?: string }>;
}

interface UserLogRow {
  id?: string;
  type?: number;
  token_name?: string;
  group_id?: string;
  fallback_count?: number;
  route_decision?: string;
}

interface CreatedToken {
  id?: string;
  key?: string;
  group_route_order?: string[];
  auto_failover?: boolean;
}

interface RelayOutcome {
  status: number;
  marker?: string;
  unauthorizedModelStatus: number;
  autoFailover?: boolean;
  tokenName: string;
  error?: { message?: string; type?: string; code?: string };
  payload?: Record<string, unknown>;
}

interface TestChannelSpec {
  name: string;
  upstreamModel: string;
  priority: number;
}

interface TestGroupFixture {
  groupID: string;
  packageID: string;
  channelsByName: Record<string, string>;
}

interface StubOptions {
  port: number;
  actionsByModel: Record<string, StubAction[]>;
}

type StubAction =
  | { kind: 'success'; marker: string }
  | { kind: 'http_error'; status: number; code: string; message: string }
  | { kind: 'transport_error' }
  | { kind: 'delay'; milliseconds: number; marker: string }
  | { kind: 'stream_success'; marker: string }
  | { kind: 'stream_hold'; marker: string }
  | { kind: 'stream_partial_error'; marker: string };

interface StubRequest {
  model: string;
  body: Record<string, unknown>;
}

interface UpstreamStub {
  server: Server;
  received: StubRequest[];
  actionsByModel: Record<string, StubAction[]>;
  reset: () => void;
  beginRouting: () => void;
}

async function startUpstreamStub(options: StubOptions): Promise<UpstreamStub> {
  let validatingChannels = true;
  const received: StubRequest[] = [];
  const actionIndexes = new Map<string, number>();
  const server = createServer(async (request, response) => {
    if (request.method === 'GET' && request.url === '/v1/models') {
      response.writeHead(200, { 'content-type': 'application/json' });
      response.end(JSON.stringify({ object: 'list', data: [{ id: 'gpt-5.4', object: 'model' }] }));
      return;
    }
    if (request.method !== 'POST' || request.url !== '/v1/chat/completions') {
      response.writeHead(404, { 'content-type': 'application/json' });
      response.end(JSON.stringify({ error: 'not found' }));
      return;
    }

    const chunks: Buffer[] = [];
    for await (const chunk of request) chunks.push(Buffer.from(chunk));

    let payload: Record<string, unknown> = {};
    try {
      payload = JSON.parse(Buffer.concat(chunks).toString('utf8')) as Record<string, unknown>;
    } catch {
      response.writeHead(400, { 'content-type': 'application/json' });
      response.end(JSON.stringify({ error: 'invalid JSON' }));
      return;
    }

    const model = String(request.headers.authorization ?? '').replace(/^Bearer sk-fixture-/, '');
    if (payload.model !== 'gpt-5.4') {
      response.writeHead(400).end();
      return;
    }
    received.push({ model, body: payload });
    const actions = options.actionsByModel[model];
    if (!actions?.length) {
      response.writeHead(404, { 'content-type': 'application/json' });
      response.end(JSON.stringify({ error: `unexpected model ${model}` }));
      return;
    }

    const actionIndex = actionIndexes.get(model) ?? 0;
    const action: StubAction = validatingChannels
      ? { kind: 'success', marker: 'verification ok' }
      : actions[Math.min(actionIndex, actions.length - 1)]!;
    actionIndexes.set(model, actionIndex + 1);
    if (action.kind === 'transport_error') {
      request.socket.destroy();
      return;
    }
    if (action.kind === 'http_error') {
      response.writeHead(action.status, { 'content-type': 'application/json' });
      response.end(
        JSON.stringify({
          error: {
            message: action.message,
            type: 'upstream_error',
            code: action.code,
          },
        }),
      );
      return;
    }
    if (action.kind === 'delay') {
      await new Promise((resolve) => setTimeout(resolve, action.milliseconds));
      if (request.destroyed || response.destroyed) return;
    }

    if (
      action.kind === 'stream_success' ||
      action.kind === 'stream_partial_error' ||
      action.kind === 'stream_hold'
    ) {
      response.writeHead(200, {
        'content-type': 'text/event-stream',
        'cache-control': 'no-cache',
        connection: 'keep-alive',
      });
      response.write(
        `data: ${JSON.stringify({
          id: 'chatcmpl-group-route-stream',
          object: 'chat.completion.chunk',
          model,
          choices: [{ index: 0, delta: { content: action.marker }, finish_reason: null }],
        })}\n\n`,
      );
      if (action.kind === 'stream_partial_error') {
        response.destroy();
        return;
      }
      if (action.kind === 'stream_hold') {
        await new Promise<void>((resolve) => {
          response.on('close', resolve);
        });
        return;
      }
      response.end('data: [DONE]\n\n');
      return;
    }

    response.writeHead(200, { 'content-type': 'application/json' });
    response.end(
      JSON.stringify({
        id: 'chatcmpl-group-route-e2e',
        object: 'chat.completion',
        created: 1,
        model,
        choices: [
          {
            index: 0,
            message: {
              role: 'assistant',
              content: action.marker,
            },
            finish_reason: 'stop',
          },
        ],
        usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
      }),
    );
  });

  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(options.port, '127.0.0.1', () => {
      server.off('error', reject);
      resolve();
    });
  });
  return {
    server,
    received,
    actionsByModel: options.actionsByModel,
    beginRouting: () => {
      validatingChannels = false;
      received.length = 0;
      actionIndexes.clear();
    },
    reset: () => {
      received.length = 0;
      actionIndexes.clear();
    },
  };
}

async function createAdminContext(baseURL: string, privateKey: string) {
  const { token } = await loginWithWallet(baseURL, privateKey);
  return apiContext(baseURL, { Authorization: `Bearer ${token}` });
}

async function setRetryTimes(
  admin: Awaited<ReturnType<typeof createAdminContext>>,
  value: number,
): Promise<void> {
  const response = await admin.put(`${ADMIN_OPTIONS}/`, {
    data: { key: 'RetryTimes', value: String(value) },
  });
  const body = (await response.json()) as Envelope<unknown>;
  expect(body.success, body.message).toBe(true);
}

async function getRetryTimes(
  admin: Awaited<ReturnType<typeof createAdminContext>>,
): Promise<string> {
  const response = await admin.get(`${ADMIN_OPTIONS}/`);
  const body = (await response.json()) as Envelope<Array<{ key: string; value: string }>>;
  expect(body.success, body.message).toBe(true);
  const setting = body.data?.find((item) => item.key === 'RetryTimes');
  expect(setting).toBeTruthy();
  return setting!.value;
}

function executeDisposableDatabaseStatement(query: string): void {
  const env = envFor('router');
  const engine = env['ROUTER_GROUP_ROUTE_E2E_DB_ENGINE'];
  const host = env['ROUTER_GROUP_ROUTE_E2E_DB_HOST'];
  const port = env['ROUTER_GROUP_ROUTE_E2E_DB_PORT'];
  const database = env['ROUTER_GROUP_ROUTE_E2E_DB_NAME'];
  const username = env['ROUTER_GROUP_ROUTE_E2E_DB_USER'];
  const password = env['ROUTER_GROUP_ROUTE_E2E_DB_PASSWORD'];
  expect(['mysql', 'postgres']).toContain(engine);
  expect(['127.0.0.1', 'localhost', '::1']).toContain(host);
  expect(database ?? '').toMatch(/^[a-zA-Z0-9_][a-zA-Z0-9_-]*$/);
  expect(username ?? '').toMatch(/^[a-zA-Z0-9_][a-zA-Z0-9_.-]*$/);
  expect(port ?? '').toMatch(/^\d+$/);
  expect(username).toBeTruthy();
  expect(password).toBeTruthy();
  expect(query).not.toContain(';');
  if (engine === 'mysql') {
    execFileSync(
      'mysql',
      [
        '--batch',
        '--skip-column-names',
        '-h',
        host!,
        '-P',
        port ?? '3306',
        '-u',
        username!,
        database!,
        '-e',
        query,
      ],
      { env: { ...process.env, MYSQL_PWD: password }, stdio: 'pipe' },
    );
    return;
  }
  execFileSync(
    'psql',
    [
      '-X',
      '-q',
      '-t',
      '-h',
      host!,
      '-p',
      port ?? '5432',
      '-U',
      username!,
      '-d',
      database!,
      '-c',
      query,
    ],
    { env: { ...process.env, PGPASSWORD: password }, stdio: 'pipe' },
  );
}

function cancelPackageEntitlement(userID: string, packageID: string): void {
  expect(userID).toMatch(/^[a-zA-Z0-9-]+$/);
  expect(packageID).toMatch(/^[a-zA-Z0-9-]+$/);
  executeDisposableDatabaseStatement(
    `UPDATE user_package_subscriptions SET status = 4 WHERE user_id = '${userID}' AND package_id = '${packageID}' AND status = 1`,
  );
}

function deleteTokenGroupOrder(tokenID: string): void {
  expect(tokenID).toMatch(/^[a-zA-Z0-9-]+$/);
  executeDisposableDatabaseStatement(
    `DELETE FROM api_token_group_routes WHERE token_id = '${tokenID}'`,
  );
}

function parseStreamPayload(raw: string): RelayOutcome['payload'] & {
  choices?: Array<{ delta?: { content?: string } }>;
} {
  const events = raw
    .split(/\r?\n/)
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).trim())
    .filter((line) => line && line !== '[DONE]');
  let marker = '';
  for (const event of events) {
    try {
      const parsed = JSON.parse(event) as {
        choices?: Array<{ delta?: { content?: string } }>;
      };
      marker += parsed.choices?.[0]?.delta?.content ?? '';
    } catch {
      continue;
    }
  }
  return { choices: [{ delta: { content: marker } }] };
}

async function createTestGroup(
  admin: Awaited<ReturnType<typeof createAdminContext>>,
  userIDs: string[],
  baseURL: string,
  name: string,
  logicalModel: string,
  channelSpecs: TestChannelSpec[],
): Promise<TestGroupFixture> {
  const channelIDs: string[] = [];
  const channelsByName: Record<string, string> = {};
  const bindings: Array<Record<string, unknown>> = [];
  for (const channelSpec of channelSpecs) {
    const channelResponse = await admin.post(`${ADMIN_CHANNELS}/`, {
      data: {
        name: `e2e-${name}-${channelSpec.name}-${Date.now()}`,
        protocol: 'openai',
        key: `sk-fixture-${channelSpec.upstreamModel}`,
        base_url: baseURL,
        status: 1,
        priority: channelSpec.priority,
      },
    });
    const channel = (await channelResponse.json()) as Envelope<{ id?: string }>;
    expect(channel.success, channel.message).toBe(true);
    const channelID = String(channel.data?.id ?? '').trim();
    expect(channelID).not.toBe('');
    await publishTestChannel(admin, channelID, 'gpt-5.4', logicalModel, '/v1/chat/completions', false);
    channelIDs.push(channelID);
    channelsByName[channelSpec.name] = channelID;
    bindings.push({
      model: logicalModel,
      channel_id: channelID,
      upstream_model: 'gpt-5.4',
      enabled: true,
      priority: channelSpec.priority,
    });
  }

  const groupResponse = await admin.post(`${ADMIN_GROUPS}/`, {
    data: {
      name: `e2e-${name}-${Date.now()}`,
      description: 'Isolated Router group-routing E2E fixture',
      channel_ids: channelIDs,
      models: bindings,
    },
  });
  const group = (await groupResponse.json()) as Envelope<{ id?: string }>;
  expect(group.success, group.message).toBe(true);
  const groupID = String(group.data?.id ?? '').trim();
  expect(groupID).not.toBe('');

  const packageResponse = await admin.post(`${ADMIN_PACKAGES}/`, {
    data: {
      name: `e2e-${name}-${Date.now()}`,
      description: 'Disposable Router group-routing E2E entitlement',
      group_id: groupID,
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
  });
  const servicePackage = (await packageResponse.json()) as Envelope<{ id?: string }>;
  expect(servicePackage.success, servicePackage.message).toBe(true);
  const packageID = String(servicePackage.data?.id ?? '').trim();
  expect(packageID).not.toBe('');

  for (const userID of userIDs) {
    const assignmentResponse = await admin.post(`${ADMIN_PACKAGES}/${packageID}/assign`, {
      data: { user_id: userID, start_at: 0 },
    });
    const assignment = (await assignmentResponse.json()) as Envelope<unknown>;
    expect(assignment.success, assignment.message).toBe(true);
  }
  return { groupID, packageID, channelsByName };
}

async function removeTestGroups(
  admin: Awaited<ReturnType<typeof createAdminContext>>,
  fixtures: TestGroupFixture[],
  userIDs: string[],
): Promise<void> {
  for (const fixture of fixtures) {
    for (const userID of userIDs) {
      cancelPackageEntitlement(userID, fixture.packageID);
      expect(fixture.groupID).toMatch(/^[a-zA-Z0-9-]+$/);
      const column = envFor('router').ROUTER_GROUP_ROUTE_E2E_DB_ENGINE === 'postgres' ? '"group"' : '`group`';
      executeDisposableDatabaseStatement(
        `UPDATE users SET ${column} = '' WHERE id = '${userID}' AND ${column} = '${fixture.groupID}'`,
      );
    }
    const packageDelete = (await (
      await admin.delete(`${ADMIN_PACKAGES}/${fixture.packageID}`)
    ).json()) as Envelope<unknown>;
    expect(packageDelete.success, packageDelete.message).toBe(true);
  }
  for (const fixture of fixtures) {
    const groupDelete = (await (
      await admin.delete(`${ADMIN_GROUPS}/${fixture.groupID}`)
    ).json()) as Envelope<unknown>;
    expect(groupDelete.success, groupDelete.message).toBe(true);
    for (const channelID of Object.values(fixture.channelsByName)) {
      const channelDelete = (await (
        await admin.delete(`${ADMIN_CHANNELS}/${channelID}`)
      ).json()) as Envelope<unknown>;
      expect(channelDelete.success, channelDelete.message).toBe(true);
    }
  }
}

async function requestWithGroupOrder(
  ctx: Awaited<ReturnType<typeof apiContext>>,
  order: string[],
  model: string,
  options: {
    automaticFailover?: boolean;
    requestModel?: string;
    messages?: Array<Record<string, unknown>>;
    rawBody?: string;
    specificChannelID?: string;
    remainQuota?: number;
    unlimitedQuota?: boolean;
    tokenName?: string;
    stream?: boolean;
    beforeRelay?: (tokenID: string) => Promise<void>;
  } = {},
): Promise<RelayOutcome> {
  const tokenName = options.tokenName ?? `e2e-group-route-${Date.now()}`;
  const created = (await (
    await ctx.post(`${USER_TOKEN}/`, {
      data: {
        name: tokenName,
        remain_quota: options.remainQuota ?? 0,
        unlimited_quota: options.unlimitedQuota ?? options.remainQuota === undefined,
        unlimited_request_count: true,
        expired_time: -1,
        models: model,
        group_route_order: order,
        ...(options.automaticFailover === undefined
          ? {}
          : { auto_failover: options.automaticFailover }),
      },
    })
  ).json()) as Envelope<CreatedToken>;
  expect(created.success, created.message).toBe(true);
  const tokenID = String(created.data?.id ?? '').trim();
  let relay: Awaited<ReturnType<typeof apiContext>> | undefined;
  try {
    expect(tokenID).not.toBe('');
    expect(created.data?.key).toBeTruthy();
    expect(created.data?.group_route_order).toEqual(order);
    await options.beforeRelay?.(tokenID);
    const authKey = options.specificChannelID
      ? `${created.data!.key}-${options.specificChannelID}`
      : created.data!.key!;
    relay = await apiContext(baseURLFor('router')!, { Authorization: `Bearer ${authKey}` });
    const requestOptions = options.rawBody
      ? { data: options.rawBody, headers: { 'content-type': 'application/json' } }
      : {
          data: {
            model: options.requestModel ?? model,
            messages: options.messages ?? [{ role: 'user', content: 'Return the test marker.' }],
            ...(options.stream ? { stream: true } : {}),
          },
        };
    const response = await relay.post(CHAT_COMPLETIONS, requestOptions);
    let payload: {
      choices?: Array<{ message?: { content?: string }; delta?: { content?: string } }>;
      error?: { message?: string; type?: string; code?: string };
      [key: string]: unknown;
    } = {};
    try {
      const raw = await response.text();
      payload = options.stream ? parseStreamPayload(raw) : (JSON.parse(raw) as typeof payload);
    } catch {
      payload = {};
    }
    let unauthorizedModelStatus = 0;
    if (!options.requestModel || options.requestModel === model) {
      const unauthorized = await relay.post(CHAT_COMPLETIONS, {
        data: {
          model: `${model}-outside-token-scope`,
          messages: [{ role: 'user', content: 'This model is not authorized by the token.' }],
        },
      });
      unauthorizedModelStatus = unauthorized.status();
    }
    return {
      status: response.status(),
      marker: payload.choices?.[0]?.message?.content ?? payload.choices?.[0]?.delta?.content,
      unauthorizedModelStatus,
      tokenName,
      autoFailover: created.data?.auto_failover,
      error: payload.error,
      payload,
    };
  } finally {
    if (relay) await relay.dispose();
    if (tokenID) await ctx.delete(`${USER_TOKEN}/${tokenID}/`).catch(() => {});
  }
}

async function listUserLogs(
  ctx: Awaited<ReturnType<typeof apiContext>>,
  tokenName: string,
): Promise<UserLogRow[]> {
  const url = new URL(USER_LOGS, 'http://tester.invalid');
  url.searchParams.set('type', '2');
  url.searchParams.set('page', '1');
  url.searchParams.set('page_size', '100');
  url.searchParams.set('token_name', tokenName);
  const response = await ctx.get(`${url.pathname}${url.search}`);
  const body = (await response.json()) as Envelope<UserLogRow[]>;
  expect(body.success, body.message).toBe(true);
  return body.data ?? [];
}

async function waitForUserLogs(
  ctx: Awaited<ReturnType<typeof apiContext>>,
  tokenName: string,
  predicate: (rows: UserLogRow[]) => boolean,
): Promise<UserLogRow[]> {
  let rows: UserLogRow[] = [];
  for (let attempt = 0; attempt < 20; attempt += 1) {
    rows = await listUserLogs(ctx, tokenName);
    if (predicate(rows)) return rows;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  return rows;
}

async function abortStreamingRequestAfterFirstChunk(
  baseURL: string,
  apiKey: string,
  model: string,
) {
  return new Promise<{ receivedChunk: boolean; statusCode?: number }>((resolve, reject) => {
    const url = new URL(CHAT_COMPLETIONS, baseURL);
    const request = httpRequest(
      url,
      {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${apiKey}`,
          'Content-Type': 'application/json',
        },
      },
      (response) => {
        let receivedChunk = false;
        response.on('data', () => {
          if (receivedChunk) return;
          receivedChunk = true;
          request.destroy();
          resolve({ receivedChunk, statusCode: response.statusCode });
        });
        response.on('end', () => {
          if (!receivedChunk) resolve({ receivedChunk, statusCode: response.statusCode });
        });
      },
    );
    request.setTimeout(10000, () => request.destroy(new Error('stream abort timed out')));
    request.on('error', (error) => {
      if (error.message === 'socket hang up') return;
      reject(error);
    });
    request.end(
      JSON.stringify({
        model,
        stream: true,
        messages: [{ role: 'user', content: 'Return a streaming test marker.' }],
      }),
    );
  });
}

interface GroupRouteScenarioContext {
  admin: Awaited<ReturnType<typeof createAdminContext>>;
  userContext: Awaited<ReturnType<typeof apiContext>>;
  userID: string;
  adminUserID: string;
  logicalModel: string;
  upstreamModel: string;
  firstGroup: string;
  preferredGroup: string;
  noCandidateGroup: string;
  firstGroupFixture: TestGroupFixture;
  preferredGroupFixture: TestGroupFixture;
  noCandidateFixture: TestGroupFixture;
  entitledGroupIDs: string[];
  adminGroupOrder: string[];
  stub: UpstreamStub;
  baseURL: string;
}

async function runRemainingGroupRouteScenarios(context: GroupRouteScenarioContext): Promise<void> {
  const {
    admin,
    userContext,
    userID,
    adminUserID,
    logicalModel,
    upstreamModel,
    firstGroup,
    preferredGroup,
    firstGroupFixture,
    preferredGroupFixture,
    entitledGroupIDs,
    adminGroupOrder,
    stub,
    baseURL,
  } = context;
  const orderAFirst = [
    firstGroup,
    preferredGroup,
    ...entitledGroupIDs.filter((id) => id !== firstGroup && id !== preferredGroup),
  ];
  const orderBFirst = [
    preferredGroup,
    firstGroup,
    ...entitledGroupIDs.filter((id) => id !== firstGroup && id !== preferredGroup),
  ];

  // Legacy Tokens can have no persisted order rows. Removing the order row in
  // the disposable DB must use the documented deterministic entitlement order.
  const legacyOrder = await requestWithGroupOrder(userContext, orderBFirst, logicalModel, {
    tokenName: `gr-legacy-order-${Date.now()}`,
    beforeRelay: async (tokenID) => deleteTokenGroupOrder(tokenID),
  });
  expect.soft(legacyOrder.status).toBe(200);
  expect.soft(legacyOrder.marker).toBe('served-by:group-a-primary');
  expect.soft(stub.received.map((request) => request.model)).toEqual(['group-a-primary']);
  stub.reset();

  // GR-E2E-005. An upstream HTTP 429 within one group should move to the next
  // channel in that group regardless of RetryTimes.
  await setRetryTimes(admin, 0);
  stub.actionsByModel['group-a-primary'] = [
    {
      kind: 'http_error',
      status: 429,
      code: 'rate_limit_exceeded',
      message: 'fixture channel rate limit',
    },
  ];
  stub.actionsByModel['group-a-secondary'] = [
    { kind: 'success', marker: 'served-by:group-a-secondary' },
  ];
  const sameGroupFallback = await requestWithGroupOrder(userContext, orderAFirst, logicalModel, {
    tokenName: `gr005-${Date.now()}`,
  });
  expect.soft(sameGroupFallback.status).toBe(200);
  expect.soft(sameGroupFallback.marker).toBe('served-by:group-a-secondary');
  expect
    .soft(stub.received.map((item) => item.model))
    .toEqual(['group-a-primary', 'group-a-secondary']);
  stub.reset();

  // GR-E2E-006. Exhaust the first group's HTTP-failing candidates, then serve
  // the request from the next entitled group. RetryTimes remains zero.
  stub.actionsByModel['group-a-secondary'] = [
    {
      kind: 'http_error',
      status: 503,
      code: 'upstream_unavailable',
      message: 'fixture unavailable',
    },
  ];
  stub.actionsByModel['group-b-http-error'] = [
    {
      kind: 'http_error',
      status: 429,
      code: 'rate_limit_exceeded',
      message: 'fixture channel rate limit',
    },
  ];
  stub.actionsByModel['group-a-http-error'] = [
    {
      kind: 'http_error',
      status: 503,
      code: 'upstream_unavailable',
      message: 'fixture unavailable',
    },
  ];
  stub.actionsByModel['group-a-transport-error'] = [
    {
      kind: 'http_error',
      status: 503,
      code: 'upstream_unavailable',
      message: 'fixture unavailable',
    },
  ];
  stub.actionsByModel['group-a-stream'] = [
    {
      kind: 'http_error',
      status: 503,
      code: 'upstream_unavailable',
      message: 'fixture unavailable',
    },
  ];
  stub.actionsByModel[upstreamModel] = [
    {
      kind: 'http_error',
      status: 429,
      code: 'rate_limit_exceeded',
      message: 'fixture primary channel rate limit',
    },
  ];
  const crossGroupFallback = await requestWithGroupOrder(userContext, orderAFirst, logicalModel, {
    tokenName: `gr006-${Date.now()}`,
  });
  expect.soft(crossGroupFallback.status).toBe(200);
  expect.soft(crossGroupFallback.marker).toBe('served-by:group-b-secondary');
  expect
    .soft(stub.received.map((item) => item.model))
    .toEqual([
      'group-a-primary',
      'group-a-secondary',
      'group-a-http-error',
      'group-a-transport-error',
      'group-a-stream',
      upstreamModel,
      'group-b-http-error',
      'group-b-secondary',
    ]);
  const crossGroupLogs = await waitForUserLogs(userContext, crossGroupFallback.tokenName, (rows) =>
    rows.some((row) => Number(row.type) === 2),
  );
  const crossGroupSuccesses = crossGroupLogs.filter((row) => Number(row.type) === 2);
  expect.soft(crossGroupSuccesses).toHaveLength(1);
  expect.soft(String(crossGroupSuccesses[0]?.group_id ?? '').trim()).toBe(preferredGroup);
  stub.reset();

  // GR-E2E-007. RetryTimes=0 stops after an explicit transport failure.
  // RetryTimes=1 permits one attempt at the next eligible channel.
  stub.actionsByModel['group-a-primary'] = [{ kind: 'transport_error' }];
  stub.actionsByModel['group-a-secondary'] = [
    { kind: 'success', marker: 'served-by:group-a-secondary' },
  ];
  await setRetryTimes(admin, 0);
  const noTransportRetry = await requestWithGroupOrder(userContext, orderAFirst, logicalModel, {
    tokenName: `gr007-zero-${Date.now()}`,
  });
  expect.soft(noTransportRetry.status).toBeGreaterThanOrEqual(500);
  expect.soft(stub.received.map((item) => item.model)).toEqual(['group-a-primary']);
  stub.reset();
  await setRetryTimes(admin, 1);
  const oneTransportRetry = await requestWithGroupOrder(userContext, orderAFirst, logicalModel, {
    tokenName: `gr007-one-${Date.now()}`,
  });
  expect.soft(oneTransportRetry.status).toBe(200);
  expect.soft(oneTransportRetry.marker).toBe('served-by:group-a-secondary');
  expect
    .soft(stub.received.map((item) => item.model))
    .toEqual(['group-a-primary', 'group-a-secondary']);
  stub.reset();

  // GR-E2E-008. User/token scope, local quota, client input errors, content
  // policy responses and client cancellation must not escape to another group.
  const outsideModel = await requestWithGroupOrder(userContext, orderBFirst, logicalModel, {
    requestModel: `${logicalModel}-outside-token-scope`,
  });
  expect.soft(outsideModel.status).toBeGreaterThanOrEqual(400);
  expect.soft(outsideModel.status).toBeLessThan(500);
  expect.soft(stub.received).toHaveLength(0);

  const noTokenQuota = await requestWithGroupOrder(userContext, orderBFirst, logicalModel, {
    remainQuota: 0,
    unlimitedQuota: false,
  });
  expect.soft(noTokenQuota.status).toBeGreaterThanOrEqual(400);
  expect.soft(stub.received).toHaveLength(0);
  stub.reset();

  const malformedRequest = await requestWithGroupOrder(userContext, orderBFirst, logicalModel, {
    rawBody: '{ this is not valid json',
  });
  expect.soft(malformedRequest.status).toBeGreaterThanOrEqual(400);
  expect.soft(stub.received).toHaveLength(0);

  stub.actionsByModel[upstreamModel] = [
    {
      kind: 'http_error',
      status: 400,
      code: 'content_policy_violation',
      message: 'fixture safety rejection',
    },
  ];
  const contentRejection = await requestWithGroupOrder(userContext, orderBFirst, logicalModel);
  expect.soft(contentRejection.status).toBe(400);
  expect.soft(stub.received.map((item) => item.model)).toEqual([upstreamModel]);
  stub.reset();
  stub.actionsByModel[upstreamModel] = [{ kind: 'success', marker: 'served-by:group-b' }];

  const streamToken = await createTokenWithGroupOrder(userContext, orderBFirst, logicalModel, {
    tokenName: `gr008-client-abort-${Date.now()}`,
  });
  stub.actionsByModel[upstreamModel] = [{ kind: 'stream_hold', marker: 'partial-from-b' }];
  const clientAbort = await abortStreamingRequestAfterFirstChunk(
    baseURL,
    streamToken.key,
    logicalModel,
  );
  expect.soft(clientAbort.receivedChunk).toBe(true);
  expect.soft(stub.received.map((item) => item.model)).toEqual([upstreamModel]);
  await userContext.delete(`${USER_TOKEN}/${streamToken.id}/`).catch(() => {});
  stub.reset();
  stub.actionsByModel[upstreamModel] = [{ kind: 'success', marker: 'served-by:group-b' }];

  // GR-E2E-009. Several failed candidates followed by success produce a
  // single successful settlement in the group that actually served the call.
  stub.actionsByModel['group-a-primary'] = [
    { kind: 'http_error', status: 429, code: 'rate_limit_exceeded', message: 'fixture rate limit' },
  ];
  stub.actionsByModel['group-a-secondary'] = [
    {
      kind: 'http_error',
      status: 503,
      code: 'upstream_unavailable',
      message: 'fixture unavailable',
    },
  ];
  const settledFallback = await requestWithGroupOrder(userContext, orderAFirst, logicalModel, {
    tokenName: `gr009-${Date.now()}`,
  });
  expect.soft(settledFallback.status).toBe(200);
  const settlementLogs = await waitForUserLogs(userContext, settledFallback.tokenName, (rows) =>
    rows.some((row) => Number(row.type) === 2),
  );
  const successRows = settlementLogs.filter((row) => Number(row.type) === 2);
  expect.soft(successRows).toHaveLength(1);
  expect.soft(String(successRows[0]?.group_id ?? '').trim()).toBe(preferredGroup);
  expect.soft(successRows[0]?.fallback_count ?? 0).toBeGreaterThan(0);
  stub.reset();

  // GR-E2E-010. A caller-selected channel is fixed even when its upstream
  // returns an HTTP failure. It must not enter ordered group failover.
  stub.actionsByModel[upstreamModel] = [
    {
      kind: 'http_error',
      status: 503,
      code: 'upstream_unavailable',
      message: 'pinned channel failure',
    },
  ];
  const orderForAdmin = [preferredGroup, ...adminGroupOrder.filter((id) => id !== preferredGroup)];
  const pinned = await requestWithGroupOrder(admin, orderForAdmin, logicalModel, {
    specificChannelID: preferredGroupFixture.channelsByName.primary,
  });
  expect.soft(pinned.status).toBe(503);
  expect.soft(stub.received.map((item) => item.model)).toEqual([upstreamModel]);
  stub.reset();

  // GR-E2E-011. If every entitled candidate reports a channel failure, the
  // relay returns a controlled error and records no successful settlement.
  stub.actionsByModel['group-a-primary'] = [
    {
      kind: 'http_error',
      status: 503,
      code: 'upstream_unavailable',
      message: 'fixture unavailable',
    },
  ];
  stub.actionsByModel['group-a-secondary'] = [
    {
      kind: 'http_error',
      status: 503,
      code: 'upstream_unavailable',
      message: 'fixture unavailable',
    },
  ];
  stub.actionsByModel['group-a-http-error'] = [
    {
      kind: 'http_error',
      status: 503,
      code: 'upstream_unavailable',
      message: 'fixture unavailable',
    },
  ];
  stub.actionsByModel['group-a-transport-error'] = [
    {
      kind: 'http_error',
      status: 503,
      code: 'upstream_unavailable',
      message: 'fixture unavailable',
    },
  ];
  stub.actionsByModel['group-a-stream'] = [
    {
      kind: 'http_error',
      status: 503,
      code: 'upstream_unavailable',
      message: 'fixture unavailable',
    },
  ];
  stub.actionsByModel['group-b-http-error'] = [
    {
      kind: 'http_error',
      status: 503,
      code: 'upstream_unavailable',
      message: 'fixture unavailable',
    },
  ];
  stub.actionsByModel['group-b-secondary'] = [
    {
      kind: 'http_error',
      status: 503,
      code: 'upstream_unavailable',
      message: 'fixture unavailable',
    },
  ];
  stub.actionsByModel['group-b-stream'] = [
    {
      kind: 'http_error',
      status: 503,
      code: 'upstream_unavailable',
      message: 'fixture unavailable',
    },
  ];
  stub.actionsByModel[upstreamModel] = [
    {
      kind: 'http_error',
      status: 503,
      code: 'upstream_unavailable',
      message: 'fixture unavailable',
    },
  ];
  const exhausted = await requestWithGroupOrder(userContext, orderAFirst, logicalModel, {
    tokenName: `gr011-${Date.now()}`,
  });
  expect.soft(exhausted.status).toBeGreaterThanOrEqual(400);
  expect.soft(exhausted.marker).toBeUndefined();
  expect.soft(stub.received.map((item) => item.model)).toHaveLength(9);
  const exhaustedLogs = await waitForUserLogs(
    userContext,
    exhausted.tokenName,
    (rows) => rows.length > 0,
  );
  expect.soft(exhaustedLogs.filter((row) => Number(row.type) === 2)).toHaveLength(0);
  stub.reset();

  // GR-E2E-012. Non-streaming and streaming requests may fail over before any
  // response bytes. Once the client has received a chunk, cancellation stops
  // further upstream attempts.
  stub.actionsByModel['group-a-primary'] = [
    { kind: 'http_error', status: 429, code: 'rate_limit_exceeded', message: 'fixture rate limit' },
  ];
  stub.actionsByModel['group-a-secondary'] = [
    { kind: 'http_error', status: 429, code: 'rate_limit_exceeded', message: 'fixture rate limit' },
  ];
  stub.actionsByModel['group-a-http-error'] = [
    { kind: 'http_error', status: 429, code: 'rate_limit_exceeded', message: 'fixture rate limit' },
  ];
  stub.actionsByModel['group-a-transport-error'] = [
    { kind: 'http_error', status: 429, code: 'rate_limit_exceeded', message: 'fixture rate limit' },
  ];
  stub.actionsByModel['group-a-stream'] = [
    { kind: 'http_error', status: 429, code: 'rate_limit_exceeded', message: 'fixture rate limit' },
  ];
  stub.actionsByModel['group-b-stream'] = [
    { kind: 'stream_success', marker: 'streamed-by:group-b' },
  ];
  stub.actionsByModel['group-b-http-error'] = [
    { kind: 'http_error', status: 429, code: 'rate_limit_exceeded', message: 'fixture rate limit' },
  ];
  stub.actionsByModel['group-b-secondary'] = [
    { kind: 'http_error', status: 429, code: 'rate_limit_exceeded', message: 'fixture rate limit' },
  ];
  stub.actionsByModel[upstreamModel] = [
    { kind: 'http_error', status: 429, code: 'rate_limit_exceeded', message: 'fixture rate limit' },
  ];
  const streamFailover = await requestWithGroupOrder(userContext, orderAFirst, logicalModel, {
    requestModel: logicalModel,
    stream: true,
  });
  expect.soft(streamFailover.status).toBe(200);
  expect.soft(streamFailover.marker).toContain('streamed-by:group-b');
  expect
    .soft(stub.received.map((item) => item.model))
    .toEqual(['group-a-stream', 'group-b-stream']);
  stub.reset();

  // GR-E2E-013. Default and explicit-on enable HTTP error failover. Explicit
  // off returns the first failure without touching the next group.
  stub.actionsByModel['group-b-secondary'] = [
    { kind: 'success', marker: 'served-by:group-b-secondary' },
  ];
  stub.actionsByModel['group-a-primary'] = [
    { kind: 'http_error', status: 429, code: 'rate_limit_exceeded', message: 'fixture rate limit' },
  ];
  const autoDefault = await createTokenWithGroupOrder(userContext, orderAFirst, logicalModel, {
    tokenName: `gr013-default-${Date.now()}`,
  });
  expect.soft(autoDefault.autoFailover).toBe(true);
  await userContext.delete(`${USER_TOKEN}/${autoDefault.id}/`).catch(() => {});
  const autoOn = await requestWithGroupOrder(userContext, orderAFirst, logicalModel, {
    automaticFailover: true,
  });
  expect.soft(autoOn.status).toBe(200);
  expect.soft(stub.received.map((item) => item.model).length).toBeGreaterThan(1);
  stub.reset();
  const autoOff = await requestWithGroupOrder(userContext, orderAFirst, logicalModel, {
    automaticFailover: false,
  });
  expect.soft(autoOff.autoFailover).toBe(false);
  expect.soft(autoOff.status).toBe(429);
  expect.soft(stub.received.map((item) => item.model)).toEqual(['group-a-primary']);

  // GR-E2E-003 runs last because it revokes one package entitlement from the
  // dedicated user and administrator fixtures in the disposable test DB.
  stub.actionsByModel[upstreamModel] = [{ kind: 'success', marker: 'served-by:group-b' }];
  const staleOrderResult = await requestWithGroupOrder(userContext, orderAFirst, logicalModel, {
    tokenName: `gr003-${Date.now()}`,
    beforeRelay: async () => {
      cancelPackageEntitlement(userID, firstGroupFixture.packageID);
      cancelPackageEntitlement(adminUserID, firstGroupFixture.packageID);
    },
  });
  expect.soft(staleOrderResult.status).toBe(200);
  expect.soft(staleOrderResult.marker).toBe('served-by:group-b');
  expect.soft(stub.received.map((item) => item.model)).toEqual([upstreamModel]);
}

async function createTokenWithGroupOrder(
  ctx: Awaited<ReturnType<typeof apiContext>>,
  order: string[],
  model: string,
  options: { automaticFailover?: boolean; remainQuota?: number; tokenName?: string } = {},
): Promise<{ id: string; key: string; name: string; autoFailover?: boolean }> {
  const name = options.tokenName ?? `e2e-group-route-${Date.now()}`;
  const created = (await (
    await ctx.post(`${USER_TOKEN}/`, {
      data: {
        name,
        remain_quota: options.remainQuota ?? 0,
        unlimited_quota: options.remainQuota === undefined,
        unlimited_request_count: true,
        expired_time: -1,
        models: model,
        group_route_order: order,
        ...(options.automaticFailover === undefined
          ? {}
          : { auto_failover: options.automaticFailover }),
      },
    })
  ).json()) as Envelope<CreatedToken>;
  expect(created.success, created.message).toBe(true);
  const id = String(created.data?.id ?? '').trim();
  expect.soft(id).not.toBe('');
  expect.soft(created.data?.key).toBeTruthy();
  expect.soft(created.data?.group_route_order).toEqual(order);
  return { id, key: created.data!.key!, name, autoFailover: created.data?.auto_failover };
}

function normalizedURL(value: string): string {
  return value.trim().replace(/\/+$/, '');
}

test(
  localizedTitle(
    '【分组路由 / E2E】 Token 顺序选择首个可用组并跳过无候选组',
    'group routing follows token order and skips groups without candidates',
  ),
  async () => {
    const env = envFor('router');
    test.setTimeout(180_000);
    const enabled = env['ROUTER_GROUP_ROUTE_E2E_ENABLED'] === 'true';
    test.skip(
      !enabled,
      'Set ROUTER_GROUP_ROUTE_E2E_ENABLED=true for the isolated Router E2E suite',
    );
    const local = activeTestEnv === 'local';
    if (local) expect(env.ROUTER_GROUP_ROUTE_E2E_DB_NAME).toMatch(/^tester_router_e2e_[a-f0-9]{8}$/);
    else expect(activeTestEnv).toBe('test');

    const baseURL = baseURLFor('router');
    const allowedURL = env['ROUTER_GROUP_ROUTE_E2E_ALLOWED_ROUTER_URL'] ?? '';
    expect(baseURL).toBeTruthy();
    expect(allowedURL).not.toBe('');
    expect(normalizedURL(baseURL!)).toBe(normalizedURL(allowedURL));
    expect(['127.0.0.1', 'localhost', '::1']).toContain(new URL(baseURL!).hostname);

    const userPrivateKey = local ? Wallet.createRandom().privateKey : env.ROUTER_GROUP_ROUTE_E2E_USER_PRIVATE_KEY ?? '';
    const adminPrivateKey = (local ? env.ROUTER_ADMIN_PRIVATE_KEY : env.ROUTER_GROUP_ROUTE_E2E_ADMIN_PRIVATE_KEY) ?? '';
    const logicalModel = env.ROUTER_GROUP_ROUTE_E2E_MODEL ?? (local ? 'group-router-fixture' : '');
    const upstreamModel = env.ROUTER_GROUP_ROUTE_E2E_UPSTREAM_MODEL_B ?? (local ? 'group-b-primary' : '');
    const stubPort = Number(env['ROUTER_GROUP_ROUTE_E2E_STUB_PORT'] ?? '39091');
    expect(adminPrivateKey).not.toBe('');
    expect(userPrivateKey).not.toBe('');
    expect(logicalModel).not.toBe('');
    expect(upstreamModel).not.toBe('');
    expect(env['ROUTER_GROUP_ROUTE_E2E_DATABASE_DISPOSABLE']).toBe('true');
    expect(Number.isInteger(stubPort) && stubPort > 0 && stubPort < 65536).toBe(true);

    const mockBaseURL = `http://127.0.0.1:${stubPort}`;
    const stub = await startUpstreamStub({
      port: stubPort,
      actionsByModel: {
        'group-a-primary': [{ kind: 'success', marker: 'served-by:group-a-primary' }],
        'group-a-secondary': [{ kind: 'success', marker: 'served-by:group-a-secondary' }],
        [upstreamModel]: [{ kind: 'success', marker: 'served-by:group-b' }],
        'group-b-secondary': [{ kind: 'success', marker: 'served-by:group-b-secondary' }],
        'group-a-http-error': [
          {
            kind: 'http_error',
            status: 429,
            code: 'rate_limit_exceeded',
            message: 'fixture upstream rate limit',
          },
        ],
        'group-b-http-error': [
          {
            kind: 'http_error',
            status: 429,
            code: 'rate_limit_exceeded',
            message: 'fixture upstream rate limit',
          },
        ],
        'group-a-transport-error': [{ kind: 'transport_error' }],
        'group-b-stream': [{ kind: 'stream_success', marker: 'streamed-by:group-b' }],
        'group-a-stream': [
          {
            kind: 'http_error',
            status: 429,
            code: 'rate_limit_exceeded',
            message: 'fixture stream rate limit',
          },
        ],
      },
    });
    let ctx: Awaited<ReturnType<typeof apiContext>> | undefined;
    let admin: Awaited<ReturnType<typeof createAdminContext>> | undefined;
    let userID = '';
    let adminUserID = '';
    const fixtureGroups: TestGroupFixture[] = [];

    try {
      const user = await loginWithWallet(baseURL!, userPrivateKey);
      const adminIdentity = await loginWithWallet(baseURL!, adminPrivateKey);
      expect(user.userId).not.toBe(adminIdentity.userId);
      userID = user.userId.trim();
      adminUserID = adminIdentity.userId.trim();
      ctx = await apiContext(baseURL!, { Authorization: `Bearer ${user.token}` });
      admin = await createAdminContext(baseURL!, adminPrivateKey);
      const beforeFixture = (await (
        await ctx.get(AVAILABLE_MODELS)
      ).json()) as AvailableModels;
      expect(beforeFixture.success, beforeFixture.message).toBe(true);
      expect(beforeFixture.sources ?? []).toEqual([]);

      const firstGroupFixture = await createTestGroup(
        admin,
        [userID, adminUserID],
        mockBaseURL,
        'group-a',
        logicalModel,
        [
          { name: 'primary', upstreamModel: 'group-a-primary', priority: 300 },
          { name: 'secondary', upstreamModel: 'group-a-secondary', priority: 200 },
          { name: 'http-fallback', upstreamModel: 'group-a-http-error', priority: 100 },
          { name: 'transport-fallback', upstreamModel: 'group-a-transport-error', priority: 50 },
          { name: 'stream', upstreamModel: 'group-a-stream', priority: 25 },
        ],
      );
      const preferredGroupFixture = await createTestGroup(
        admin,
        [userID, adminUserID],
        mockBaseURL,
        'group-b',
        logicalModel,
        [
          { name: 'primary', upstreamModel, priority: 300 },
          { name: 'http-fallback', upstreamModel: 'group-b-http-error', priority: 200 },
          { name: 'group-fallback', upstreamModel: 'group-b-secondary', priority: 150 },
          { name: 'stream', upstreamModel: 'group-b-stream', priority: 100 },
        ],
      );
      const noCandidateFixture = await createTestGroup(
        admin,
        [userID, adminUserID],
        mockBaseURL,
        'group-empty',
        logicalModel,
        [],
      );
      fixtureGroups.push(firstGroupFixture, preferredGroupFixture, noCandidateFixture);
      stub.beginRouting();
      const firstGroup = firstGroupFixture.groupID;
      const preferredGroup = preferredGroupFixture.groupID;
      const noCandidateGroup = noCandidateFixture.groupID;
      const originalRetryTimes = await getRetryTimes(admin);

      const available = (await (
        await ctx.get(AVAILABLE_MODELS)
      ).json()) as AvailableModels;
      expect(available.success, available.message).toBe(true);
      expect(available.data).toContain(logicalModel);
      const entitledGroupIDs = [
        ...new Set(
          (available.sources ?? [])
            .map((source) => String(source.group_id ?? '').trim())
            .filter(Boolean),
        ),
      ];
      expect(entitledGroupIDs).toContain(firstGroup);
      expect(entitledGroupIDs).toContain(preferredGroup);
      expect(entitledGroupIDs).toContain(noCandidateGroup);
      const adminAvailable = (await (
        await admin.get(AVAILABLE_MODELS)
      ).json()) as AvailableModels;
      expect(adminAvailable.success, adminAvailable.message).toBe(true);
      expect(adminAvailable.data).toContain(logicalModel);
      const adminGroupOrder = [
        ...new Set(
          (adminAvailable.sources ?? [])
            .map((source) => String(source.group_id ?? '').trim())
            .filter(Boolean),
        ),
      ];
      expect(adminGroupOrder).toContain(preferredGroup);

      const order = [
        preferredGroup,
        firstGroup,
        noCandidateGroup,
        ...entitledGroupIDs.filter(
          (id) => id !== preferredGroup && id !== firstGroup && id !== noCandidateGroup,
        ),
      ];
      const preferredOrderResult = await requestWithGroupOrder(ctx, order, logicalModel);
      const preferredOrderCalls = stub.received.map((request) => request.model);
      const routeLogs = await listUserLogs(ctx, preferredOrderResult.tokenName);
      const preferredSuccessLog = routeLogs.find((row) => Number(row.type) === 2);
      stub.reset();

      const emptyGroupFirstOrder = [
        noCandidateGroup,
        preferredGroup,
        firstGroup,
        ...entitledGroupIDs.filter(
          (id) => id !== preferredGroup && id !== firstGroup && id !== noCandidateGroup,
        ),
      ];
      const emptyGroupResult = await requestWithGroupOrder(ctx, emptyGroupFirstOrder, logicalModel);
      const emptyGroupCalls = stub.received.map((request) => request.model);
      expect
        .soft({
          preferredOrder: {
            status: preferredOrderResult.status,
            marker: preferredOrderResult.marker,
            unauthorizedModelStatus: preferredOrderResult.unauthorizedModelStatus,
            routeLog: preferredSuccessLog,
            upstreamModels: preferredOrderCalls,
          },
          emptyFirst: {
            status: emptyGroupResult.status,
            marker: emptyGroupResult.marker,
            unauthorizedModelStatus: emptyGroupResult.unauthorizedModelStatus,
            upstreamModels: emptyGroupCalls,
          },
        })
        .toEqual({
          preferredOrder: {
            status: 200,
            marker: 'served-by:group-b',
            unauthorizedModelStatus: expect.any(Number),
            routeLog: expect.objectContaining({ group_id: preferredGroup }),
            upstreamModels: [upstreamModel],
          },
          emptyFirst: {
            status: 200,
            marker: 'served-by:group-b',
            unauthorizedModelStatus: expect.any(Number),
            upstreamModels: [upstreamModel],
          },
        });
      expect.soft(preferredOrderResult.unauthorizedModelStatus).toBeGreaterThanOrEqual(400);
      expect.soft(preferredOrderResult.unauthorizedModelStatus).toBeLessThan(500);
      expect.soft(emptyGroupResult.unauthorizedModelStatus).toBeGreaterThanOrEqual(400);
      expect.soft(emptyGroupResult.unauthorizedModelStatus).toBeLessThan(500);

      try {
        await runRemainingGroupRouteScenarios({
          admin,
          userContext: ctx,
          userID,
          adminUserID,
          logicalModel,
          upstreamModel,
          firstGroup,
          preferredGroup,
          noCandidateGroup,
          firstGroupFixture,
          preferredGroupFixture,
          noCandidateFixture,
          entitledGroupIDs,
          adminGroupOrder,
          stub,
          baseURL: baseURL!,
        });
      } finally {
        await setRetryTimes(admin, Number(originalRetryTimes));
      }
    } finally {
      try {
        if (admin && userID && adminUserID && fixtureGroups.length > 0) {
          await removeTestGroups(admin, fixtureGroups, [userID, adminUserID]);
        }
      } finally {
        if (ctx) await ctx.dispose();
        if (admin) await admin.dispose();
        await new Promise<void>((resolve, reject) => {
          stub.server.close((error) => (error ? reject(error) : resolve()));
        });
      }
    }
  },
);
