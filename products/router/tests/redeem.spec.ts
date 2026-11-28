import { randomUUID } from 'node:crypto';
import { localizedTitle } from '../../../shared/title';
import { test, expect, envFor } from '../fixtures';
import { seedWalletSession } from '../helpers/session';

test(localizedTitle(
  '【余额与兑换码 / E2E】 RT-E2E-003 从工作台提交无效兑换码并显示错误',
  'RT-E2E-003 the workspace submits a redemption code and displays its rejection',
), async ({ page, baseURL }) => {
  test.skip(!baseURL || !envFor('router').ROUTER_WALLET_PRIVATE_KEY, 'Router Web and wallet are required');
  await page.goto(baseURL!);
  await seedWalletSession(page, baseURL!);
  await page.goto('/workspace/topup');
  await page.getByRole('button', { name: /^(兑换码|redeem code)$/i }).click();
  const dialog = page.getByRole('dialog');
  const code = randomUUID().replaceAll('-', '');
  await dialog.getByRole('textbox').fill(code);
  const [response] = await Promise.all([
    page.waitForResponse(r => new URL(r.url()).pathname === '/api/v1/public/user/topup'
      && r.request().method() === 'POST'),
    dialog.getByRole('button', { name: /^(确\s*认|confirm)$/i }).click(),
  ]);
  expect(response.request().postDataJSON()).toEqual({ code });
  expect(response.ok()).toBe(true);
  const body = await response.json();
  expect(body.success).toBe(false);
  expect(body.message).toMatch(/无效的兑换码|invalid redemption code/i);
  await expect(page.getByText(body.message, { exact: true }).first()).toBeVisible();
});
