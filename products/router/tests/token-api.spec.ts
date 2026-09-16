/**
 * router — Token CRUD API contract (mutating, self-cleaning).
 *
 * `POST /api/v1/public/token/` is gated by the backend "available models"
 * check *before* it validates the request body — an account with no purchased
 * models is answered `{success:false, message:"当前账号暂无可用模型…"}` (HTTP
 * 200). Purchasing models is the external-payment boundary we don't cross, so:
 *
 *   - RT-API-024: attempt a real create. Funded account → id+key returned, then
 *     deleted in `finally`. Unfunded account → assert the create reached the
 *     models gate (nothing written). Either branch is an honest assertion.
 *   - RT-API-026: a create missing `name` is rejected (param error *or* the
 *     models gate, whichever the backend reaches first).
 *   - RT-API-027: `GET /token/` returns a well-formed paginated list.
 *   - RT-API-030: `GET /token/search` returns a well-formed (possibly empty)
 *     result set for a non-matching keyword.
 *
 * Verified against router `internal/admin/controller/token/*` + api.go routes.
 */
import { test, expect, baseURLFor, envFor } from '../fixtures';
import { apiContext } from '../../../shared/api';
import { acquireRouterToken } from '../helpers/auth';

function skipIfNoService() {
  test.skip(!baseURLFor('router'), 'ROUTER_BASE_URL not configured');
}
function skipIfNoKey() {
  test.skip(!envFor('router')['ROUTER_WALLET_PRIVATE_KEY'], 'ROUTER_WALLET_PRIVATE_KEY not configured');
}

interface CreateResp {
  success?: boolean;
  message?: string;
  data?: { id?: string | number; key?: string };
}

// RT-API-024 (P0)
test('POST /token/ creates a token (or reaches the models gate)', async () => {
  skipIfNoService();
  skipIfNoKey();
  const baseURL = baseURLFor('router')!;
  const { token } = await acquireRouterToken(baseURL);
  const ctx = await apiContext(baseURL, { Authorization: `Bearer ${token}` });
  const name = `e2e-tok-api-${Date.now()}`;
  let createdId = '';
  try {
    const res = await ctx.post('/api/v1/public/token/', { data: { name } });
    expect(res.status()).toBe(200);
    const body = (await res.json()) as CreateResp;

    if (body.success) {
      // Funded account: a fresh id (and one-time key) is returned.
      createdId = String(body.data?.id ?? '');
      expect(createdId).not.toBe('');
      test.info().annotations.push({ type: 'branch', description: 'funded: token created' });
    } else {
      // Unfunded account: the backend gates creation on available models.
      expect(body.message ?? '').toMatch(/暂无可用模型|available model|购买套餐|充值/i);
      test.info().annotations.push({
        type: 'boundary',
        description: 'no available models — create verified to the backend gate only',
      });
    }
  } finally {
    if (createdId) {
      await ctx.delete(`/api/v1/public/token/${createdId}/`).catch(() => {});
    }
    await ctx.dispose();
  }
});

// RT-API-026 (P1)
test('POST /token/ without a name is rejected', async () => {
  skipIfNoService();
  skipIfNoKey();
  const baseURL = baseURLFor('router')!;
  const { token } = await acquireRouterToken(baseURL);
  const ctx = await apiContext(baseURL, { Authorization: `Bearer ${token}` });
  try {
    const res = await ctx.post('/api/v1/public/token/', { data: {} });
    expect(res.status()).toBe(200);
    const body = (await res.json()) as CreateResp;
    // Rejected either by param validation or by the models gate (checked first
    // on an unfunded account). Either way nothing is written and no key leaks.
    expect(body.success).toBe(false);
    expect(body.data?.key).toBeUndefined();
    expect(body.message ?? '').toMatch(/参数|name|名称|暂无可用模型|购买套餐|充值/i);
  } finally {
    await ctx.dispose();
  }
});

// RT-API-027 (P1)
test('GET /token/ returns a well-formed paginated list', async () => {
  skipIfNoService();
  skipIfNoKey();
  const baseURL = baseURLFor('router')!;
  const { token } = await acquireRouterToken(baseURL);
  const ctx = await apiContext(baseURL, { Authorization: `Bearer ${token}` });
  try {
    const res = await ctx.get('/api/v1/public/token/');
    expect(res.status()).toBe(200);
    const body = (await res.json()) as { success?: boolean; data?: unknown; meta?: unknown };
    expect(body.success).toBe(true);
    expect(Array.isArray(body.data)).toBe(true);

    // If any token exists, GET /token/:id returns that token's detail.
    const first = (body.data as Array<{ id?: string | number }>)[0];
    test.skip(!first?.id, 'account has no tokens to fetch a detail for');
    const detail = await ctx.get(`/api/v1/public/token/${first!.id}`);
    expect(detail.status()).toBe(200);
    const detailBody = (await detail.json()) as { success?: boolean; data?: { id?: unknown } };
    expect(detailBody.success).toBe(true);
    expect(String(detailBody.data?.id)).toBe(String(first!.id));
  } finally {
    await ctx.dispose();
  }
});

// RT-API-030 (P2)
test('GET /token/search returns a well-formed result set', async () => {
  skipIfNoService();
  skipIfNoKey();
  const baseURL = baseURLFor('router')!;
  const { token } = await acquireRouterToken(baseURL);
  const ctx = await apiContext(baseURL, { Authorization: `Bearer ${token}` });
  try {
    // A keyword that cannot match any real token → empty but well-formed.
    const res = await ctx.get(`/api/v1/public/token/search?keyword=zzz-no-such-${Date.now()}`);
    expect(res.status()).toBe(200);
    const body = (await res.json()) as { success?: boolean; data?: unknown };
    expect(body.success).toBe(true);
    expect(Array.isArray(body.data)).toBe(true);
    expect((body.data as unknown[]).length).toBe(0);
  } finally {
    await ctx.dispose();
  }
});
