/**
 * Helpers for driving the wallet's popup and approval UIs.
 *
 * The wallet's MV3 extension has two UI surfaces:
 *
 *   1. **Popup** (`html/popup.html`) — the main 380×600 view that owns the
 *      keyring, account list, transfer screen, etc. Opened via
 *      `chrome-extension://<id>/html/popup.html`.
 *   2. **Approval window** (`html/approval.html`) — a *separate*
 *      `chrome.windows.create({ type: 'popup' })` window that appears
 *      whenever a dApp requests an unlock-required action
 *      (`eth_requestAccounts`, `personal_sign`, etc.). The Playwright
 *      context fires a new `page` event when this window opens.
 *
 * Selectors are copied from `wallet/tests/e2e/extension-smoke.test.mjs`,
 * which is the source of truth for popup flows.
 */
import type { BrowserContext, Page } from '@playwright/test';

import { getExtensionId } from './extension';

/** Default popup dimensions — mirror `js/config/ui-config.js:POPUP_DIMENSIONS`. */
export const POPUP_WIDTH = 380;
export const POPUP_HEIGHT = 600;

export const SELECTORS = {
  welcomePage: '#welcomePage',
  welcomeCreateWalletBtn: '#welcomeCreateWalletBtn',
  welcomeImportWalletBtn: '#welcomeImportWalletBtn',

  setPasswordPage: '#setPasswordPage',
  setWalletName: '#setWalletName',
  setPasswordBtn: '#setPasswordBtn',
  passwordPromptInput: '#passwordPromptInput',
  passwordPromptConfirm: '#passwordPromptConfirm',

  walletPage: '#walletPage',
  accountAddress: '#accountAddress',
  walletHeaderMenuBtn: '#walletHeaderMenuBtn',
  walletHeaderMenu: '#walletHeaderMenu',
  lockWalletBtn: '#lockWalletBtn',

  unlockPage: '#unlockPage',
  unlockPassword: '#unlockPassword',
  unlockBtn: '#unlockBtn',
  globalToast: '#globalToast',

  globalWaitingOverlay: '#globalWaitingOverlay',

  transferPage: '#transferPage',
  transferBtn: '#transferBtn',
  recipientAddress: '#recipientAddress',
  amount: '#amount',

  importPage: '#importPage',
  importMnemonic: '#importMnemonic',
  importWalletPassword: '#importWalletPassword',
  importBtn: '#importBtn',
  importAccountName: '#importAccountName',
} as const;

/** Open the main popup (380×600) and return the page. */
export async function openPopup(context: BrowserContext, extensionId?: string): Promise<Page> {
  const id = extensionId ?? (await getExtensionId(context));
  const page = await context.newPage();
  await page.setViewportSize({ width: POPUP_WIDTH, height: POPUP_HEIGHT });
  await page.goto(`chrome-extension://${id}/html/popup.html`);
  return page;
}

/** Convenience for typed lookups; returns a Playwright Locator. */
export function byId(page: Page, key: keyof typeof SELECTORS) {
  const sel = SELECTORS[key];
  return page.locator(sel);
}

/**
 * Wait for the next approval window to open, return it.
 *
 * `requestType` filters by the `type=...` query parameter the wallet uses
 * (`connect`, `transaction`, `sign`, `profile`). If omitted, returns the
 * first approval window regardless of type.
 *
 * The approval window has the URL pattern
 * `chrome-extension://<id>/html/approval.html?requestId=...&type=<type>`.
 */
export async function waitForApproval(
  context: BrowserContext,
  extensionId: string,
  options: { requestType?: string; timeout?: number } = {},
): Promise<Page> {
  const { requestType, timeout = 30_000 } = options;

  const approvalPage = await context.waitForEvent('page', {
    predicate: (page) => {
      const url = page.url();
      if (!url.startsWith(`chrome-extension://${extensionId}/html/approval.html`)) return false;
      if (!requestType) return true;
      return new URL(url).searchParams.get('type') === requestType;
    },
    timeout,
  });
  await approvalPage.bringToFront();
  return approvalPage;
}