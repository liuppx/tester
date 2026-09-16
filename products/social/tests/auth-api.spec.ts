/**
 * social — platform auth API (login / register / guard).
 *
 * Covers the email+password auth path and the AuthInterceptor guard on the
 * Spring Boot platform backend (8888). All assertions are against live
 * behaviour verified while authoring these tests.
 *
 * Env:
 *  - SOCIAL_BASE_URL      platform backend (8888). Tests skip when unset.
 *  - SOCIAL_IDENTITY_URL  web3-identity (8901) — used only by SO-API-024 to
 *                         mint a real token via SIWE for /user/self.
 *
 * Contract (live):
 *  - Business envelope: {code, message, data}; success is code === 200.
 *  - Wrong password    -> HTTP 200, {code:10001,"密码不正确"}.
 *  - Protected routes authenticate via custom header `accessToken: <jwt>`.
 *      · missing header      -> {code:400,"未登录"} (NO_LOGIN)
 *      · well-formed but tampered JWT -> {code:401,"token无效或已过期"} (INVALID_TOKEN)
 *      · malformed garbage token      -> {code:500,"系统繁忙"} (parse error)
 */
import { test, expect, baseURLFor, envFor } from '../fixtures';
import { apiContext } from '../../../shared/api';
import {
  registerAndLogin,
  randomEmailCreds,
  siweLogin,
  platformCtx,
  type Envelope,
  type LoginVO,
} from '../helpers/auth';
import { Wallet } from 'ethers';

const platformURL = () => baseURLFor('social');
const identityURL = () => envFor('social')['SOCIAL_IDENTITY_URL'];

function skipIfNoPlatform() {
  test.skip(!platformURL(), 'SOCIAL_BASE_URL not configured');
}

// SO-API-001 — email+password login yields a JWT envelope.
test('SO-API-001 email+password login returns LoginVO', async () => {
  skipIfNoPlatform();
  const { login } = await registerAndLogin(platformURL()!);
  expect(login.code).toBe(200);
  expect(typeof login.data.accessToken).toBe('string');
  expect(login.data.accessToken.length).toBeGreaterThan(20);
  expect(typeof login.data.refreshToken).toBe('string');
  expect(login.data.accessTokenExpiresIn).toBeGreaterThan(0);
  expect(login.data.refreshTokenExpiresIn).toBeGreaterThan(0);
});

// SO-API-002 — wrong password is rejected, no token issued.
test('SO-API-002 wrong password is rejected', async () => {
  skipIfNoPlatform();
  const { creds } = await registerAndLogin(platformURL()!);
  const ctx = await apiContext(platformURL()!);
  try {
    const res = await ctx.post('/login', {
      data: { email: creds.email, password: creds.password + '_wrong', terminal: 0 },
    });
    const body = (await res.json()) as Envelope<LoginVO | null>;
    expect(body.code).not.toBe(200);
    expect(body.data).toBeFalsy();
  } finally {
    await ctx.dispose();
  }
});

// SO-API-003 (P1) — unregistered email is rejected.
test('SO-API-003 unknown email login is rejected', async () => {
  skipIfNoPlatform();
  const ctx = await apiContext(platformURL()!);
  try {
    const email = `nobody_${Date.now()}@test.com`;
    const res = await ctx.post('/login', {
      data: { email, password: 'whatever123', terminal: 0 },
    });
    const body = (await res.json()) as Envelope<LoginVO | null>;
    expect(body.code).not.toBe(200);
    expect(body.data).toBeFalsy();
  } finally {
    await ctx.dispose();
  }
});

// SO-API-004 — register a new user, then log in with it.
test('SO-API-004 register new user then login', async () => {
  skipIfNoPlatform();
  const creds = randomEmailCreds();
  const { register, login } = await registerAndLogin(platformURL()!, creds);
  expect(register.code).toBe(200);
  expect(login.code).toBe(200);
  expect(login.data.accessToken.length).toBeGreaterThan(20);
});

// SO-API-011 — protected route without accessToken header is rejected.
test('SO-API-011 protected route without accessToken is rejected', async () => {
  skipIfNoPlatform();
  const ctx = await apiContext(platformURL()!);
  try {
    const res = await ctx.get('/user/self');
    const body = (await res.json()) as Envelope<unknown>;
    expect(body.code).not.toBe(200); // NO_LOGIN (400)
    expect(body.code).toBe(400);
    expect(body.data).toBeFalsy();
  } finally {
    await ctx.dispose();
  }
});

// SO-API-012 — tampered (well-formed) accessToken is rejected as INVALID_TOKEN.
test('SO-API-012 tampered accessToken is rejected', async () => {
  skipIfNoPlatform();
  test.skip(!identityURL(), 'SOCIAL_IDENTITY_URL not configured');
  // Mint a real JWT, then flip one char of its signature segment so the
  // payload still parses but the HMAC check fails -> INVALID_TOKEN (401).
  const wallet = Wallet.createRandom();
  const { envelope } = await siweLogin(identityURL()!, wallet.privateKey);
  expect(envelope.code).toBe(200);
  const parts = envelope.data.accessToken.split('.');
  const s = parts[2] ?? '';
  const flipped = s.slice(0, -1) + (s.slice(-1) === 'A' ? 'B' : 'A');
  const tampered = `${parts[0]}.${parts[1]}.${flipped}`;

  const ctx = await platformCtx(platformURL()!, tampered);
  try {
    const res = await ctx.get('/user/self');
    const body = (await res.json()) as Envelope<unknown>;
    expect(body.code).toBe(401); // INVALID_TOKEN
    expect(body.data).toBeFalsy();
  } finally {
    await ctx.dispose();
  }
});

// SO-API-024 — GET /user/self returns the current user (via a real SIWE token).
test('SO-API-024 /user/self returns the current user', async () => {
  skipIfNoPlatform();
  test.skip(!identityURL(), 'SOCIAL_IDENTITY_URL not configured');
  const wallet = Wallet.createRandom();
  const { address, envelope } = await siweLogin(identityURL()!, wallet.privateKey);
  expect(envelope.code).toBe(200);

  const ctx = await platformCtx(platformURL()!, envelope.data.accessToken);
  try {
    const res = await ctx.get('/user/self');
    const body = (await res.json()) as Envelope<{ id: number; walletAddress: string }>;
    expect(body.code).toBe(200);
    expect(body.data.id).toBeGreaterThan(0);
    // The auto-provisioned wallet user carries the (lower-cased) address.
    expect(body.data.walletAddress.toLowerCase()).toBe(address.toLowerCase());
  } finally {
    await ctx.dispose();
  }
});
