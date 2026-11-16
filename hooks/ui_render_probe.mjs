#!/usr/bin/env node
// Deterministic UI render probe for `ctl run-ui-render-evidence`.
//
// Renders each declared target at each viewport width, saves a full-page
// screenshot and measures what an auditor cannot read off a diff: horizontal
// overflow, undersized touch targets, text contrast, unnamed controls, images
// without alt text, broken images and console errors. No LLM, no network
// beyond the declared loopback / file targets.
//
// usage: node ui_render_probe.mjs --spec <spec.json> --out <dir>
// spec:  {"targets": [{"id", "url", "wait_for"?}], "widths": [320, 768, 1440],
//         "height": 900, "nav_timeout_ms": 30000}
// Prints one JSON object on stdout. Exit 0 when the probe ran (even with
// findings), 3 when no browser driver is available, 2 on a usage error.

import { createRequire } from 'node:module';
import { readFileSync, mkdirSync } from 'node:fs';
import { join, resolve } from 'node:path';

const MAX_ITEMS = 20;

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--spec') out.spec = argv[i += 1];
    else if (argv[i] === '--out') out.out = argv[i += 1];
  }
  return out;
}

function loadChromium() {
  // The project's own install wins (same version its tests use), then a
  // global one. A driver is never installed from here.
  const bases = [join(process.cwd(), 'package.json'), import.meta.url];
  const names = ['playwright', '@playwright/test', 'playwright-core'];
  for (const base of bases) {
    const req = createRequire(base);
    for (const name of names) {
      try {
        const mod = req(name);
        if (mod && mod.chromium) return { chromium: mod.chromium, driver: name };
      } catch { /* try the next one */ }
    }
  }
  return null;
}

