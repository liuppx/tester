import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test, expect } from '@playwright/test';
import { assertCpuBrowserIsolation } from '../../shared/browser';
import { loadWalletContext, teardownWalletContext } from '../../products/wallet/helpers/extension';

test('isolated Chromium can render and interact with a page', async ({ page }) => {
  assertCpuBrowserIsolation();
  await page.setContent('<button onclick="this.textContent=\'Clicked\'">Ready</button>');
  await page.getByRole('button', { name: 'Ready' }).click();
  await expect(page.getByRole('button', { name: 'Clicked' })).toBeVisible();
});

test('wallet persistent context loads an MV3 extension under the same isolation', async () => {
  const extension = mkdtempSync(join(tmpdir(), 'tester-mv3-fixture-'));
  const previous = process.env.WALLET_EXTENSION_PATH;
  writeFileSync(
    join(extension, 'manifest.json'),
    JSON.stringify({
      manifest_version: 3,
      name: 'Tester isolation fixture',
      version: '1.0.0',
      background: { service_worker: 'worker.js' },
      action: { default_popup: 'popup.html' },
    }),
  );
  writeFileSync(join(extension, 'worker.js'), 'chrome.runtime.onInstalled.addListener(() => {});');
  writeFileSync(join(extension, 'popup.html'), '<h1>Isolated MV3</h1>');
  process.env.WALLET_EXTENSION_PATH = extension;
  try {
    // Exercise the real Wallet launch helper, including headed/Xvfb behavior.
    const wallet = await loadWalletContext({ headless: false });
    try {
      const page = await wallet.context.newPage();
      await page.goto(`chrome-extension://${wallet.extensionId}/popup.html`);
      await expect(page.getByRole('heading', { name: 'Isolated MV3' })).toBeVisible();
    } finally {
      await teardownWalletContext(wallet);
    }
  } finally {
    if (previous === undefined) delete process.env.WALLET_EXTENSION_PATH;
    else process.env.WALLET_EXTENSION_PATH = previous;
    rmSync(extension, { recursive: true, force: true });
  }
});
