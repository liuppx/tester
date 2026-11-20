/**
 * router — package purchase cards & the (unrouted) balance status page
 * (RT-UI-024 / RT-UI-025).
 *
 *   - RT-UI-024: the service-pricing page's `#pricing-package-section` mounts
 *     PackagePurchasePage, which renders package cards when plans are available
 *     and an explicit empty state when the environment has no purchasable plans.
 *   - RT-UI-025: DEGRADED SKIP. BalanceStatusPage (`.router-topup-balance-layout`
 *     with the three 充值/兑换/赠送 `.router-topup-statistic` cards + the 兑换码充值
 *     entry) is dead code — no route in web/src/App.jsx mounts it. `/workspace/topup`
 *     always renders QuotaPage via TopUpLayout regardless of `?tab`, so the
 *     balance status layout is unreachable in this build.
 *
 * Selectors verified against router web/src/pages/ServicePricing/index.jsx,
 * web/src/pages/TopUp/PackagePurchasePage.jsx and BalanceStatusPage.jsx.
 */
import { localizedTitle } from '../../../shared/title';
import { test, expect, baseURLFor, envFor } from '../fixtures';
import { seedWalletSession } from '../helpers/session';

function skipIfNoService() {
  test.skip(!baseURLFor('router'), 'ROUTER_BASE_URL not configured');
}

// RT-UI-024 (P2)
test(localizedTitle("【pricing ui / UI】 定价页面展示套餐卡片或无套餐空状态", "the pricing page shows package cards or the empty state"), async ({ page, baseURL, recorder }) => {
  skipIfNoService();
  test.skip(!envFor('router')['ROUTER_WALLET_PRIVATE_KEY'], 'ROUTER_WALLET_PRIVATE_KEY not configured');
  await page.goto(baseURL!, { waitUntil: 'domcontentloaded' });
  await seedWalletSession(page, baseURL!, envFor('router')['ROUTER_WALLET_PRIVATE_KEY']!);

  await page.goto(`${baseURL}/workspace/service/pricing`, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#pricing-package-section')).toBeVisible({ timeout: 15_000 });

  const cards = page.locator('.router-package-purchase-card');
  const emptyState = page.getByText(/No purchasable packages|暂无可购买套餐/);
  await expect
    .poll(async () => (await cards.count()) > 0 || (await emptyState.isVisible()), {
      timeout: 15_000,
    })
    .toBe(true);

  const hasCards = (await cards.count()) > 0;
  const card = cards.first();
  const hasValidPackageSurface = hasCards
    ? (await card.locator('.router-package-purchase-card-title').isVisible()) &&
      (await card.locator('.router-package-purchase-price').isVisible())
    : await emptyState.isVisible();
  expect(hasValidPackageSurface).toBe(true);

  if (hasCards) {
    await recorder.step(page, '套餐购买页展示套餐卡片');
  } else {
    await recorder.step(page, '套餐购买页展示无可购套餐的空状态');
  }
});

// RT-UI-025 (P2) — DEGRADED SKIP: BalanceStatusPage is unrouted dead code.
test(localizedTitle("【余额与兑换码 / UI】 RT-UI-025 余额状态页显示三类余额", "the balance status page shows the three balances (RT-UI-025)"), async () => {
  test.skip(
    true,
    'BalanceStatusPage (`.router-topup-balance-layout`) is not wired into web/src/App.jsx — ' +
      '/workspace/topup always renders QuotaPage via TopUpLayout regardless of ?tab, so the ' +
      'three-balance status layout is unreachable in this build',
  );
});
