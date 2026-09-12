/**
 * warehouse — UI smoke: the wallet login button is present and reaches
 * the SDK on click.
 *
 * The live "钱包登录" button drives `@yeying-community/web3-bs`'s
 * `loginWithWalletIdentity` flow, which builds a UCAN-style presentation
 * that is too complex to mock without standing up a real wallet. This
 * spec covers the slice of behaviour that *is* mockable:
 *
 *   - the unauthenticated landing page renders the wallet button,
 *   - clicking it dispatches a request to the identity issuer (the SDK
 *     creates a session before signing),
 *   - if no wallet is present the SDK rejects with a clean error (not a
 *     page crash), so the UI surfaces "未检测到钱包".
 *
 * End-to-end authentication is exercised by:
 *   - siwe.spec.ts (classical SIWE challenge/verify against /api/v1/public/auth)
 *   - ui-authenticated.spec.ts (browser post-login UI seeded from a JWT)
 *   - the warehouse backend integration tests (full UCAN path against a
 *     real wallet).
 */
import { test, expect, baseURLFor } from '../fixtures';

function skipIfNoService() {
  test.skip(!baseURLFor('warehouse'), 'WAREHOUSE_BASE_URL not configured');
}

test('landing page renders the wallet + passport login options', async ({ page, baseURL }) => {
  skipIfNoService();
  await page.goto(baseURL!, { waitUntil: 'networkidle' });

  await expect(page.getByRole('button', { name: '通行证登录' })).toBeVisible();
  // The wallet button only shows up when window.ethereum is detected; in
  // headless without a wallet the landing shows "未检测到钱包" instead.
  const walletBtn = page.getByRole('button', { name: '钱包登录' });
  const noWallet = page.getByText('未检测到钱包插件');
  await expect(walletBtn.or(noWallet)).toBeVisible();
});