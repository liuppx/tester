/**
 * router — top-up order lifecycle API (RT-API-032 / 033 / 034 / 035).
 *
 * DEGRADED SKIP on this deployment.
 *
 * The live router runs `top_up_mode = "api"` (see GET /api/v1/public/status).
 * In that mode `CreateTopupOrderWithDB` (router
 * `internal/admin/model/topup_order.go`) calls
 * `createTopupOrderByExternalPayAPI` — an outbound request that registers the
 * order with the real third-party payment provider (`config.TopUpLink`,
 * e.g. wp.tidukongjian.com). `RefreshTopupOrderStatusWithDB` and
 * `CancelTopupOrderWithDB` likewise query that provider. Creating / refreshing /
 * cancelling real orders there is the external-payment-provider boundary this
 * suite deliberately does not cross (mirroring the stubbing in
 * `topup.spec.ts`). So when `top_up_mode==='api'` these four cases skip.
 *
 * On a `redirect`-mode deployment order creation is purely local (a redirect URL
 * is built without any provider call), so the full create → list → detail →
 * refresh → cancel flow runs for real. That branch is implemented below and
 * self-cleans by cancelling the order it creates.
 */
import { Wallet } from 'ethers';
import { localizedTitle } from '../../../shared/title';
import { test } from '../helpers/topup-catalog';
import { expect, baseURLFor, envFor } from '../fixtures';
import { apiContext } from '../../../shared/api';
import { acquireRouterToken, loginWithWallet } from '../helpers/auth';

function skipIfNoService() {
  test.skip(!baseURLFor('router'), 'ROUTER_BASE_URL not configured');
}
function skipIfNoKey() {
  test.skip(!envFor('router')['ROUTER_WALLET_PRIVATE_KEY'], 'ROUTER_WALLET_PRIVATE_KEY not configured');
}

async function topUpModeIsExternal(baseURL: string): Promise<boolean> {
  const ctx = await apiContext(baseURL);
  try {
    const body = (await (await ctx.get('/api/v1/public/status')).json()) as {
      data?: { top_up_mode?: string };
    };
    return (body.data?.top_up_mode ?? '').toLowerCase() === 'api';
  } finally {
    await ctx.dispose();
  }
}

const EXTERNAL_REASON =
  'top_up_mode=api: creating/refreshing/cancelling orders hits the real external payment provider (out of scope)';

test.describe(localizedTitle("【令牌、充值与余额】充值订单生命周期（串行、自清理）（top-up order lifecycle (serial, self-cleaning)）", "top-up order lifecycle (serial, self-cleaning)"), () => {
  test.describe.configure({ mode: 'serial' });
  let createdOrderId = '';

  test.afterAll(async () => {
    if (!createdOrderId) return;
    const baseURL = baseURLFor('router');
    if (!baseURL) return;
    const { token } = await acquireRouterToken(baseURL);
    const ctx = await apiContext(baseURL, { Authorization: `Bearer ${token}` });
    await ctx.post(`/api/v1/public/user/topup/orders/${createdOrderId}/cancel`).catch(() => {});
    await ctx.dispose();
  });

  // RT-API-032 (P1)
  test(localizedTitle("【令牌、充值与余额 / API】 POST /user/topup/orders 创建 balance_topup 订单", "POST /user/topup/orders creates a balance_topup order"), async ({ catalog }) => {
    skipIfNoService();
    skipIfNoKey();
    const baseURL = baseURLFor('router')!;
    test.skip(await topUpModeIsExternal(baseURL), EXTERNAL_REASON);

    const { token } = await acquireRouterToken(baseURL);
    const ctx = await apiContext(baseURL, { Authorization: `Bearer ${token}` });
    try {
      const plans = (await (await ctx.get('/api/v1/public/user/topup/plans')).json()) as {
        success?: boolean; message?: string; data?: Array<{ id?: string }>;
      };
      expect(plans.success, plans.message).toBe(true);
      const planId = catalog ? plans.data?.find(p => p.id === catalog.planId)?.id : plans.data?.[0]?.id;
      if (catalog) expect(planId).toBe(catalog.planId);
      test.skip(!planId, 'no top-up plan available to order');

      const res = await ctx.post('/api/v1/public/user/topup/orders', {
        data: { business_type: 'balance_topup', plan_id: planId },
      });
      expect(res.status()).toBe(200);
      const body = (await res.json()) as {
        success?: boolean;
        data?: { id?: string; transaction_id?: string; status?: string; redirect_url?: string };
      };
      expect(body.success).toBe(true);
      expect(body.data?.id).toBeTruthy();
      expect(body.data?.transaction_id).toBeTruthy();
      expect(['created', 'pending']).toContain(body.data?.status);
      createdOrderId = String(body.data?.id ?? '');
    } finally {
      await ctx.dispose();
    }
  });

  // RT-API-033 (P1)
  test(localizedTitle("【令牌、充值与余额 / API】 GET /user/topup/orders 列出订单，/orders/:id 返回详细信息", "GET /user/topup/orders lists the order and /orders/:id returns detail"), async () => {
    skipIfNoService();
    skipIfNoKey();
    const baseURL = baseURLFor('router')!;
    test.skip(await topUpModeIsExternal(baseURL), EXTERNAL_REASON);
    test.skip(!createdOrderId, 'no order created in this run');

    const { token } = await acquireRouterToken(baseURL);
    const ctx = await apiContext(baseURL, { Authorization: `Bearer ${token}` });
    try {
      const list = (await (await ctx.get('/api/v1/public/user/topup/orders')).json()) as {
        success?: boolean;
        data?: { items?: Array<{ id?: string }> };
      };
      expect(list.success).toBe(true);
      expect((list.data?.items ?? []).some(o => String(o.id) === createdOrderId)).toBe(true);

      const detail = (await (await ctx.get(`/api/v1/public/user/topup/orders/${createdOrderId}`)).json()) as {
        success?: boolean;
        data?: { id?: string; amount?: number; currency?: string; status?: string };
      };
      expect(detail.success).toBe(true);
      expect(String(detail.data?.id)).toBe(createdOrderId);
      expect(detail.data?.currency).toBeTruthy();
    } finally {
      await ctx.dispose();
    }
  });

  // RT-API-034 (P1)
  test(localizedTitle("【令牌、充值与余额 / API】 POST /user/topup/orders/:id/refresh 返回规范化状态", "POST /user/topup/orders/:id/refresh returns a normalized status"), async () => {
    skipIfNoService();
    skipIfNoKey();
    const baseURL = baseURLFor('router')!;
    test.skip(await topUpModeIsExternal(baseURL), EXTERNAL_REASON);
    test.skip(!createdOrderId, 'no order created in this run');

    const { token } = await acquireRouterToken(baseURL);
    const ctx = await apiContext(baseURL, { Authorization: `Bearer ${token}` });
    try {
      const res = await ctx.post(`/api/v1/public/user/topup/orders/${createdOrderId}/refresh`);
      expect(res.status()).toBe(200);
      const body = (await res.json()) as { success?: boolean; data?: { status?: string } };
      expect(body.success).toBe(true);
      expect(['created', 'pending', 'paid', 'fulfilled', 'canceled', 'failed']).toContain(
        body.data?.status,
      );
    } finally {
      await ctx.dispose();
    }
  });

  // RT-API-035 (P1)
  test(localizedTitle("【令牌、充值与余额 / API】 POST /user/topup/orders/:id/cancel 取消挂单", "POST /user/topup/orders/:id/cancel cancels a pending order"), async () => {
    skipIfNoService();
    skipIfNoKey();
    const baseURL = baseURLFor('router')!;
    test.skip(await topUpModeIsExternal(baseURL), EXTERNAL_REASON);
    test.skip(!createdOrderId, 'no order created in this run');

    const { token } = await acquireRouterToken(baseURL);
    const ctx = await apiContext(baseURL, { Authorization: `Bearer ${token}` });
    try {
      const res = await ctx.post(`/api/v1/public/user/topup/orders/${createdOrderId}/cancel`);
      expect(res.status()).toBe(200);
      const body = (await res.json()) as { success?: boolean; data?: { status?: string } };
      expect(body.success).toBe(true);
      expect(body.data?.status).toBe('canceled');
      createdOrderId = ''; // already canceled — nothing for afterAll to clean.
    } finally {
      await ctx.dispose();
    }
  });
});

