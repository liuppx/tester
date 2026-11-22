import { existsSync, readdirSync } from 'node:fs';
import type { LaunchOptions } from '@playwright/test';
import policy from './browser-policy.json' with { type: 'json' };

/** Flags are supplemental; Linux must also use the browser-runtime namespace. */
export function assertCpuBrowserIsolation(): void {
  if (process.env.TESTER_CPU_BROWSER !== '1' || process.platform !== 'linux') return;
  if (readdirSync('/sys').length || existsSync('/dev/dri') || existsSync('/dev/nvidiactl')) {
    throw new Error('CPU browser mode requires scripts/browser-runtime.sh on Linux.');
  }
}

/** Also applies to contexts launched directly by wallet helpers. */
export function cpuBrowserLaunchOptions(options: LaunchOptions = {}): LaunchOptions {
  if (process.env.TESTER_CPU_BROWSER !== '1') return options;
  assertCpuBrowserIsolation();
  return {
    ...options,
    channel: options.channel ?? 'chromium',
    args: [...(options.args ?? []), ...policy.args],
    env: {
      ...process.env,
      ...options.env,
      LIBGL_ALWAYS_SOFTWARE: '1',
      GALLIUM_DRIVER: 'llvmpipe',
    },
  };
}
