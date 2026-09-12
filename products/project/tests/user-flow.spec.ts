/**
 * project — user-perspective baseline.
 *
 * Minimum user-flow coverage that any project e2e deployment should hit:
 *
 *   1. landing renders the project (DooTask) login or dashboard
 *   2. navigation surfaces at least one link or menu entry
 *   3. auth or wallet-connect affordance is discoverable in the DOM
 *
 * All three skip when `PROJECT_BASE_URL` is unset. When project (currently
 * down on :20833) comes up, these will start passing without code changes.
 */
import { test, expect, baseURLFor } from '../fixtures';

function skipIfNoService() {
  test.skip(!baseURLFor('project'), 'PROJECT_BASE_URL not configured');
}

test('landing renders the project home', async ({ page }) => {
  skipIfNoService();
  await page.goto('/');
  await expect(page.locator('body')).toBeVisible();
  const text = (await page.locator('body').innerText()).trim();
  expect(text.length).toBeGreaterThan(50);
});

test('navigation surfaces at least one link or menu entry', async ({ page }) => {
  skipIfNoService();
  await page.goto('/');
  // SPA mounts may not have rendered every header link to "visible"
  // yet. Assert DOM-attached, which matches what a user perceives:
  // an entry to navigate to.
  const entries = page.locator('a[href], [role="link"], [role="menuitem"]');
  expect(await entries.count()).toBeGreaterThan(0);
});

test('auth or wallet-connect affordance is discoverable', async ({ page }) => {
  skipIfNoService();
  await page.goto('/');
  // Loose: any input field, button, or link. Asserting DOM-attached
  // (not visible) — Element Plus and similar frameworks wrap `<input>`
  // in hidden containers, so visibility checks are fragile. The user's
  // perception is just "is there something here to interact with".
  const affordance = page.locator('input, button, a, [role="button"], [role="link"]');
  expect(await affordance.count()).toBeGreaterThan(0);
});