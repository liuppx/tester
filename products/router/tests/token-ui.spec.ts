/**
 * router — token-row interactions & the token edit page (RT-UI-022 / RT-UI-023).
 *
 * Both create a dedicated token and clean it up afterward. If the account has
 * no package entitlement, the test helper temporarily adds a synthetic BYOK
 * model; no upstream request is made and no paid package is required.
 *
 * Selectors verified against router web/src/components/TokensTable.jsx and
 * web/src/pages/Token/EditToken.jsx.
 */
import { localizedTitle } from '../../../shared/title';
import { test, expect, baseURLFor, envFor } from '../fixtures';
import { seedWalletSession } from '../helpers/session';
import { createTestToken } from '../helpers/test-token';

function skipIfNoService() {
  test.skip(!baseURLFor('router'), 'ROUTER_BASE_URL not configured');
}
function skipIfNoKey() {
  test.skip(
    !envFor('router')['ROUTER_WALLET_PRIVATE_KEY'],
    'ROUTER_WALLET_PRIVATE_KEY not configured',
  );
}

// RT-UI-022 (P2)
test(localizedTitle("【令牌、充值与余额 / API】 令牌行支持复制和启用/禁用切换", "token row supports copy and enable/disable toggle"), async ({ page, baseURL, recorder }) => {
  skipIfNoService();
  skipIfNoKey();
  const owned = await createTestToken(baseURL!, `e2e-uitoken-${Date.now().toString(36)}`);

  try {
    await page.goto(baseURL!, { waitUntil: 'domcontentloaded' });
    await seedWalletSession(page, baseURL!, envFor('router')['ROUTER_WALLET_PRIVATE_KEY']!);
    await page.context().grantPermissions(['clipboard-read', 'clipboard-write']).catch(() => {});
    await page.goto(`${baseURL}/workspace/token`, { waitUntil: 'domcontentloaded' });

    const table = page.locator('.router-list-table');
    await expect(table).toBeVisible({ timeout: 15_000 });
    const row = table.locator('tbody tr').filter({ hasText: owned.name });
    await expect(row).toBeVisible({ timeout: 15_000 });

    // Copy the token key → success toast appears.
    await row.locator('.router-icon-button').click();
    await expect(page.getByText(/复制成功|copied|Copied|copy/i).first()).toBeVisible({ timeout: 10_000 });
    await recorder.step(page, '令牌复制成功');

    // Toggle the status switch → the PUT (?status_only=true) fires and settles.
    const sw = row.locator('.router-token-status-switch');
    await expect(sw).toBeVisible();
    const [putRes] = await Promise.all([
      page.waitForResponse(
        r => r.url().includes('/api/v1/public/token/') && r.request().method() === 'PUT',
        { timeout: 15_000 },
      ),
      sw.click(),
    ]);
    expect(putRes.ok()).toBe(true);
    await recorder.step(page, '令牌状态切换');

    // Flip it back so the run is side-effect neutral.
    const [resetRes] = await Promise.all([
      page.waitForResponse(
        r => r.url().includes('/api/v1/public/token/') && r.request().method() === 'PUT',
        { timeout: 15_000 },
      ),
      sw.click(),
    ]);
    expect(resetRes.ok()).toBe(true);
  } finally {
    await owned.cleanup();
  }
});

// RT-UI-023 (P2)
test(localizedTitle("【令牌、充值与余额 / UI】 令牌编辑页面保存名称更改", "the token edit page saves name changes"), async ({ page, baseURL, recorder }) => {
  skipIfNoService();
  skipIfNoKey();
  // Mint a DEDICATED token to edit: the account is shared across token specs via
  // ROUTER_WALLET_PRIVATE_KEY, so editing firstToken() races concurrent specs that
  // delete rows (the edit page then re-renders/detaches mid-interaction).
  const owned = await createTestToken(baseURL!, `e2e-uiedit-${Date.now().toString(36)}`);
  const { id } = owned;

  try {
    await page.goto(baseURL!, { waitUntil: 'domcontentloaded' });
    await seedWalletSession(page, baseURL!, envFor('router')['ROUTER_WALLET_PRIVATE_KEY']!);
    await page.goto(`${baseURL}/workspace/token/${id}`, { waitUntil: 'domcontentloaded' });

    // The detail page (EditToken.jsx isDetailMode) opens read-only, split into
    // sections, and loads the token async — loadToken + loadAvailableModels each
    // re-render the basic section, so the edit button can detach mid-click. Wait for
    // the name field to render, then retry the "enter edit mode" step as a unit until
    // the field is actually editable.
    const nameInput = page.getByPlaceholder(/请输入名称|Please enter name/);
    await expect(nameInput).toBeVisible({ timeout: 15_000 });
    await expect(async () => {
      // token.buttons.edit (zh 编辑 / en Edit) switches the basic section into edit mode.
      await page.getByRole('button', { name: /^编辑$|^Edit$/ }).first().click();
      await expect(nameInput).toBeEditable({ timeout: 2_000 });
    }).toPass({ timeout: 15_000 });
    const newName = `e2e-ui-edit-${Date.now()}`;
    await nameInput.fill(newName);

    const [putRes] = await Promise.all([
      page.waitForResponse(
        r => r.url().includes('/api/v1/public/token/') && r.request().method() === 'PUT',
        { timeout: 15_000 },
      ),
      // Save = token.edit.buttons.submit (zh 确认 / en Confirm).
      page.getByRole('button', { name: /^确认$|^Confirm$/ }).first().click(),
    ]);
    const putBody = (await putRes.json()) as { success?: boolean };
    expect(putBody.success).toBe(true);
    await recorder.step(page, '令牌编辑保存');
  } finally {
    // Dedicated token — remove it along with any temporary BYOK entitlement.
    await owned.cleanup();
  }
});
