import { localizedTitle } from '../../../shared/title';
import { envFor, expect, test } from '../fixtures';
import { withPublication } from '../helpers/publication';
import { seedWalletSession } from '../helpers/session';

test(localizedTitle('【开发者应用生命周期 / E2E】 ND-E2E-003 审核通过后发布并出现在市场', 'ND-E2E-003 approved application appears in the marketplace'), async ({ page }) => {
  const env = envFor('node');
  test.skip(!env['NODE_WALLET_PRIVATE_KEY'], 'NODE_WALLET_PRIVATE_KEY not configured');
  await withPublication(async ({ app, approve }) => {
    await approve();
    const base = env.baseURL!;
    await page.goto(base, { waitUntil: 'domcontentloaded' });
    await seedWalletSession(page, base, env['NODE_WALLET_PRIVATE_KEY']!);
    await page.goto(`${base}/market?keyword=${encodeURIComponent(app.name)}`, { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.tab-market-clickable').getByText(app.name, { exact: true })).toBeVisible({ timeout: 15_000 });
  });
});

test(localizedTitle('【应用审核 / E2E】 ND-E2E-004 管理员提交审批并核对结果', 'ND-E2E-004 administrator approves a pending application'), async () => {
  await withPublication(async ({ auditId, readAudit, approve, readApplication }) => {
    expect(await readAudit()).toMatchObject({ meta: { uid: auditId }, summary: { state: '待审批' } });
    await approve();
    expect(await readAudit()).toMatchObject({
      meta: { uid: auditId },
      summary: { state: '审批通过', approvalCount: 1, isDecided: true },
    });
    expect(await readApplication()).toMatchObject({ status: 'BUSINESS_STATUS_ONLINE', isOnline: true });
  });
});

test(localizedTitle('【开发者应用生命周期 / API】 ND-API-023 下线应用恢复为离线', 'ND-API-023 unpublish restores an application to offline and removes it from the market'), async () => {
  await withPublication(async ({ app, approve, readApplication, readMarket, unpublish }) => {
    await approve();
    expect(await readMarket()).toContainEqual(expect.objectContaining({ uid: app.uid, isOnline: true }));
    await unpublish();
    expect(await readApplication()).toMatchObject({ uid: app.uid, status: 'BUSINESS_STATUS_OFFLINE', isOnline: false });
    expect(await readMarket()).not.toContainEqual(expect.objectContaining({ uid: app.uid }));
  });
});
