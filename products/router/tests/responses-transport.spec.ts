/* eslint-disable playwright/no-skipped-test -- Full relay fixtures require an isolated test Router. */
import { isDeepStrictEqual } from 'node:util';
import { apiContext } from '../../../shared/api';
import { localizedTitle } from '../../../shared/title';
import { baseURLFor, expect, test } from '../fixtures';
import { withResponsesCodex } from '../helpers/responses-codex';
import {
  backupReply,
  firstReply,
  responsesPath,
  withResponsesFixture,
} from '../helpers/responses-fixture';

const upgrade = {
  Connection: 'Upgrade',
  Upgrade: 'websocket',
  'Sec-WebSocket-Version': '13',
  'Sec-WebSocket-Key': 'dGhlIHNhbXBsZSBub25jZQ==',
};

test(
  localizedTitle(
    'RT-API-079 Responses WebSocket 握手返回 426',
    'RT-API-079 Responses WebSocket upgrade returns 426',
  ),
  async () => {
    const url = baseURLFor('router');
    test.skip(!url, 'ROUTER_BASE_URL not configured');
    const ctx = await apiContext(url!);
    try {
      const response = await ctx.get(responsesPath, { headers: upgrade, maxRedirects: 0 });
      expect(response.status()).toBe(426);
      expect(response.headers()).toMatchObject({ allow: 'POST', 'cache-control': 'no-store' });
      expect(await response.json()).toMatchObject({ error: { code: 'websocket_not_supported' } });
    } finally {
      await ctx.dispose();
    }
  },
);

test(
  localizedTitle(
    'RT-API-080 Responses 方法与鉴权',
    'RT-API-080 Responses methods and authentication',
  ),
  async () => {
    const url = baseURLFor('router');
    test.skip(!url, 'ROUTER_BASE_URL not configured');
    const ctx = await apiContext(url!);
    try {
      for (const headers of [{}, { ...upgrade, Connection: 'keep-alive' }]) {
        const response = await ctx.get(responsesPath, { headers, maxRedirects: 0 });
        expect(response.status()).toBe(405);
        expect(response.headers().allow).toBe('POST');
        expect(await response.json()).toMatchObject({ error: { code: 'method_not_allowed' } });
      }
      const authHeaders: Record<string, string>[] = [
        {},
        { Authorization: 'Bearer sk-tester-invalid-key' },
      ];
      for (const headers of authHeaders) {
        const response = await ctx.post(responsesPath, {
          headers,
          data: { model: 'gpt-5.4', input: 'test' },
          maxRedirects: 0,
        });
        expect(response.status()).toBe(401);
        expect(await response.json()).toMatchObject({ error: { message: expect.any(String) } });
      }
    } finally {
      await ctx.dispose();
    }
  },
);