// Runs inside the page. Must stay self-contained (no closure references).
function measure(maxItems) {
  const vw = window.innerWidth;
  const describe = (el) => {
    let s = el.tagName.toLowerCase();
    if (el.id) s += `#${el.id}`;
    const cls = typeof el.className === 'string' ? el.className.trim().split(/\s+/).filter(Boolean).slice(0, 2) : [];
    if (cls.length) s += `.${cls.join('.')}`;
    const text = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('alt') || '').trim().replace(/\s+/g, ' ').slice(0, 40);
    return text ? `${s} "${text}"` : s;
  };
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none' && Number(cs.opacity) !== 0;
  };
  const push = (list, item) => { if (list.length < maxItems) list.push(item); };

  // Horizontal overflow.
  const doc = document.documentElement;
  const scrollWidth = Math.max(doc.scrollWidth, document.body ? document.body.scrollWidth : 0);
  const overflow = { overflows: scrollWidth > vw + 1, scroll_width: scrollWidth, viewport_width: vw, offenders: [] };
  if (overflow.overflows) {
    for (const el of document.body.querySelectorAll('*')) {
      if (!visible(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.right > vw + 1 && r.width <= scrollWidth) push(overflow.offenders, { element: describe(el), right: Math.round(r.right) });
      if (overflow.offenders.length >= 5) break;
    }
  }

  // Interactive elements: target size and accessible name.
  const interactiveSel = 'a[href], button, input:not([type="hidden"]), select, textarea, summary, [role="button"], [role="link"], [role="checkbox"], [role="radio"], [role="switch"], [role="tab"], [role="menuitem"], [tabindex]:not([tabindex="-1"])';
  const smallTargets = [];
  const unnamed = [];
  let under44 = 0;
  let interactiveCount = 0;
  const nameOf = (el) => {
    const labelled = el.getAttribute('aria-labelledby');
    if (labelled) {
      const t = labelled.split(/\s+/).map((id) => (document.getElementById(id) || {}).textContent || '').join(' ').trim();
      if (t) return t;
    }
    const aria = (el.getAttribute('aria-label') || '').trim();
    if (aria) return aria;
    if (el.labels && el.labels.length) {
      const t = Array.from(el.labels).map((l) => l.textContent || '').join(' ').trim();
      if (t) return t;
    }
    const tag = el.tagName.toLowerCase();
    if (tag === 'input') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (['submit', 'button', 'reset'].includes(type)) return (el.value || type).trim();
      if (type === 'image') return (el.getAttribute('alt') || '').trim();
    }
    const text = (el.textContent || '').trim();
    if (text) return text;
    const img = el.querySelector('img[alt], svg[aria-label], [aria-label]');
    if (img) return (img.getAttribute('alt') || img.getAttribute('aria-label') || '').trim();
    const svgTitle = el.querySelector('svg title');
    if (svgTitle && svgTitle.textContent.trim()) return svgTitle.textContent.trim();
    return (el.getAttribute('title') || '').trim();
  };
  for (const el of document.querySelectorAll(interactiveSel)) {
    if (!visible(el) || el.disabled || el.getAttribute('aria-hidden') === 'true') continue;
    interactiveCount += 1;
    const r = el.getBoundingClientRect();
    const tag = el.tagName.toLowerCase();
    // WCAG 2.5.8 exempts links inline in a sentence; skip them.
    const inlineLink = tag === 'a' && getComputedStyle(el).display === 'inline'
      && el.parentElement && (el.parentElement.textContent || '').trim().length > (el.textContent || '').trim().length;
    if (!inlineLink) {
      const w = Math.round(r.width);
      const h = Math.round(r.height);
      if (w < 24 || h < 24) push(smallTargets, { element: describe(el), width: w, height: h });
      else if (w < 44 || h < 44) under44 += 1;
    }
    if (!nameOf(el)) push(unnamed, { element: describe(el) });
  }

  // Images.
  const missingAlt = [];
  const broken = [];
  for (const img of document.querySelectorAll('img')) {
    const decorative = img.getAttribute('role') === 'presentation' || img.getAttribute('role') === 'none' || img.getAttribute('aria-hidden') === 'true';
    if (!img.hasAttribute('alt') && !decorative) push(missingAlt, { element: describe(img), src: (img.getAttribute('src') || '').slice(0, 120) });
    if (img.complete && img.naturalWidth === 0 && img.getAttribute('src')) push(broken, { element: describe(img), src: (img.getAttribute('src') || '').slice(0, 120) });
  }

  // Text contrast (WCAG 2.x relative luminance). Only measured where the
  // background is a solid colour all the way up; anything over an image or
  // gradient is counted as unmeasured rather than guessed.
  const parse = (v) => {
    const m = /^rgba?\(([^)]+)\)$/.exec(v || '');
    if (!m) return null;
    const p = m[1].split(/[,\s/]+/).filter(Boolean).map(Number);
    if (p.length < 3 || p.slice(0, 3).some(Number.isNaN)) return null;
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 && !Number.isNaN(p[3]) ? p[3] : 1 };
  };
  const over = (top, bottom) => ({
    r: top.r * top.a + bottom.r * (1 - top.a),
    g: top.g * top.a + bottom.g * (1 - top.a),
    b: top.b * top.a + bottom.b * (1 - top.a),
    a: 1,
  });
  const lum = (c) => {
    const f = (x) => { const s = x / 255; return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  };
  const backgroundOf = (el) => {
    const layers = [];
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      const cs = getComputedStyle(node);
      if (cs.backgroundImage && cs.backgroundImage !== 'none') return null;
      if (Number(cs.opacity) < 1) return null;
      const bg = parse(cs.backgroundColor);
      if (bg && bg.a > 0) {
        layers.push(bg);
        if (bg.a === 1) break;
      }
    }
    let base = { r: 255, g: 255, b: 255, a: 1 };
    for (let i = layers.length - 1; i >= 0; i -= 1) base = over(layers[i], base);
    return base;
  };
  const lowContrast = [];
  let contrastChecked = 0;
  let contrastUnmeasured = 0;
  const seen = new Set();
  const walker = document.createTreeWalker(document.body || doc, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (!node.nodeValue || !node.nodeValue.trim()) continue;
    const el = node.parentElement;
    if (!el || seen.has(el) || !visible(el)) continue;
    if (['script', 'style', 'noscript', 'option'].includes(el.tagName.toLowerCase())) continue;
    seen.add(el);
    const cs = getComputedStyle(el);
    const fg = parse(cs.color);
    const bg = backgroundOf(el);
    if (!fg || !bg) { contrastUnmeasured += 1; continue; }
    contrastChecked += 1;
    const text = over(fg, bg);
    const l1 = lum(text);
    const l2 = lum(bg);
    const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    const size = parseFloat(cs.fontSize) || 16;
    const weight = Number(cs.fontWeight) || 400;
    const large = size >= 24 || (size >= 18.66 && weight >= 700);
    const required = large ? 3 : 4.5;
    if (ratio + 0.005 < required) {
      push(lowContrast, {
        element: describe(el), ratio: Math.round(ratio * 100) / 100, required,
        color: cs.color, background: `rgb(${Math.round(bg.r)}, ${Math.round(bg.g)}, ${Math.round(bg.b)})`,
        font_size_px: Math.round(size * 10) / 10,
      });
    }
  }

  return {
    horizontal_overflow: overflow,
    interactive_count: interactiveCount,
    small_targets: smallTargets,
    targets_under_44: under44,
    unnamed_controls: unnamed,
    images_missing_alt: missingAlt,
    broken_images: broken,
    low_contrast: lowContrast,
    contrast_checked: contrastChecked,
    contrast_unmeasured: contrastUnmeasured,
  };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.spec || !args.out) {
    console.error('usage: node ui_render_probe.mjs --spec <spec.json> --out <dir>');
    process.exit(2);
  }
  const spec = JSON.parse(readFileSync(args.spec, 'utf8'));
  const outDir = resolve(args.out);
  mkdirSync(outDir, { recursive: true });

  const loaded = loadChromium();
  if (!loaded) {
    console.log(JSON.stringify({ status: 'skipped', reason: 'no Playwright driver found (looked for playwright, @playwright/test, playwright-core in the project and next to the plugin)' }));
    process.exit(3);
  }

  let browser;
  try {
    browser = await loaded.chromium.launch();
  } catch (err) {
    console.log(JSON.stringify({ status: 'skipped', reason: `browser could not start: ${String(err && err.message ? err.message : err).split('\n')[0]}` }));
    process.exit(3);
  }

  const widths = spec.widths || [320, 768, 1440];
  const height = spec.height || 900;
  const navTimeout = spec.nav_timeout_ms || 30000;
  const targets = [];
  try {
    for (const target of spec.targets || []) {
      const viewports = [];
      for (const width of widths) {
        const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 1 });
        const page = await context.newPage();
        const consoleErrors = [];
        page.on('console', (msg) => { if (msg.type() === 'error' && consoleErrors.length < MAX_ITEMS) consoleErrors.push(msg.text().slice(0, 300)); });
        page.on('pageerror', (err) => { if (consoleErrors.length < MAX_ITEMS) consoleErrors.push(`uncaught: ${String(err.message || err).slice(0, 300)}`); });
        const entry = { width, height, screenshot: null, load_error: null, console_errors: consoleErrors };
        try {
          const response = await page.goto(target.url, { waitUntil: 'load', timeout: navTimeout });
          if (response && response.status() >= 400) entry.load_error = `HTTP ${response.status()}`;
          await page.waitForLoadState('networkidle', { timeout: 5000 }).catch(() => {});
          if (target.wait_for) {
            await page.waitForSelector(target.wait_for, { state: 'visible', timeout: navTimeout }).catch(() => {
              entry.load_error = entry.load_error || `wait_for selector never became visible: ${target.wait_for}`;
            });
          }
          Object.assign(entry, await page.evaluate(measure, MAX_ITEMS));
          const file = `${target.id}-${width}.png`;
          await page.screenshot({ path: join(outDir, file), fullPage: true });
          entry.screenshot = file;
        } catch (err) {
          entry.load_error = String(err && err.message ? err.message : err).split('\n')[0].slice(0, 300);
        }
        await context.close();
        viewports.push(entry);
      }
      targets.push({ id: target.id, url: target.url, viewports });
    }
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify({ status: 'captured', driver: loaded.driver, widths, targets }));
}

main().catch((err) => {
  console.log(JSON.stringify({ status: 'failed', reason: String(err && err.stack ? err.stack : err).slice(0, 2000) }));
  process.exit(1);
});
