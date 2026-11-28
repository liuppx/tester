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

test(localizedTitle(
  '【余额与兑换码 / UI】 RT-UI-025 工作台显示总额度、已用和剩余额度',
  'RT-UI-025 the workspace shows total, used and remaining quota',
), async ({ page, baseURL }) => {
  skipIfNoService();
  test.skip(!envFor('router').ROUTER_WALLET_PRIVATE_KEY, 'Router wallet is required');
  await page.goto(baseURL!);
  await seedWalletSession(page, baseURL!);
  await page.evaluate(() => localStorage.setItem('topup_display_currency', 'YYC'));
  const [overview] = await Promise.all([
    page.waitForResponse(r => new URL(r.url()).pathname === '/api/v1/public/user/quota/overview'),
    page.goto('/workspace/topup'),
  ]);
  const body = await overview.json();
  expect(body.success).toBe(true);
  for (const [field, label] of [
    ['total_amount', /总额度|Total Quota/i],
    ['used_amount', /已用额度|Used Quota/i],
    ['remaining_amount', /剩余额度|Remaining Quota/i],
  ] as const) {
    const card = page.locator('.router-quota-summary-item').filter({ hasText: label });
    expect(typeof body.data[field]).toBe('number');
    const amount = body.data[field].toLocaleString('en-US', {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    });
    await expect(card.locator('.router-topup-amount-trigger')).toHaveText(amount + 'YYC');
  }
});
