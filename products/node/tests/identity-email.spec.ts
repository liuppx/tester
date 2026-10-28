/* eslint-disable playwright/no-skipped-test -- These user-data E2E flows require explicit local/test and Mailpit gates. */
import { randomBytes } from 'node:crypto';
import type { APIRequestContext, APIResponse } from '@playwright/test';
import { Wallet } from 'ethers';

import { apiContext } from '../../../shared/api';
import { localizedTitle } from '../../../shared/title';
import { envFor, test, expect } from '../fixtures';
import { makeSelfAssertedIdentity } from '../helpers/identity';
import {
  linkWalletToIdentity,
  requestAndConfirmEmail,
  resolveMailpitUrl,
} from '../helpers/emailIdentity';

function isLoopback(hostname: string): boolean {
  return ['localhost', '127.0.0.1', '::1', '[::1]'].includes(hostname.toLowerCase());
}

function testEndpoints() {
  const env = envFor('node');
  const selectedEnv = process.env.TEST_ENV?.trim().toLowerCase();
  test.skip(
    selectedEnv !== 'local' && selectedEnv !== 'test',
    'Set TEST_ENV=local or TEST_ENV=test to run identity email E2E cases',
  );
  test.skip(
    env['NODE_IDENTITY_EMAIL_E2E_ENABLED']?.trim().toLowerCase() !== 'true',
    'Set NODE_IDENTITY_EMAIL_E2E_ENABLED=true to opt in to identity email E2E cases',
  );

  const apiUrl = env['NODE_API_URL'] ?? env.baseURL;
  test.skip(!apiUrl, 'NODE_API_URL or NODE_BASE_URL is not configured');
  const apiHost = new URL(apiUrl!).hostname.toLowerCase();
  test.skip(
    selectedEnv === 'local' && !isLoopback(apiHost),
    'TEST_ENV=local requires a loopback Node API URL',
  );
  test.skip(
    apiHost === 'node.yeying.pub' || apiHost.endsWith('.node.yeying.pub'),
    'Identity email E2E cases are blocked from the production Node host',
  );

  const mailpitUrl = resolveMailpitUrl(env['MAILPIT_URL']);
  test.skip(
    !isLoopback(new URL(mailpitUrl).hostname),
    'Identity email E2E cases require a loopback Mailpit instance',
  );
  return { apiUrl: apiUrl!, mailpitUrl };
}

async function ensureMailpitAvailable(request: APIRequestContext, mailpitUrl: string) {
  let response: APIResponse;
  try {
    response = await request.get(`${mailpitUrl}/api/v1/search?limit=1`);
  } catch {
    test.skip(true, 'Mailpit is not reachable; identity email E2E cases are skipped');
    throw new Error('Mailpit is not reachable');
  }
  test.skip(!response.ok(), 'Mailpit is not available; identity email E2E cases are skipped');
}

function uniqueEmail(): string {
  return `e2e-${randomBytes(12).toString('hex')}@example.test`;
}

async function ensureNodeAvailable(request: APIRequestContext) {
  let response: APIResponse;
  try {
    response = await request.get('/api/v1/public/health');
  } catch {
    test.skip(true, 'Node API is not reachable; identity email E2E cases are skipped');
    throw new Error('Node API is not reachable');
  }
  expect(response.status()).toBe(200);
  const body = (await response.json()) as { code: number; data: { status: string } };
  expect(body.code).toBe(0);
  expect(body.data.status).toBe('ok');
}

test(
  localizedTitle(
    '【身份邮箱绑定 / API】 ND-API-038 同 DID 多钱包可重验邮箱且普通换绑被拒',
    'ND-API-038 one DID can re-verify its email through another wallet but cannot silently rebind',
  ),
  async () => {
    const { apiUrl, mailpitUrl } = testEndpoints();
    const request = await apiContext(apiUrl);
    try {
      await ensureNodeAvailable(request);
      await ensureMailpitAvailable(request, mailpitUrl);
      const identity = makeSelfAssertedIdentity();
      const firstAccount = await linkWalletToIdentity(
        request,
        identity,
        Wallet.createRandom().privateKey,
      );
      const secondAccount = await linkWalletToIdentity(
        request,
        identity,
        Wallet.createRandom().privateKey,
      );
      const email = uniqueEmail();

      const initial = await requestAndConfirmEmail(
        request,
        mailpitUrl,
        identity,
        firstAccount,
        email,
      );
      expect(initial.status()).toBe(200);
      const initialBody = (await initial.json()) as {
        data: { credentials: Array<{ type: string }> };
      };
      expect(initialBody.data.credentials.map((item) => item.type)).toContain('EmailCredential');

      const renewed = await requestAndConfirmEmail(
        request,
        mailpitUrl,
        identity,
        secondAccount,
        email,
      );
      expect(renewed.status()).toBe(200);

      const replacement = await requestAndConfirmEmail(
        request,
        mailpitUrl,
        identity,
        secondAccount,
        uniqueEmail(),
      );
      expect(replacement.status()).toBe(409);
      expect(((await replacement.json()) as { message: string }).message).toBe(
        'IDENTITY_EMAIL_CHANGE_REQUIRES_AUTHORIZATION',
      );
    } finally {
      await request.dispose();
    }
  },
);

test(
  localizedTitle(
    '【身份邮箱绑定 / API】 ND-API-039 跨 DID 邮箱冲突且无插件注册不能绕过绑定',
    'ND-API-039 another DID and password-only registration cannot reuse a wallet-bound email',
  ),
  async () => {
    const { apiUrl, mailpitUrl } = testEndpoints();
    const request = await apiContext(apiUrl);
    try {
      await ensureNodeAvailable(request);
      await ensureMailpitAvailable(request, mailpitUrl);
      const existingIdentity = makeSelfAssertedIdentity();
      const existingAccount = await linkWalletToIdentity(
        request,
        existingIdentity,
        Wallet.createRandom().privateKey,
      );
      const email = uniqueEmail();
      const initial = await requestAndConfirmEmail(
        request,
        mailpitUrl,
        existingIdentity,
        existingAccount,
        email,
      );
      expect(initial.status()).toBe(200);

      const otherIdentity = makeSelfAssertedIdentity();
      const otherAccount = await linkWalletToIdentity(
        request,
        otherIdentity,
        Wallet.createRandom().privateKey,
      );
      const duplicate = await requestAndConfirmEmail(
        request,
        mailpitUrl,
        otherIdentity,
        otherAccount,
        email,
      );
      expect(duplicate.status()).toBe(409);
      expect(((await duplicate.json()) as { message: string }).message).toBe(
        'IDENTITY_EMAIL_ALREADY_BOUND',
      );

      const registrationIdentity = makeSelfAssertedIdentity();
      const registration = await request.post('/api/v1/public/auth/email/register/request', {
        data: {
          email,
          username: `e2e${randomBytes(8).toString('hex')}`,
          avatar: 'https://example.invalid/avatar.png',
          identityDocument: registrationIdentity.buildDocument(),
        },
      });
      expect(registration.status()).toBe(409);
      expect(((await registration.json()) as { message: string }).message).toBe(
        'IDENTITY_EMAIL_ALREADY_REGISTERED',
      );
    } finally {
      await request.dispose();
    }
  },
);
