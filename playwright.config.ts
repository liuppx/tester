import { defineConfig, devices, type Project } from '@playwright/test';
import { baseURLFor, loadEnv } from './shared/env';
import { defaultReporters } from './shared/reporters';

const HEADED = process.env.PWHEADLESS === '0';

const products = [
  'warehouse',
  'node',
  'router',
  'chat',
  'social',
  'project',
  'knowledge',
  'marketplace',
  'books',
  'agent',
  'wallet',
] as const;

type ProductName = (typeof products)[number];

const chatOverrides = { navigationTimeout: 60_000, actionTimeout: 15_000 };

/**
 * Wallet is a Chromium MV3 extension loaded from disk via `--load-extension`.
 * The path is resolved from `WALLET_EXTENSION_PATH` (preferred) or
 * `WALLET_REPO_PATH` (matches marketplace/books convention).
 *
 * MV3 service workers don't fully load in headless Chromium, so wallet tests
 * run headed by default. The `PWHEADLESS=0` env still works for any
 * already-headed session.
 */
const walletExtensionPath =
  process.env['WALLET_EXTENSION_PATH']?.trim() || process.env['WALLET_REPO_PATH']?.trim() || '';

const walletOverrides: NonNullable<Project['use']> = {
  // Persistent context with the extension loaded; the persistent context is
  // what gives us a real `chrome.runtime.id` and lets the SW keep state.
  launchOptions: walletExtensionPath
    ? {
        channel: 'chromium',
        headless: false,
        args: [
          `--disable-extensions-except=${walletExtensionPath}`,
          `--load-extension=${walletExtensionPath}`,
        ],
      }
    : { channel: 'chromium', headless: false },
  // Extension service workers can take several seconds to register on first
  // launch; give some headroom for the initial UI assertions.
  actionTimeout: 20_000,
  navigationTimeout: 60_000,
};

const projects: Project[] = products.map((product: ProductName) => ({
  name: product,
  testDir: `products/${product}/tests`,
  testMatch: /.*\.spec\.ts$/,
  outputDir: `test-results/${product}`,
  use: {
    ...devices['Desktop Chrome'],
    baseURL: baseURLFor(product),
    headless: !HEADED,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'off',
    ...(product === 'chat' ? chatOverrides : {}),
    ...(product === 'wallet' ? walletOverrides : {}),
  },
}));

export default defineConfig({
  testDir: '.',
  // testDir is overridden per-project; this keeps the root happy when run with --list.
  testMatch: /.*\.spec\.ts$/,
  timeout: 30_000,
  expect: { timeout: 5_000 },
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.PWWORKERS ? Number(process.env.PWWORKERS) : undefined,
  reporter: defaultReporters(),
  // Snapshot env once so each spec sees a consistent view.
  globalSetup: './scripts/global-setup.ts',
  use: {
    headless: !HEADED,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'off',
    actionTimeout: 10_000,
    navigationTimeout: 20_000,
  },
  projects,
  metadata: {
    products: loadEnv(),
  },
});
