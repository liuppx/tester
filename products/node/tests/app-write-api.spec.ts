/**
 * node — application write-path authorization & signed-action guards (API).
 *
 * Every mutating application route runs the request body through
 * `executeSignedAction`, which verifies a `personal_sign` envelope before the
 * handler body runs. These API tests exercise that gate and the authorization
 * checks around it, using a fresh throwaway wallet per test so residue lives
 * under a never-reused owner address.
 *
 * Covers:
 *   - ND-API-022 publish an un-approved app → 403 Audit not approved
 *   - ND-API-025 missing / invalid signature envelope → 400 / 401
 *   - ND-API-016 create missing did/version → 400
 *   - ND-API-017 create owner mismatch → 403
 *   - ND-API-018 create without JWT → 401
 *   - ND-API-026 application config PUT → GET round-trip
 *   - ND-API-028 non-admin audit search out of scope → 403
 *
 * Signed-action envelope + payload contracts live in
 * `products/node/helpers/signedAction.ts` (mirrors node
 * `src/routes/public/applications.ts`).
 */
import { randomUUID } from 'crypto';
import { Wallet, getAddress, type BaseWallet } from 'ethers';

import { test, expect, baseURLFor, envFor } from '../fixtures';
import { apiContext } from '../../../shared/api';
import { loginWithWallet } from '../helpers/auth';
import {
  buildConfigUpsertBody,
  buildCreateApplicationBody,
  deleteBody,
  publishBody,
} from '../helpers/signedAction';

function skipIfNoService() {
  test.skip(!baseURLFor('node'), 'NODE_BASE_URL not configured');
}

interface Session {
  wallet: BaseWallet;
  address: string; // checksummed
  ctx: Awaited<ReturnType<typeof apiContext>>;
}

/** Fresh random wallet → real SIWE login → authed API context. */
async function freshSession(baseURL: string): Promise<Session> {
  const wallet = Wallet.createRandom();
  const tokens = await loginWithWallet(baseURL, wallet.privateKey);
  const ctx = await apiContext(baseURL, { Authorization: `Bearer ${tokens.token}` });
  return { wallet, address: getAddress(wallet.address), ctx };
}

async function createApp(s: Session, baseURL: string) {
  const req = await buildCreateApplicationBody(s.wallet, s.address);
  const res = await s.ctx.post('/api/v1/public/applications', { data: req.body });
  const body = (await res.json()) as { code: number; data?: { uid?: string } };
  expect(res.status(), await res.text().catch(() => '')).toBe(200);
  expect(body.code).toBe(0);
  expect(body.data?.uid).toBeTruthy();
  return { uid: body.data!.uid! };
}

test('ND-API-022 publishing an un-approved application is rejected with 403', async () => {
  skipIfNoService();
  test.skip(!envFor('node')['NODE_WALLET_PRIVATE_KEY'], 'node wallet env not configured');
  const baseURL = baseURLFor('node')!;
  const s = await freshSession(baseURL);
  try {
    const { uid } = await createApp(s, baseURL);
    // A valid signature envelope — so this is NOT a signature failure; the
    // publish is blocked purely by the missing approved audit gate.
    const res = await s.ctx.post(`/api/v1/public/applications/${uid}/publish`, {
      data: await publishBody(s.wallet, s.address, uid),
    });
    expect(res.status()).toBe(403);
    const body = (await res.json()) as { code: number; message: string };
    expect(body.message).toBe('Audit not approved');
    // clean up
    await s.ctx.delete(`/api/v1/public/applications/${uid}`, {
      data: await deleteBody(s.wallet, s.address, uid),
    });
  } finally {
    await s.ctx.dispose();
  }
});

test('ND-API-025 write operations reject missing / invalid signature envelopes', async () => {
  skipIfNoService();
  test.skip(!envFor('node')['NODE_WALLET_PRIVATE_KEY'], 'node wallet env not configured');
  const baseURL = baseURLFor('node')!;
  const s = await freshSession(baseURL);
  try {
    const { uid } = await createApp(s, baseURL);

    // publish with no envelope → 400 Missing signature fields
    const noSig = await s.ctx.post(`/api/v1/public/applications/${uid}/publish`, { data: {} });
    expect(noSig.status()).toBe(400);
    expect(((await noSig.json()) as { message: string }).message).toBe('Missing signature fields');

    // publish with a well-formed-but-wrong signature → 401 Invalid signature
    const badSig = await s.ctx.post(`/api/v1/public/applications/${uid}/publish`, {
      data: {
        requestId: randomUUID(),
        timestamp: new Date().toISOString(),
        signature: '0x' + 'ab'.repeat(65),
      },
    });
    expect(badSig.status()).toBe(401);
    expect(((await badSig.json()) as { message: string }).message).toBe('Invalid signature');

    // delete with no envelope → 400 (delete is also a signed action)
    const delNoSig = await s.ctx.delete(`/api/v1/public/applications/${uid}`, { data: {} });
    expect(delNoSig.status()).toBe(400);
    expect(((await delNoSig.json()) as { message: string }).message).toBe('Missing signature fields');

    // clean up with a valid envelope
    await s.ctx.delete(`/api/v1/public/applications/${uid}`, {
      data: await deleteBody(s.wallet, s.address, uid),
    });
  } finally {
    await s.ctx.dispose();
  }
});

