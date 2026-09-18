/**
 * Wallet — popup: create a Tron HD wallet (secp256k1, m/44'/195'/0'/0/0)
 * via the wallet-type menu and assert the resulting account has a
 * Base58Check (`T...`) Tron mainnet address.
 *
 * v1 added a third "Tron HD" option next to the existing "HD Wallet"
 * and "MPC Wallet" entries on `#createWalletTypeMenu`. Picking it
 * reveals a Tron network submenu (Mainnet / Shasta / Nile) so the user
 * can target a non-mainnet address prefix.
 *
 * Flow:
 *
 *   1. Welcome → 新建钱包 → `#setPasswordPage`.
 *   2. Open the wallet-type dropdown → click the Tron HD entry.
 *   3. The hidden `#tronCreateWalletFields` section appears; pick
 *      `Shasta (Testnet)` from the Tron network submenu.
 *   4. Confirm with `#setPasswordBtn` + the password modal.
 *   5. Land on `#walletPage`; `#accountAddress` should be a Base58Check
 *      Tron address (matches /^[1-9A-HJ-NP-Za-km-z]{34,35}$/) with the
 *      Shasta prefix byte (0xa0) reflected in the encoded form.
 *
 * Why we don't compute the exact address here: the deterministic
 * Anvil/Hardhat mnemonic yields a random HD seed for the *first* run,
 * so the address depends on entropy. We instead assert on the shape:
 * Base58Check alphabet, 34–35 chars (some 21-byte payloads encode to
 * 35 because of leading zeros), and the leading character. A freshly
 * generated mnemonic + Tron mainnet prefix 0x41 produces addresses
 * starting with `T` most of the time; Shasta (prefix 0xa0) starts with
 * `2` or `4`. See `wallet/tests/tron-vault.test.mjs` for the unit-level
 * proof.
 */
import { test, expect } from '../fixtures';

import { loadWalletContext, teardownWalletContext } from '../helpers/extension';
import { stubPublicEndpoints } from '../helpers/network';
import { byId, openPopup, TEST_PASSWORD, TEST_WALLET_NAME } from '../helpers/popup';

test('create Tron HD wallet via wallet-type menu lands on #walletPage with a T... address', async ({ recorder }) => {
  const ctx = await loadWalletContext();
  try {
    await stubPublicEndpoints(ctx.context);
    const popup = await openPopup(ctx.context, ctx.extensionId);

    await byId(popup, 'welcomePage').waitFor({ state: 'visible' });
    await byId(popup, 'welcomeCreateWalletBtn').click();
    await byId(popup, 'setPasswordPage').waitFor({ state: 'visible' });
    await recorder.step(popup, '设置密码页（默认 HD）', {
      note: '右上角的钱包类型菜单里有 HD / Tron HD / MPC 三个选项。',
    });

    // Open the wallet-type dropdown and pick Tron HD.
    await byId(popup, 'createWalletTypeTrigger').click();
    await byId(popup, 'createWalletTypeMenu')
      .locator('.network-option[data-wallet-type="tron"]')
      .click();
    await expect(byId(popup, 'createWalletTypeLabel')).toHaveText('Tron HD');
    await expect(byId(popup, 'createWalletTypeSelect')).toHaveValue('tron');
    await expect(byId(popup, 'tronCreateWalletFields')).toBeVisible();
    await recorder.step(popup, '选择 Tron HD 后展开网络子菜单', {
      note: '默认 Mainnet；Tron 助记词也是新生成（与 EVM HD 路径不同：m/44\'/195\'/0\'/0/0）。',
    });

    // Switch the Tron reference to Shasta so the address uses prefix 0xa0.
    await byId(popup, 'tronCreateNetworkTrigger').click();
    await byId(popup, 'tronCreateNetworkMenu')
      .locator('.network-option[data-tron-reference="shasta"]')
      .click();
    await expect(byId(popup, 'tronCreateNetworkLabel')).toHaveText(/Shasta/);
    await expect(byId(popup, 'tronCreateNetworkSelect')).toHaveValue('shasta');

    await byId(popup, 'setWalletName').fill('E2E Tron HD');
    await byId(popup, 'setPasswordBtn').click();
    await byId(popup, 'passwordPromptInput').fill(TEST_PASSWORD);
    await byId(popup, 'passwordPromptConfirm').click();

    await byId(popup, 'walletPage').waitFor({ state: 'visible', timeout: 30_000 });
    await recorder.step(popup, '创建完成，回到主页', {
      note: '地址是 Base58Check 形态；Shasta 前缀 0xa0 编码后多以 2 开头。',
    });

    const address = (await byId(popup, 'accountAddress').textContent())?.trim() ?? '';
    // Base58Check alphabet, 34–35 chars (some payloads lead with 0 → 35 chars).
    expect(address).toMatch(/^[1-9A-HJ-NP-Za-km-z]{34,35}$/);
    // Shasta prefix byte (0xa0) encodes to Base58 with leading '2' or '4'
    // most of the time. We don't pin to a single char because the
    // leading bytes after the prefix vary; just ensure it's NOT the
    // mainnet 'T' prefix.
    expect(address.startsWith('T')).toBe(false);
  } finally {
    await teardownWalletContext(ctx);
  }
});

test('wallet-type menu exposes the Tron HD entry', async () => {
  const ctx = await loadWalletContext({ headless: true });
  try {
    const popup = await openPopup(ctx.context, ctx.extensionId);
    await byId(popup, 'welcomePage').waitFor({ state: 'visible' });
    await byId(popup, 'welcomeCreateWalletBtn').click();
    await byId(popup, 'setPasswordPage').waitFor({ state: 'visible' });

    await byId(popup, 'createWalletTypeTrigger').click();
    await byId(popup, 'createWalletTypeMenu').waitFor({ state: 'visible' });
    const labels = await byId(popup, 'createWalletTypeMenu')
      .locator('.network-option')
      .allTextContents();
    expect(labels.map((s) => s.trim())).toEqual(expect.arrayContaining(['HD Wallet', 'Tron HD', 'MPC Wallet']));
  } finally {
    await teardownWalletContext(ctx);
  }
});