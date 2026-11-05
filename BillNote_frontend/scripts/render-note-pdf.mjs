#!/usr/bin/env node
/**
 * Print trusted, sanitized note HTML to PDF using the same KaTeX as the UI.
 * stdin: {html: string, expressions: [{tex: string, display: boolean}]}
 * The HTML contains <!--BILINOTE_MATH_0--> etc.; screenshots are data URIs.
 * stdout: binary PDF. Diagnostics go to stderr.
 *
 * Browser printing handles formula baselines, fonts and pagination together;
 * no formula screenshots, TeX parser, or browser download are required.
 */
import process from 'node:process';
import path from 'node:path';
import {readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import katex from 'katex';
import {chromium} from 'playwright-core';

const require = createRequire(import.meta.url);
const MAX_INPUT_BYTES = 256 * 1024 * 1024;
const MAX_EXPRESSIONS = 1000;
const MAX_TEX_CHARS = 8192;
const TIMEOUT_MS = 20_000;
const DEADLINE_MS = 45_000;
const deadlineAt = Date.now() + DEADLINE_MS;
let stopping = false;

function remainingTimeout() {
  if (stopping || Date.now() >= deadlineAt) throw new Error('PDF export timed out');
  return Math.max(1, Math.min(TIMEOUT_MS, deadlineAt - Date.now()));
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[character]));
}

async function readRequest() {
  let size = 0;
  const chunks = [];
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > MAX_INPUT_BYTES) throw new Error('PDF input exceeds the size limit');
    chunks.push(chunk);
  }
  const request = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  if (!request || typeof request.html !== 'string' || !Array.isArray(request.expressions)) {
    throw new Error('Expected note HTML and an expressions array');
  }
  if (request.expressions.length > MAX_EXPRESSIONS) throw new Error('Too many math expressions');
  return request;
}

function renderMath(expression) {
  const tex = typeof expression?.tex === 'string' ? expression.tex : '';
  try {
    if (typeof expression?.display !== 'boolean' || !tex.trim()) {
      throw new Error('Invalid math expression');
    }
    if (tex.length > MAX_TEX_CHARS) throw new Error('Math expression exceeds the size limit');
    return katex.renderToString(tex, {
      displayMode: expression.display,
      throwOnError: true,
      trust: false,
      strict: 'ignore',
      maxExpand: 1000,
      maxSize: 50,
      // Fresh macros per expression prevent definitions leaking across math.
      macros: {},
      output: 'htmlAndMathml',
    });
  } catch {
    // Keep the complete source visible and escaped if notation is unsupported.
    const tag = expression?.display ? 'div' : 'span';
    return `<${tag} class="pdf-math-error"><span>公式无法排版，保留原文：</span>`
      + `<code>${escapeHtml(tex)}</code></${tag}>`;
  }
}