test('ND-API-016 create missing did/version returns 400', async () => {
  skipIfNoService();
  test.skip(!envFor('node')['NODE_WALLET_PRIVATE_KEY'], 'node wallet env not configured');
  const baseURL = baseURLFor('node')!;
  const s = await freshSession(baseURL);
  try {
    // owner omitted → defaults to the logged-in address (passes owner check);
    // did/version validation runs before the signature gate.
    const res = await s.ctx.post('/api/v1/public/applications', {
      data: { name: 'no-did', location: 'http://localhost:3020' },
    });
    expect(res.status()).toBe(400);
    expect(((await res.json()) as { message: string }).message).toBe('Missing did or version');
  } finally {
    await s.ctx.dispose();
  }
});

test('ND-API-017 create with mismatched owner returns 403', async () => {
  skipIfNoService();
  test.skip(!envFor('node')['NODE_WALLET_PRIVATE_KEY'], 'node wallet env not configured');
  const baseURL = baseURLFor('node')!;
  const s = await freshSession(baseURL);
  try {
    const other = getAddress(Wallet.createRandom().address);
    const res = await s.ctx.post('/api/v1/public/applications', {
      data: { owner: other, did: `did:e2e:${Date.now()}`, version: 1, location: 'http://localhost:3020' },
    });
    expect(res.status()).toBe(403);
    expect(((await res.json()) as { message: string }).message).toBe('Owner mismatch');
  } finally {
    await s.ctx.dispose();
  }
});

test('ND-API-018 create without a JWT returns 401', async () => {
  skipIfNoService();
  const baseURL = baseURLFor('node')!;
  const ctx = await apiContext(baseURL);
  try {
    const res = await ctx.post('/api/v1/public/applications', {
      data: { did: `did:e2e:${Date.now()}`, version: 1, location: 'http://localhost:3020' },
    });
    expect(res.status()).toBe(401);
    expect(((await res.json()) as { message: string }).message).toBe('Missing access token');
  } finally {
    await ctx.dispose();
  }
});

test('ND-API-026 application config round-trips through PUT then GET', async () => {
  skipIfNoService();
  test.skip(!envFor('node')['NODE_WALLET_PRIVATE_KEY'], 'node wallet env not configured');
  const baseURL = baseURLFor('node')!;
  const s = await freshSession(baseURL);
  try {
    const { uid } = await createApp(s, baseURL);
    const config = [
      { code: 'DOMAIN_A', instance: 'instance-a' },
      { code: 'DOMAIN_B', instance: 'instance-b' },
    ];

    const put = await s.ctx.put(`/api/v1/public/applications/${uid}/config`, {
      data: await buildConfigUpsertBody(s.wallet, s.address, uid, config),
    });
    expect(put.status(), await put.text().catch(() => '')).toBe(200);
    expect(((await put.json()) as { code: number }).code).toBe(0);

    const get = await s.ctx.get(`/api/v1/public/applications/${uid}/config`);
    expect(get.status()).toBe(200);
    const body = (await get.json()) as { data: { config: Array<{ code: string; instance: string }> } };
    expect(body.data.config).toEqual(config);

    await s.ctx.delete(`/api/v1/public/applications/${uid}`, {
      data: await deleteBody(s.wallet, s.address, uid),
    });
  } finally {
    await s.ctx.dispose();
  }
});

test('ND-API-028 non-admin audit search outside own scope returns 403', async () => {
  skipIfNoService();
  test.skip(!envFor('node')['NODE_WALLET_PRIVATE_KEY'], 'node wallet env not configured');
  const baseURL = baseURLFor('node')!;
  const s = await freshSession(baseURL);
  try {
    const other = getAddress(Wallet.createRandom().address);
    const res = await s.ctx.post('/api/v1/public/audits/search', {
      data: { condition: { applicant: other } },
    });
    expect(res.status()).toBe(403);
    expect(((await res.json()) as { message: string }).message).toBe('Audit search scope denied');
  } finally {
    await s.ctx.dispose();
  }
});
