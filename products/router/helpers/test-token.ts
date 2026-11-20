import type { APIRequestContext } from '@playwright/test';
import { envFor } from '../../../shared/env';
import { apiContext } from '../../../shared/api';
import { acquireRouterToken } from './auth';

const USER_TOKEN = '/api/v1/public/token';
const PERSONAL_CONNECTIONS = '/api/v1/public/personal/channels/connections';

interface TokenEnvelope {
  success?: boolean;
  message?: string;
  data?: { id?: string | number; key?: string };
}

interface ConnectionEnvelope {
  success?: boolean;
  message?: string;
  data?: { id?: string };
}

export interface TestToken {
  id: string;
  key: string;
  name: string;
  ctx: APIRequestContext;
  cleanup: () => Promise<void>;
}

/**
 * Create an isolated token for E2E assertions. If the shared account has no
 * package entitlement, add a temporary BYOK connection with a synthetic model
 * first. No upstream request is made; the connection is removed with the token.
 */
export async function createTestToken(baseURL: string, name: string): Promise<TestToken> {
  const { token } = await acquireRouterToken(baseURL);
  const ctx = await apiContext(baseURL, { Authorization: `Bearer ${token}` });
  const tokenBody = {
    name,
    unlimited_quota: true,
    unlimited_request_count: true,
    expired_time: -1,
    models: '',
  };
  let tokenId = '';
  let connectionId = '';

  try {
    let create = (await (
      await ctx.post(`${USER_TOKEN}/`, { data: tokenBody })
    ).json()) as TokenEnvelope;

    if (!create.success || create.data?.id == null) {
      if (!/暂无可用模型/.test(create.message ?? '')) {
        throw new Error(`could not create E2E token: ${create.message ?? 'unknown error'}`);
      }

      const model = envFor('router')['ROUTER_PERSONAL_UPSTREAM_MODEL'] ?? 'gpt-4o-mini';
      const connection = (await (
        await ctx.post(PERSONAL_CONNECTIONS, {
          data: {
            name: `e2e-token-entitlement-${Date.now().toString(36)}`,
            protocol: 'openai',
            base_url: '',
            api_key: 'sk-e2e-test-only',
            models: [model],
            status: 1,
          },
        })
      ).json()) as ConnectionEnvelope;
      if (!connection.success || !connection.data?.id) {
        throw new Error(
          `could not create temporary BYOK entitlement: ${connection.message ?? 'unknown error'}`,
        );
      }
      connectionId = connection.data.id.trim();

      create = (await (
        await ctx.post(`${USER_TOKEN}/`, { data: tokenBody })
      ).json()) as TokenEnvelope;
    }

    if (create.data?.id != null) tokenId = String(create.data.id).trim();
    if (!create.success || !tokenId || !create.data?.key) {
      throw new Error(
        `could not create E2E token after entitlement setup: ${create.message ?? 'missing token data'}`,
      );
    }
    const key = create.data.key;

    return {
      id: tokenId,
      key,
      name,
      ctx,
      cleanup: async () => {
        await ctx.delete(`${USER_TOKEN}/${tokenId}/`).catch(() => {});
        if (connectionId) {
          await ctx.delete(`${PERSONAL_CONNECTIONS}/${connectionId}`).catch(() => {});
        }
        await ctx.dispose();
      },
    };
  } catch (error) {
    if (tokenId) await ctx.delete(`${USER_TOKEN}/${tokenId}/`).catch(() => {});
    if (connectionId) await ctx.delete(`${PERSONAL_CONNECTIONS}/${connectionId}`).catch(() => {});
    await ctx.dispose();
    throw error;
  }
}
