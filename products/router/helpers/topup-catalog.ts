import { randomUUID } from 'node:crypto';
import { apiContext } from '../../../shared/api';
import { activeTestEnv, baseURLFor } from '../../../shared/env';
import { test as base, expect } from '../fixtures';
import { acquireAdminToken } from './auth';

type Catalog = { planId: string; packageId: string; callbackToken: string };

export const test = base.extend<object, { catalog: Catalog | undefined }>({
  catalog: [async ({}, use) => {
    const callbackToken = process.env.ROUTER_LOCAL_TOPUP_CALLBACK_TOKEN;
    if (!callbackToken) return use(undefined);
    const baseURL = baseURLFor('router')!;
    expect(activeTestEnv).toBe('local');
    expect(baseURL).toBe(process.env.ROUTER_RESPONSES_E2E_ALLOWED_ROUTER_URL);
    expect(new URL(baseURL).hostname).toBe('127.0.0.1');
    expect(process.env.ROUTER_RESPONSES_E2E_DB_NAME).toMatch(/^tester_router_e2e_[a-f0-9]{8}$/);
    const { token } = await acquireAdminToken(baseURL);
    const admin = await apiContext(baseURL, { Authorization: `Bearer ${token}` });
    const name = `tester-checkout-${randomUUID().slice(0, 8)}`;
    const create = async (path: string, data: Record<string, unknown>): Promise<string> => {
      const res = await admin.post(path, { data });
      const body = await res.json();
      expect(body.success, body.message).toBe(true);
      expect(body.data.id).toBeTruthy();
      return body.data.id;
    };
    // Order history retains the catalog references until the batch database is dropped.
    try {
      const group = await create('/api/v1/admin/group/', { name, channel_ids: [], models: [] });
      const planId = await create('/api/v1/admin/topup/plan', {
        name, group_id: group, amount: 1, amount_currency: 'CNY', quota_amount: 10,
        quota_currency: 'USD', enabled: true, public_visible: true,
      });
      const packageId = await create('/api/v1/admin/package/', {
        name, group_id: group, package_type: 'yyc_quota', quota_metric: 'yyc',
        period_type: 'package_total', period_limit: 10000, daily_quota_limit: 10000,
        duration_days: 1, sale_price: 1, enabled: true, public_visible: true, source: 'manual',
      });
      await use({ planId, packageId, callbackToken });
    } finally {
      await admin.dispose();
    }
  }, { scope: 'worker' }],
});
