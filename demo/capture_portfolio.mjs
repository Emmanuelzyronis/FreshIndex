import fs from 'node:fs';
import path from 'node:path';
import playwright from '/home/emmanuelzyronis/node_modules/playwright/index.js';
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';

const out = process.argv[2] || 'demo/artifacts/portfolio-live/portfolio';
const port = Number(process.argv[3] || 8778);
fs.mkdirSync(out, { recursive: true });
const server = spawn('python3', ['-m', 'demo.evidence_page.server', '--port', String(port)], { stdio: 'ignore' });
await new Promise(r => setTimeout(r, 900));
const browser = await playwright.chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, recordVideo: { dir: out, size: { width: 1440, height: 900 } } });
const page = await context.newPage();
await page.goto(`http://127.0.0.1:${port}/`, { waitUntil: 'networkidle' });
await page.waitForTimeout(4500);
await page.screenshot({ path: path.join(out, 'hero.png') });
await page.locator('.architecture').screenshot({ path: path.join(out, 'architecture.png') });
await page.locator('.hero').scrollIntoViewIfNeeded();
await page.waitForTimeout(11000);
for (let index = 0; index < 4; index += 1) {
  await page.locator('.section').nth(index).scrollIntoViewIfNeeded();
  await page.waitForTimeout(11000);
}
await page.locator('.hero').scrollIntoViewIfNeeded();
await page.waitForTimeout(2500);
const video = await page.video().path();
await context.close();
await browser.close();
fs.renameSync(video, path.join(out, 'freshindex-demo.webm'));
server.kill('SIGTERM');
const evidence = fs.readdirSync('demo/artifacts').filter(x => fs.existsSync(path.join('demo/artifacts', x, 'evidence.json'))).sort().pop();
fs.copyFileSync(path.join('demo/artifacts', evidence, 'evidence.json'), path.join(out, 'evidence.json'));
console.log(out);
