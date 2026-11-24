import { defineConfig, type PlaywrightTestConfig } from '@playwright/test';
import base from './playwright.config';
import policy from './shared/browser-policy.json' with { type: 'json' };

// Inherited by workers, including helpers that launch their own contexts.
process.env.TESTER_CPU_BROWSER = '1';
process.env.LIBGL_ALWAYS_SOFTWARE = '1';
process.env.GALLIUM_DRIVER = 'llvmpipe';

type ScheduledProject = NonNullable<PlaywrightTestConfig['projects']>[number];

const projects = (base.projects ?? [])
  .filter((project: ScheduledProject) => process.env.TESTER_LOCAL_SOURCE_MODE === '1' || project.name !== 'knowledge')
  .map((project: ScheduledProject) => {
    const launchOptions = project.use?.launchOptions ?? {};
    const existingArgs = launchOptions.args ?? [];

    return {
      ...project,
      use: {
        ...project.use,
        launchOptions: {
          ...launchOptions,
          channel: launchOptions.channel ?? 'chromium',
          args: [...existingArgs, ...policy.args],
        },
      },
    };
  });

export default defineConfig({
  ...base,
  projects,
  workers: process.env.PWWORKERS ? Number(process.env.PWWORKERS) : 2,
});