test.describe('Codex Responses relay', () => {
  // Concurrent package creation can deadlock on the isolated Router database.
  test.describe.configure({ mode: 'default', timeout: 120_000 });
  test.skip(
    process.env.ROUTER_RESPONSES_E2E_ENABLED !== 'true',
    'Set ROUTER_RESPONSES_E2E_ENABLED=true for the isolated suite',
  );

  test(
    localizedTitle(
      'RT-E2E-006 Codex 回退 HTTP，续聊时切换额度耗尽的渠道',
      'RT-E2E-006 Codex falls back to HTTP and switches upstream on quota exhaustion while resuming',
    ),
    async ({}, testInfo) => {
      await withResponsesFixture(async (fixture) => {
        const { upstream } = fixture;
        await withResponsesCodex(fixture, true, async (client) => {
          const firstPrompt = 'TESTER_RESPONSES_FIRST_PROMPT';
          const nextPrompt = 'TESTER_RESPONSES_RESUMED_PROMPT';
          const started = await client.send(firstPrompt);
          expect(started.threadID).toMatch(/^[a-f\d-]{36}$/);
          expect(started.output).toContain(firstReply);
          expect(client.requests).toEqual([
            { method: 'GET', status: 426 },
            { method: 'POST', status: 200 },
          ]);
          expect(upstream.calls.map(({ channel }) => channel)).toEqual(['primary']);

          upstream.exhaustPrimary();
          const resumed = await client.send(nextPrompt, started.threadID);
          expect(resumed.threadID).toBe(started.threadID);
          expect(resumed.output).toContain(backupReply);
          expect(client.requests.slice(2)).toEqual([
            { method: 'GET', status: 426 },
            { method: 'POST', status: 200 },
          ]);
          expect(upstream.calls.map(({ channel }) => channel)).toEqual([
            'primary',
            'primary',
            'backup',
          ]);
          const original = upstream.calls[1]!.body;
          const routed = upstream.calls[2]!.body;
          expect(routed.previous_response_id ?? null).toBeNull();
          expect(
            isDeepStrictEqual(routed.input, original.input),
            'failover preserved the input',
          ).toBe(true);
          const firstItemID = upstream.calls[0]!.responseID.replace(/^resp_/, 'msg_');
          const input = routed.input as Array<{
            id?: string;
            role?: string;
            content?: Array<{ text?: string }>;
          }>;
          const expectedMessages = [firstPrompt, firstReply, nextPrompt];
          const messages = input
            .flatMap(({ role, content }) => (content ?? []).map(({ text }) => ({ role, text })))
            .filter(({ text }) => text && expectedMessages.includes(text));
          expect(messages).toEqual([
            { role: 'user', text: firstPrompt },
            { role: 'assistant', text: firstReply },
            { role: 'user', text: nextPrompt },
          ]);
          await testInfo.attach('codex-resumed-item-id', {
            body: JSON.stringify({
              carriesUpstreamAssistantItemID: input.some(({ id }) => id === firstItemID),
            }),
            contentType: 'application/json',
          });
        });
      });
    },
  );

  test(
    localizedTitle(
      'RT-E2E-007 Codex 关闭 WebSocket 后直接使用 HTTP',
      'RT-E2E-007 Codex with WebSockets disabled sends HTTP directly',
    ),
    async () => {
      await withResponsesFixture(async (fixture) => {
        await withResponsesCodex(fixture, false, async (client) => {
          const result = await client.send('TESTER_RESPONSES_HTTP_ONLY');
          expect(result.output).toContain(firstReply);
          expect(client.requests).toEqual([{ method: 'POST', status: 200 }]);
          expect(fixture.upstream.calls.map(({ channel }) => channel)).toEqual(['primary']);
        });
      });
    },
  );

  test(
    localizedTitle(
      'RT-E2E-008 Responses 绑定原上游时维持当前额度失败行为',
      'RT-E2E-008 Responses retains upstream binding on quota failure',
    ),
    async () => {
      await withResponsesFixture(async ({ routerURL, key, model, upstream }) => {
        const ctx = await apiContext(routerURL, { Authorization: `Bearer ${key}` });
        const body = { model, stream: true, store: false };
        try {
          const first = await ctx.post(responsesPath, {
            data: { ...body, input: 'Start the session' },
          });
          expect(first.status()).toBe(200);
          const responseID = upstream.calls[0]!.responseID;
          const bound = await ctx.post(responsesPath, {
            data: { ...body, previous_response_id: responseID, input: 'Continue the session' },
          });
          expect(bound.status()).toBe(200);
          expect(upstream.calls.map(({ channel }) => channel)).toEqual(['primary', 'primary']);
          expect(upstream.calls[1]!.body.previous_response_id).toBe(responseID);

          upstream.exhaustPrimary();
          const pinned = await ctx.post(responsesPath, {
            data: { ...body, previous_response_id: responseID, input: 'Use upstream state' },
          });
          expect(pinned.status()).toBe(503);
          expect(upstream.calls[2]?.body.previous_response_id).toBe(responseID);
          expect(upstream.calls.map(({ channel }) => channel)).toEqual([
            'primary',
            'primary',
            'primary',
          ]);
        } finally {
          await ctx.dispose();
        }
      });
    },
  );

  test(
    localizedTitle(
      'RT-E2E-009 自包含工具历史随 Responses 请求切换渠道',
      'RT-E2E-009 Responses fails over with self-contained local tool history',
    ),
    async () => {
      await withResponsesFixture(async ({ routerURL, key, model, upstream }) => {
        const ctx = await apiContext(routerURL, { Authorization: `Bearer ${key}` });
        try {
          upstream.exhaustPrimary();
          const input = [
            { type: 'message', role: 'user', content: 'Run the local test tool' },
            { type: 'function_call', call_id: 'call_tester', name: 'test_tool', arguments: '{}' },
            { type: 'function_call_output', call_id: 'call_tester', output: 'local test result' },
          ];
          const replay = await ctx.post(responsesPath, {
            data: { model, stream: true, store: false, input },
          });
          expect(replay.status()).toBe(200);
          expect(await replay.text()).toContain(backupReply);
          expect(upstream.calls.map(({ channel }) => channel)).toEqual(['primary', 'backup']);
          for (const call of upstream.calls) expect(call.body.input).toEqual(input);
        } finally {
          await ctx.dispose();
        }
      });
    },
  );

  test(
    localizedTitle(
      'RT-E2E-010 备用渠道拒绝旧 item ID 时返回会话不兼容',
      'RT-E2E-010 Responses reports incompatible state when the backup rejects an upstream item ID',
    ),
    async () => {
      await withResponsesFixture(async ({ routerURL, key, model, upstream }) => {
        const ctx = await apiContext(routerURL, { Authorization: `Bearer ${key}` });
        const body = { model, stream: true, store: false };
        try {
          const first = await ctx.post(responsesPath, {
            data: { ...body, input: 'First test prompt' },
          });
          expect(first.status()).toBe(200);
          const itemID = upstream.calls[0]!.responseID.replace(/^resp_/, 'msg_');
          upstream.exhaustPrimary();
          upstream.rejectBackupItem(itemID);

          const response = await ctx.post(responsesPath, {
            data: {
              ...body,
              input: [
                { type: 'message', role: 'user', content: 'First test prompt' },
                {
                  type: 'message',
                  role: 'assistant',
                  id: itemID,
                  content: [{ type: 'output_text', text: firstReply }],
                },
                { type: 'message', role: 'user', content: 'Continue the test' },
              ],
            },
          });
          expect(upstream.calls.map(({ channel }) => channel)).toEqual([
            'primary',
            'primary',
            'backup',
          ]);
          expect(response.status()).toBe(400);
          expect(await response.json()).toMatchObject({ error: { code: 'state_incompatible' } });
        } finally {
          await ctx.dispose();
        }
      });
    },
  );
});
