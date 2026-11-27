/**
 * node — user-perspective baseline.
 *
 * Minimum user-flow coverage that any node e2e deployment should hit:
 *
 *   1. landing renders the product home (real content, not blank/404)
 *   2. the public sign-in screen offers an auth-method navigation control
 *   3. auth or wallet-connect affordance is discoverable in the DOM
 *
 * All three skip when `NODE_BASE_URL` is unset. When the service comes
 * up, these start passing without code changes — that's the point of a
 * baseline. Deeper node coverage (app directory, SIWE auth, my-apps)
 * lives in `apps.spec.ts`.
 */
import { localizedTitle } from '../../../shared/title';
import { test, expect, baseURLFor } from '../fixtures';

function skipIfNoService() {
  test.skip(!baseURLFor('node'), 'NODE_BASE_URL not configured');
}

test(localizedTitle("【首页与导航 / UI】 ND-UI-001 ND-UI-004 节点控制台首页正常渲染", "ND-UI-001 ND-UI-004 landing renders the node control plane"), async ({ page }) => {
  skipIfNoService();
  await page.goto('/');
  await expect(page.locator('body')).toBeVisible();
  const text = (await page.locator('body').innerText()).trim();
  expect(text.length).toBeGreaterThan(50);
});

test(localizedTitle("【首页与导航 / UI】 ND-UI-002 导航中至少显示一个链接或菜单入口", "ND-UI-002 navigation surfaces at least one link or menu entry"), async ({ page }) => {
  skipIfNoService();
  await page.goto('/');
  // Guests see an email-first auth page. Its tabs are the reachable
  // navigation controls until a session opens the application workspace.
  await expect(page.getByRole('tablist', { name: '登录方式' })).toBeVisible();
  await expect(page.getByRole('tab', { name: '邮箱' })).toBeVisible();
  await expect(page.getByRole('tab', { name: '钱包' })).toBeVisible();
});

test(localizedTitle("【首页与导航 / UI】 ND-UI-003 可找到身份验证或钱包连接入口", "ND-UI-003 auth or wallet-connect affordance is discoverable"), async ({ page }) => {
  skipIfNoService();
  await page.goto('/');
  await expect(page.getByRole('tablist', { name: '登录方式' })).toBeVisible();
  await page.getByRole('tab', { name: '钱包' }).click();
  await expect(page.getByRole('button', { name: '连接钱包并登录' })).toBeVisible();
});
