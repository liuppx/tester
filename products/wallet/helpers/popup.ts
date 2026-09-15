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
  accountName: '#accountName',
  walletHeaderMenuBtn: '#walletHeaderMenuBtn',
  walletHeaderMenu: '#walletHeaderMenu',
  lockWalletBtn: '#lockWalletBtn',

  // Account switcher (header) + accounts management page.
  accountHeader: '#accountHeader',
  accountDropdownBtn: '#accountDropdownBtn',
  accountSwitcherMenu: '#accountSwitcherMenu',
  accountSwitcherList: '#accountSwitcherList',
  manageAccountsBtn: '#manageAccountsBtn',
  accountsPage: '#accountsPage',
  walletList: '#walletList',
  createAccountModal: '#createAccountModal',
  newAccountName: '#newAccountName',
  confirmCreateAccount: '#confirmCreateAccount',

  // Account detail page (rename + QR / receive).
  accountDetailPage: '#accountDetailPage',
  accountDetailNameText: '#accountDetailNameText',
  editAccountNameBtn: '#editAccountNameBtn',
  accountDetailNameInput: '#accountDetailNameInput',
  saveAccountNameBtn: '#saveAccountNameBtn',
  accountDetailAddress: '#accountDetailAddress',
  accountDetailQr: '#accountDetailQr',

  // Network management page + add/edit form.
  networkManagePage: '#networkManagePage',
  networkAddBtn: '#networkAddBtn',
  networkManageList: '#networkManageList',
  networkFormPage: '#networkFormPage',
  networkNameInput: '#networkNameInput',
  networkRpcInput: '#networkRpcInput',
  networkChainIdInput: '#networkChainIdInput',
  networkSymbolInput: '#networkSymbolInput',
  networkExplorerInput: '#networkExplorerInput',
  saveNetworkBtn: '#saveNetworkBtn',

  // Settings + security modals.
  settingsBtn: '#settingsBtn',
  settingsPage: '#settingsPage',
  changePasswordBtn: '#changePasswordBtn',
  changePasswordModal: '#changePasswordModal',
  oldPasswordInput: '#oldPasswordInput',
  newPasswordInput: '#newPasswordInput',
  confirmNewPasswordInput: '#confirmNewPasswordInput',
  confirmChangePasswordBtn: '#confirmChangePasswordBtn',
  secretDisplayModal: '#secretDisplayModal',
  secretDisplayValue: '#secretDisplayValue',
  confirmSecretDisplayBtn: '#confirmSecretDisplayBtn',

  // Dynamic password-prompt modal (created on demand).
  passwordPromptModal: '#passwordPromptModal',
  passwordPromptClose: '#passwordPromptClose',

  unlockPage: '#unlockPage',
  unlockPassword: '#unlockPassword',
  unlockBtn: '#unlockBtn',
  globalToast: '#globalToast',

  globalWaitingOverlay: '#globalWaitingOverlay',

  transferPage: '#transferPage',
  transferBtn: '#transferBtn',
  recipientAddress: '#recipientAddress',
  amount: '#amount',
  sendBtn: '#sendBtn',

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

/** Default password + wallet name used by setup helpers. */
export const TEST_PASSWORD = 'E2E-password-2026';
export const TEST_WALLET_NAME = 'E2E Wallet';

/**
 * Create + unlock a fresh wallet in the given context and return the
 * popup page once `#walletPage` is visible. Used by all popup specs
 * that need a ready state without re-implementing the welcome →
 * set-password dance.
 */
export async function createAndUnlockWallet(
  context: BrowserContext,
  extensionId: string,
  options?: { password?: string; walletName?: string },
): Promise<Page> {
  const password = options?.password ?? TEST_PASSWORD;
  const walletName = options?.walletName ?? TEST_WALLET_NAME;
  const popup = await openPopup(context, extensionId);
  await byId(popup, 'welcomePage').waitFor({ state: 'visible' });
  await byId(popup, 'welcomeCreateWalletBtn').click();
  await byId(popup, 'setPasswordPage').waitFor({ state: 'visible' });
  await byId(popup, 'setWalletName').fill(walletName);
  await byId(popup, 'setPasswordBtn').click();
  await byId(popup, 'passwordPromptInput').fill(password);
  await byId(popup, 'passwordPromptConfirm').click();
  await byId(popup, 'walletPage').waitFor({ state: 'visible', timeout: 30_000 });
  return popup;
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