async function embeddedKatexCss() {
  const katexRoot = path.dirname(require.resolve('katex/package.json'));
  const dist = path.join(katexRoot, 'dist');
  const css = await readFile(path.join(dist, 'katex.min.css'), 'utf8');
  const replacements = new Map();
  // KaTeX's own stylesheet has a woff2 source followed by older font formats.
  // Chromium uses woff2; embed only that source to keep the PDF input compact.
  for (const match of css.matchAll(/src\s*:[^;}]+/g)) {
    const font = /url\((['"]?)(fonts\/[A-Za-z0-9_.-]+\.woff2)\1\)/.exec(match[0]);
    if (!font) throw new Error('Unsupported KaTeX font stylesheet');
    if (!replacements.has(font[2])) {
      const data = await readFile(path.join(dist, font[2]));
      replacements.set(font[2], `url(data:font/woff2;base64,${data.toString('base64')}) format('woff2')`);
    }
  }
  return css.replace(/src\s*:[^;}]+/g, declaration => {
    const font = /url\((['"]?)(fonts\/[A-Za-z0-9_.-]+\.woff2)\1\)/.exec(declaration);
    return `src:${replacements.get(font[2])}`;
  });
}

const PRINT_CSS = `
html { width: calc(210mm - 84pt); }
body { margin: 0; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.katex-display { break-inside: avoid; page-break-inside: avoid; overflow: visible; }
.pdf-math-error { color: #8c351b; overflow-wrap: anywhere; }
.pdf-math-error code { white-space: pre-wrap; }
@page { size: A4; }
`;

async function prepareHtml(html, expressions) {
  const rendered = expressions.map(renderMath);
  const body = html.replace(/<!--BILINOTE_MATH_(\d+)-->/g, (_marker, index) => {
    const result = rendered[Number(index)];
    if (result === undefined) throw new Error('Missing math expression');
    return result;
  });
  const css = await embeddedKatexCss();
  const head = '<meta charset="utf-8">'
    + '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
    + 'script-src \'none\'; style-src \'unsafe-inline\'; img-src data:; font-src data:; '
    + 'base-uri \'none\'; form-action \'none\'">'
    + `<style>${css}\n${PRINT_CSS}</style>`;
  if (/<head(?:\s[^>]*)?>/i.test(body)) {
    return body.replace(/<head(?:\s[^>]*)?>/i, opening => opening + head);
  }
  if (/<html(?:\s[^>]*)?>/i.test(body)) {
    return body.replace(/<html(?:\s[^>]*)?>/i, opening => opening + `<head>${head}</head>`);
  }
  return `<!doctype html><html><head>${head}</head><body>${body}</body></html>`;
}

async function launchBrowser() {
  const options = {headless: true, chromiumSandbox: true};
  async function launch(extra = {}) {
    const candidate = await chromium.launch({...options, ...extra, timeout: remainingTimeout()});
    if (stopping) {
      await candidate.close();
      throw new Error('PDF export interrupted');
    }
    return candidate;
  }
  const executablePath = process.env.BILINOTE_PDF_BROWSER?.trim();
  if (executablePath) return launch({executablePath});
  for (const channel of ['msedge', 'chrome']) {
    try {
      return await launch({channel});
    } catch {
      // Try the next supported, already-installed Chromium browser.
    }
  }
  try {
    // This only uses an existing Playwright installation; never downloads one.
    return await launch();
  } catch {
    throw new Error('PDF export needs an installed Edge, Chrome, or Playwright Chromium browser');
  }
}

let browser;
async function renderPdf() {
  const request = await readRequest();
  const html = await prepareHtml(request.html, request.expressions);
  browser = await launchBrowser();
  const context = await browser.newContext({
    javaScriptEnabled: false,
    offline: true,
    serviceWorkers: 'block',
    acceptDownloads: false,
    viewport: {width: 682, height: 1123},
  });
  await context.route('**/*', route => route.abort());
  const page = await context.newPage();
  page.setDefaultTimeout(remainingTimeout());
  await page.emulateMedia({media: 'print'});
  await page.setContent(html, {waitUntil: 'load', timeout: remainingTimeout()});
  // evaluate() remains available to Playwright with page scripts disabled.
  // All resources are embedded, and nothing is fetched from the note's links.
  await page.evaluate(async () => {
    await document.fonts.ready;
    await Promise.all([...document.images].map(image => image.decode()));
    // Leave room for a neighboring text line and paragraph margins as well as
    // the mathematical box, so a page-sized matrix remains on one page.
    const pageHeight = 297 / 25.4 * 96 - 88 / 72 * 96 - 48;
    const metrics = math => {
      const boxes = [math, ...math.querySelectorAll(':scope > .katex-html > .base')]
        .map(element => element.getBoundingClientRect());
      return {
        width: Math.max(math.scrollWidth, ...boxes.map(box => box.width)),
        height: Math.max(...boxes.map(box => box.height)),
      };
    };
    for (const display of document.querySelectorAll('.katex-display')) {
      const math = display.querySelector('.katex');
      if (!math) continue;
      // Browser metrics handle the font and its CSS. Shrink only overflowing
      // display equations, leaving all ordinary text and inline math intact.
      for (let attempt = 0; attempt < 3; attempt += 1) {
        const available = display.getBoundingClientRect().width;
        const measured = metrics(math);
        const needed = measured.width;
        const style = getComputedStyle(display);
        const availableHeight = pageHeight - Number.parseFloat(style.marginTop)
          - Number.parseFloat(style.marginBottom);
        const neededHeight = measured.height;
        const ratio = Math.min(available / needed, availableHeight / neededHeight, 1);
        if (!available || ratio >= 0.999) break;
        const size = Number.parseFloat(style.fontSize);
        display.style.fontSize = `${size * ratio * 0.99}px`;
      }
    }
    for (const math of document.querySelectorAll('.katex')) {
      if (math.closest('.katex-display')) continue;
      let container = math.parentElement;
      while (container?.parentElement && getComputedStyle(container).display === 'inline') {
        container = container.parentElement;
      }
      if (!container) continue;
      const style = getComputedStyle(container);
      const available = container.clientWidth - Number.parseFloat(style.paddingLeft)
        - Number.parseFloat(style.paddingRight);
      const rect = metrics(math);
      const ratio = Math.min(available / rect.width, pageHeight / rect.height, 1);
      if (available && ratio < 0.999) {
        const size = Number.parseFloat(getComputedStyle(math).fontSize);
        math.style.fontSize = `${size * ratio * 0.99}px`;
      }
    }
  });
  const pdf = await page.pdf({
    format: 'A4',
    // page.pdf accepts px/in/cm/mm; CSS pt values are converted exactly here.
    margin: {top: `${44 / 72}in`, bottom: `${44 / 72}in`, left: `${42 / 72}in`, right: `${42 / 72}in`},
    printBackground: true,
    displayHeaderFooter: true,
    headerTemplate: '<div></div>',
    footerTemplate: '<div style="font-size:8pt;color:#888;width:100%;text-align:center;">'
      + '<span class="pageNumber"></span> / <span class="totalPages"></span></div>',
    preferCSSPageSize: true,
    tagged: true,
    timeout: remainingTimeout(),
  });
  return pdf;
}

let rejectStop;
const stopPromise = new Promise((_resolve, reject) => { rejectStop = reject; });
const deadline = setTimeout(() => {
  stopping = true;
  rejectStop(new Error('PDF export timed out'));
}, DEADLINE_MS);
const onSignal = () => {
  stopping = true;
  rejectStop(new Error('PDF export interrupted'));
};
process.once('SIGTERM', onSignal);
process.once('SIGINT', onSignal);
try {
  const pdf = await Promise.race([renderPdf(), stopPromise]);
  if (!stopping) process.stdout.write(pdf);
} catch (error) {
  process.stderr.write(`PDF renderer: ${String(error?.message ?? error).slice(0, 500)}\n`);
  process.exitCode = 1;
} finally {
  stopping = true;
  clearTimeout(deadline);
  process.removeListener('SIGTERM', onSignal);
  process.removeListener('SIGINT', onSignal);
  if (browser) await browser.close().catch(() => {});
}
