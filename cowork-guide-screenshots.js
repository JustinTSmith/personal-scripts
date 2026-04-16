/**
 * Captures screenshots of AionUi Cowork screens for the Financial Modeler Setup Guide.
 * Outputs to /tmp/cowork-screenshots/
 *
 * Screens captured:
 *   01-guid-page         — main chat/GUID page (Cowork home)
 *   02-settings-agent    — Agent settings (Plugin Marketplace equivalent)
 *   03-agent-hub-modal   — AgentHub modal (Browse Marketplace)
 *   04-settings-tools    — Tools/MCP settings (Connected Tools)
 *   05-tools-add-mcp     — MCP add-connection area
 *   06-settings-system   — System settings (folder/workspace)
 */

const { _electron: electron } = require('playwright');
const path = require('path');
const fs = require('fs');

const OUT_DIR = '/tmp/cowork-screenshots';
fs.mkdirSync(OUT_DIR, { recursive: true });

const APP_PATH = '/Applications/AionUi.app/Contents/MacOS/AionUi';

async function ss(page, name, opts = {}) {
  const file = path.join(OUT_DIR, `${name}.png`);
  await page.screenshot({ path: file, fullPage: false, ...opts });
  console.log(`  ✓ ${name}.png`);
  return file;
}

async function navigate(page, hash) {
  await page.evaluate((h) => window.location.assign(h), hash);
  await page.waitForFunction((h) => window.location.hash === h, hash, { timeout: 10_000 })
    .catch(() => {});
  await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
  await page.waitForFunction(() => (document.body.textContent?.length ?? 0) > 50, { timeout: 8_000 })
    .catch(() => {});
  // Extra settle time for lazy-loaded content
  await page.waitForTimeout(800);
}

async function tryClick(page, selector, timeout = 3000) {
  try {
    await page.waitForSelector(selector, { state: 'visible', timeout });
    await page.click(selector);
    return true;
  } catch {
    return false;
  }
}

