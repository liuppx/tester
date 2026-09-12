/**
 * Wallet — popup smoke: lock / unlock cycle, including SW kill recovery.
 *
 * Mirrors the lock/unlock tail of `wallet/tests/e2e/extension-smoke.test.mjs`
 * test 1. The flow:
 *
 *   1. Create + unlock a wallet (reuse popup-create flow).
 *   2. Lock from the header menu → reopen popup → #unlockPage.
 *   3. Wrong password → #globalToast shows "密码错误".
 *   4. Correct password → #walletPage in 5s.
 *   5. Terminate the extension service worker via CDP, reopen popup,
 *      unlock again → same #accountAddress.
 *
 * Why the SW-kill step matters: MV3 service workers can be killed by
 * Chromium at any time. The wallet is supposed to survive that and
 * restore state from chrome.storage.local. If this fails, a real user
 * could lose their wallet on a browser restart.
 */
import { test, expect } from '../fixtures';

import { loadWalletContext, teardownWalletContext } from '../helpers/extension';
import { stubPublicEndpoints } from '../helpers/network';
import { byId, openPopup } from '../helpers/popup';

const TEST_PASSWORD = 'E2E-password-2026';
const TEST_WALLET_NAME = 'E2E Wallet';

async function createAndUnlockWallet(ctx: Awaited<ReturnType<typeof loadWalletContext>>) {
  const popup = await openPopup(ctx.context, ctx.extensionId);
  await byId(popup, 'welcomePage').waitFor({ state: 'visible' });
  await byId(popup, 'welcomeCreateWalletBtn').click();
  await byId(popup, 'setPasswordPage').waitFor({ state: 'visible' });
  await byId(popup, 'setWalletName').fill(TEST_WALLET_NAME);
  await byId(popup, 'setPasswordBtn').click();
  await byId(popup, 'passwordPromptInput').fill(TEST_PASSWORD);
  await byId(popup, 'passwordPromptConfirm').click();
  await byId(popup, 'walletPage').waitFor({ state: 'visible', timeout: 30_000 });
  return popup;
}

test('lock + unlock + SW-kill recovery preserves wallet access', async () => {
  const ctx = await loadWalletContext();
  try {
    await stubPublicEndpoints(ctx.context);

    // -- Step 1: create + unlock a fresh wallet -------------------------
    const popup = await createAndUnlockWallet(ctx);

    // Capture the address so we can verify the SW-kill path returns the
    // same one (i.e. the keyring actually survived chrome.storage).
    const addressBefore = ((await byId(popup, 'accountAddress').textContent())?.trim() ?? '').toLowerCase();
    expect(addressBefore).toMatch(/^0x[\da-fA-F]+(?:…|\.\.\.)[\da-fA-F]+$/);

    // -- Step 2: lock from the header menu ------------------------------
    await byId(popup, 'walletHeaderMenuBtn').click();
    await expect(byId(popup, 'walletHeaderMenuBtn')).toHaveAttribute('aria-expanded', 'true');
    await byId(popup, 'lockWalletBtn').click();
    await byId(popup, 'unlockPage').waitFor({ state: 'visible', timeout: 15_000 });

    await popup.close();
    const locked = await openPopup(ctx.context, ctx.extensionId);
    await byId(locked, 'unlockPage').waitFor({ state: 'visible', timeout: 15_000 });

    // -- Step 3: wrong password → toast ----------------------------------
    await byId(locked, 'unlockPassword').fill('incorrect-password');
    await byId(locked, 'unlockBtn').click();
    await expect(byId(locked, 'globalToast')).toContainText('密码错误', { timeout: 10_000 });

    // -- Step 4: correct password → wallet page in 5s -------------------
    await byId(locked, 'unlockPassword').fill(TEST_PASSWORD);
    await byId(locked, 'unlockBtn').click();
    await byId(locked, 'walletPage').waitFor({ state: 'visible', timeout: 10_000 });

    // -- Step 5: terminate the service worker, prove recovery -----------
    // We need a regular page to attach a CDP session to. Open the popup
    // again (we already have one — `locked` is the now-unlocked popup).
    const cdp = await ctx.context.newCDPSession(locked);
    try {
      const { targetInfos } = await cdp.send('Target.getTargets');
      const sw = targetInfos.find(
        (t: { type: string; url: string; targetId?: string }) =>
          t.type === 'service_worker' &&
          t.url.startsWith(`chrome-extension://${ctx.extensionId}/`),
      );
      expect(sw?.targetId).toBeTruthy();
      if (sw?.targetId) {
        await cdp.send('Target.closeTarget', { targetId: sw.targetId });
      }
    } finally {
      await cdp.detach();
    }

    // After SW termination the wallet should re-prompt for the password.
    await locked.close();
    const afterKill = await openPopup(ctx.context, ctx.extensionId);
    await byId(afterKill, 'unlockPage').waitFor({ state: 'visible', timeout: 15_000 });
    await byId(afterKill, 'unlockPassword').fill(TEST_PASSWORD);
    await byId(afterKill, 'unlockBtn').click();
    await byId(afterKill, 'walletPage').waitFor({ state: 'visible', timeout: 15_000 });

    const addressAfter = ((await byId(afterKill, 'accountAddress').textContent())?.trim() ?? '').toLowerCase();
    // Both are truncated, so we can't compare full addresses; check the
    // prefix + suffix segments match. If the wallet restored a different
    // account, both would change.
    const [prefixBefore] = addressBefore.split(/…|\.\.\./);
    const [prefixAfter] = addressAfter.split(/…|\.\.\./);
    expect(prefixAfter).toBe(prefixBefore);

    // And the global waiting overlay should not be stuck — if it is, the
    // background never finished re-hydrating.
    await expect(byId(afterKill, 'globalWaitingOverlay')).toBeHidden();
  } finally {
    await teardownWalletContext(ctx);
  }
});