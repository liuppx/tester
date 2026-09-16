/**
 * Browser session seeding for social SPA e2e.
 *
 * The SPA reads its JWT from `sessionStorage.accessToken` (and refreshes via
 * `sessionStorage.refreshToken`). Seeding these before the app boots lets a
 * test land directly on an authenticated `#/home/...` route, bypassing the
 * wallet / passport login UI. The token must be a *real* JWT — Home.vue calls
 * `GET /user/self` on mount, and a 400 (未登录) hard-redirects back to `/`.
 */
import type { Page } from '@playwright/test';
import type { LoginVO } from './auth';

/** Inject accessToken/refreshToken into sessionStorage before the first load. */
export async function seedSession(page: Page, login: LoginVO): Promise<void> {
  await page.addInitScript(
    ([access, refresh]) => {
      sessionStorage.setItem('accessToken', access);
      sessionStorage.setItem('refreshToken', refresh);
    },
    [login.accessToken, login.refreshToken] as const,
  );
}