(async () => {
  console.log('Launching AionUi...');
  const app = await electron.launch({
    executablePath: APP_PATH,
    args: [],
    env: {
      ...process.env,
      AIONUI_DISABLE_AUTO_UPDATE: '1',
      AIONUI_DISABLE_DEVTOOLS: '1',
      AIONUI_E2E_TEST: '1',        // skips single-instance lock
      AIONUI_CDP_PORT: '0',
    },
    timeout: 60_000,
  });

  // Get main window — listen for first non-devtools window event
  let page = null;

  // Check existing windows first
  const existing = app.windows().find(w => !w.url().startsWith('devtools://'));
  if (existing) {
    page = existing;
  } else {
    // Wait for a window to appear
    const deadline = Date.now() + 45_000;
    while (!page && Date.now() < deadline) {
      try {
        const win = await app.waitForEvent('window', { timeout: 5_000 });
        if (!win.url().startsWith('devtools://')) {
          page = win;
        }
      } catch {
        // timeout on this attempt, retry
      }
      // Also check all current windows
      if (!page) {
        page = app.windows().find(w => !w.url().startsWith('devtools://')) ?? null;
      }
    }
  }
  if (!page) throw new Error('No main window found after 45s');

  await page.waitForLoadState('domcontentloaded');
  await page.waitForTimeout(3000); // let UI fully render + auth checks settle

  console.log(`App loaded: ${page.url()}`);
  // Take a debug screenshot first to see what state the app is in
  await ss(page, '00-debug-initial');

  // Wait for the app to be in a usable state (not login/onboarding)
  try {
    await page.waitForFunction(
      () => window.location.hash && window.location.hash.length > 1,
      { timeout: 15_000 }
    );
    console.log(`Hash route: ${await page.evaluate(() => window.location.hash)}`);
  } catch {
    console.log('No hash route detected, proceeding anyway');
  }

  // ── 1. GUID / main chat page ─────────────────────────────────────────────
  console.log('\n[1/6] GUID page (Cowork home)...');
  await navigate(page, '#/guid');
  await ss(page, '01-cowork-home');

  // ── 2. Agent Settings (Plugin Marketplace) ──────────────────────────────
  console.log('[2/6] Agent settings (Plugin Marketplace)...');
  await navigate(page, '#/settings/agent');
  await ss(page, '02-settings-plugins');

  // ── 3. AgentHub modal (Browse Marketplace) ──────────────────────────────
  console.log('[3/6] AgentHub modal (Browse Marketplace)...');
  // Try to click the "Hub" / "Marketplace" button if present
  const hubOpened = await tryClick(page,
    '[data-testid="agent-hub-btn"], button[class*="hub"], button:has-text("Hub"), button:has-text("Marketplace"), button:has-text("Browse")',
    4000
  );
  if (hubOpened) {
    await page.waitForTimeout(1000);
    await ss(page, '03-plugin-marketplace');
    // Close modal
    await tryClick(page, '.arco-modal-close-btn, button[aria-label="Close"], .arco-icon-close', 2000);
  } else {
    console.log('  (Hub modal button not found — using agent settings screenshot)');
    await ss(page, '03-plugin-marketplace');
  }

  // ── 4. Tools / MCP Settings (Connected Tools) ───────────────────────────
  console.log('[4/6] Tools/MCP settings (Connected Tools)...');
  await navigate(page, '#/settings/tools');
  await ss(page, '04-settings-connected-tools');

  // ── 5. MCP server list / add connection area ────────────────────────────
  console.log('[5/6] MCP add-connection...');
  // Try to click an "Add" or "+" button in tools
  const addClicked = await tryClick(page,
    'button:has-text("Add"), button:has-text("+ Add"), [data-testid*="add"], button[class*="add"]',
    3000
  );
  if (addClicked) {
    await page.waitForTimeout(800);
    await ss(page, '05-add-connection');
    // Close any modal
    await tryClick(page, '.arco-modal-close-btn, button[aria-label="Close"]', 2000);
  } else {
    await ss(page, '05-mcp-tools-list');
  }

  // ── 6. System settings (workspace / folder structure) ───────────────────
  console.log('[6/6] System settings (workspace)...');
  await navigate(page, '#/settings/system');
  await ss(page, '06-settings-system-workspace');

  // ── 7. Agents → Remote Agents tab ────────────────────────────────────────
  console.log('[7] Agents → Remote Agents tab...');
  await navigate(page, '#/settings/agent');
  try {
    const remoteTab = page.locator('.arco-tabs-header-title').filter({ hasText: 'Remote' }).first();
    if (await remoteTab.isVisible({ timeout: 3000 })) {
      await remoteTab.click();
      await page.waitForTimeout(1000);
    }
  } catch {}
  await ss(page, '07-agents-remote-hub');

  // ── 8. Capabilities Skills tab ────────────────────────────────────────────
  console.log('[8] Capabilities Skills tab...');
  await navigate(page, '#/settings/tools');
  try {
    const skillsTab = page.locator('.arco-tabs-header-title').filter({ hasText: /Skills/i }).first();
    if (await skillsTab.isVisible({ timeout: 3000 })) {
      await skillsTab.click();
      await page.waitForTimeout(800);
      await ss(page, '08-skills-tab');
    }
  } catch {}

  // ── 9. MCP Manual Add form ────────────────────────────────────────────────
  console.log('[9] MCP Manual Add form...');
  await navigate(page, '#/settings/tools');
  try {
    const addBtn = page.locator('button').filter({ hasText: /Manual Add|Add Connection/ }).first();
    if (await addBtn.isVisible({ timeout: 4000 })) {
      await addBtn.click();
      await page.waitForTimeout(1200);
      await ss(page, '09-mcp-add-connection-form');
      await page.keyboard.press('Escape');
      await page.waitForTimeout(500);
    }
  } catch {}

  // ── 10. GUID page with Cowork assistant ───────────────────────────────────
  console.log('[10] Cowork mode GUID page...');
  await navigate(page, '#/guid');
  await ss(page, '10-guid-with-cowork');

  console.log(`\nAll screenshots saved to: ${OUT_DIR}`);
  console.log(fs.readdirSync(OUT_DIR).join('\n'));

  await app.close();
})().catch(async (err) => {
  console.error('Error:', err.message);
  process.exit(1);
});