test(localizedTitle(
  '【充值与订单生命周期 / API】 RT-API-037 支付回调履约一次，失败订单不入账',
  'RT-API-037 payment callbacks credit a fulfilled order once and leave failed orders uncredited',
), async ({ catalog }) => {
  test.skip(!catalog, 'Requires the local disposable Router and its per-run callback token');
  const baseURL = baseURLFor('router')!;
  const wallet = await loginWithWallet(baseURL, Wallet.createRandom().privateKey);
  const user = await apiContext(baseURL, { Authorization: `Bearer ${wallet.token}` });
  const orders = '/api/v1/public/user/topup/orders';
  const callback = async (id: string, status: string) => {
    const res = await user.post('/api/v1/public/topup/callback', {
      headers: { 'X-Topup-Callback-Token': catalog!.callbackToken },
      data: { order_id: id, status },
    });
    const body = await res.json();
    expect(body.success, body.message).toBe(true);
    expect(body.data.id).toBe(id);
    return body.data.status;
  };
  const create = async () => {
    const body = await (await user.post(orders, {
      data: { business_type: 'balance_topup', plan_id: catalog!.planId },
    })).json();
    expect(body.success, body.message).toBe(true);
    expect(['created', 'pending']).toContain(body.data.status);
    const detail = await (await user.get(`${orders}/${body.data.id}`)).json();
    expect(detail.success, detail.message).toBe(true);
    expect(detail.data.id).toBe(body.data.id);
    expect(detail.data.quota).toBeGreaterThan(0);
    return detail.data;
  };
  const balance = async (): Promise<number> => {
    const body = await (await user.get('/api/v1/public/user/topup/balance/summary')).json();
    expect(body.success, body.message).toBe(true);
    expect(typeof body.data.topup_balance_amount).toBe('number');
    return body.data.topup_balance_amount;
  };
  try {
    const initial = await balance();
    const paid = await create();
    expect(await callback(paid.id, 'pending')).toBe('pending');
    expect(await callback(paid.id, 'paid')).toBe('fulfilled');
    expect(await balance()).toBe(initial + paid.quota);
    expect(await callback(paid.id, 'paid')).toBe('fulfilled');
    expect(await balance()).toBe(initial + paid.quota);
    const failed = await create();
    expect(await callback(failed.id, 'failed')).toBe('failed');
    expect(await balance()).toBe(initial + paid.quota);
  } finally {
    await user.dispose();
  }
});
