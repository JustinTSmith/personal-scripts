/**
 * Extra screenshots for specific UI elements in AionUi Cowork guide.
 */

const { _electron: electron } = require('playwright');
const path = require('path');
const fs = require('fs');

const OUT_DIR = '/tmp/cowork-screenshots';
fs.mkdirSync(OUT_DIR, { recursive: true });
const APP_PATH = '/Applications/AionUi.app/Contents/MacOS/AionUi';

async function ss(page, name) {
  const file = path.join(OUT_DIR, `${name}.png`);
  await page.screenshot({ path: file, fullPage: false });
  console.log(`  ✓ ${name}.png`);
}

async function navigate(page, hash) {
  await page.evaluate((h) => window.location.assign(h), hash);
  await page.waitForFunction((h) => window.location.hash === h, hash, { timeout: 10_000 }).catch(() => {});
  await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
  await page.waitForFunction(() => (document.body.textContent?.length ?? 0) > 50, { timeout: 8_000 }).catch(() => {});
  await page.waitForTimeout(1000);
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
      AIONUI_E2E_TEST: '1',
      AIONUI_CDP_PORT: '0',
    },
    timeout: 60_000,
  });

  let page = null;
  const deadline = Date.now() + 45_000;
  while (!page && Date.now() < deadline) {
    try {
      const win = await app.waitForEvent('window', { timeout: 5_000 });
      if (!win.url().startsWith('devtools://')) page = win;
    } catch {}
    if (!page) page = app.windows().find(w => !w.url().startsWith('devtools://')) ?? null;
  }
  if (!page) throw new Error('No main window');

  await page.waitForLoadState('domcontentloaded');
  await page.waitForTimeout(2000);
  // Wait for the app to set a hash route (auth/init complete)
  try {
    await page.waitForFunction(
      () => window.location.hash && window.location.hash.length > 1,
      { timeout: 20_000 }
    );
  } catch {}
  await page.waitForTimeout(1500);

  // ── Agents → Remote Agents tab (marketplace/hub) ──────────────────────────
  console.log('[1] Agents → Remote Agents tab...');
  await navigate(page, '#/settings/agent');
  // Click "Remote Agents" tab
  try {
    const remoteTab = page.locator('text=Remote Agents, .arco-tabs-header-title:has-text("Remote")').first();
    if (await remoteTab.isVisible({ timeout: 3000 })) {
      await remoteTab.click();
      await page.waitForTimeout(1000);
    }
  } catch {}
  await ss(page, '07-agents-remote-hub');

  // ── Assistants page ────────────────────────────────────────────────────────
  console.log('[2] Assistants settings...');
  await navigate(page, '#/settings/agent');
  try {
    const assistTab = page.locator('text=Assistants, .arco-menu-item:has-text("Assistants")').first();
    if (await assistTab.isVisible({ timeout: 3000 })) {
      await assistTab.click();
      await page.waitForTimeout(800);
    }
  } catch {}
  await ss(page, '08-assistants');

  // ── Capabilities → Skills tab ─────────────────────────────────────────────
  console.log('[3] Capabilities (Skills/MCP)...');
  await navigate(page, '#/settings/tools');
  // Try Skills tab
  try {
    const skillsTab = page.locator('.arco-tabs-header-title:has-text("Skills"), text=Skills').first();
    if (await skillsTab.isVisible({ timeout: 3000 })) {
      await skillsTab.click();
      await page.waitForTimeout(800);
      await ss(page, '09-skills-tab');
    }
  } catch {}
  // MCP tab
  try {
    const mcpTab = page.locator('.arco-tabs-header-title:has-text("MCP"), text=MCP').first();
    if (await mcpTab.isVisible({ timeout: 3000 })) {
      await mcpTab.click();
      await page.waitForTimeout(800);
      await ss(page, '10-mcp-tab');
    }
  } catch {}

  // ── Manual Add MCP form ───────────────────────────────────────────────────
  console.log('[4] Manual Add MCP form...');
  await navigate(page, '#/settings/tools');
  try {
    // Click + Manual Add button
    const addBtn = page.locator('button:has-text("Manual Add"), button:has-text("+ Manual"), .arco-btn:has-text("Manual")').first();
    if (await addBtn.isVisible({ timeout: 4000 })) {
      await addBtn.click();
      await page.waitForTimeout(1000);
      await ss(page, '11-mcp-manual-add-form');
      // close
      await page.keyboard.press('Escape');
    }
  } catch {}

  // ── Cowork assistant selected in GUID ─────────────────────────────────────
  console.log('[5] Cowork mode in GUID page...');
  await navigate(page, '#/guid');
  // Try to click on Cowork assistant selector
  try {
    const coworkBtn = page.locator('[class*="cowork"], button:has-text("Cowork"), [data-value*="cowork"], img[alt*="cowork"]').first();
    if (await coworkBtn.isVisible({ timeout: 3000 })) {
      await coworkBtn.click();
      await page.waitForTimeout(800);
    }
  } catch {}
  await ss(page, '12-cowork-selected');

  console.log(`\nExtra screenshots saved to: ${OUT_DIR}`);
  await app.close();
})().catch(async (err) => {
  console.error('Error:', err.message);
  process.exit(1);
});
