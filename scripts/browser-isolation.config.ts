import { defineConfig } from '@playwright/test';
import { cpuBrowserLaunchOptions } from '../shared/browser';

process.env.TESTER_CPU_BROWSER = '1';

export default defineConfig({
  testDir: './tests',
  testMatch: 'browser-isolation.spec.ts',
  workers: 1,
  retries: 0,
  timeout: 30_000,
  reporter: [['list']],
  outputDir: process.env.TESTER_RUNTIME_DIR ?? '/tmp/tester-browser-isolation-results',
  use: { launchOptions: cpuBrowserLaunchOptions() },
});
