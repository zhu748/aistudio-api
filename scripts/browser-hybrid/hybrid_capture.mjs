// 混合捕获主脚本（复现脚本，参数化）
//
// 架构: 浏览器 UI 全速自然运转 + GenerateContent 由 httpx 代发 (route.fulfill 回填)
// 目标: 观察 AI Studio 页面每轮的原始请求头/请求体 (工具调用历史 + 签名), 且不 403
//
// 环境变量:
//   HYBRID_OUT        捕获落盘目录（必需）
//   HYBRID_AUTH_FILE  账号 auth.json 路径（必需，与 mint_server 一致）
//   UPSTREAM_SOCKS_*  认证代理参数（socks5_bridge 用）
//   TURNS_JSON        多轮提示词 JSON 数组（默认内置 3 轮）
//
// 用法: node hybrid_capture.mjs
// 原理见 docs/BROWSER_HYBRID_403_2026-11-14.md
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import fs from 'node:fs';

const OUT = process.env.HYBRID_OUT;
const AUTH_PATH = process.env.HYBRID_AUTH_FILE;
if (!OUT || !AUTH_PATH) {
  console.error('missing HYBRID_OUT / HYBRID_AUTH_FILE env');
  process.exit(1);
}
fs.mkdirSync(OUT, { recursive: true });

const DEFAULT_TURNS = [
  'Please remember this: my favorite color is teal, and my cat is named Mochi. Also search the web for the latest news about the James Webb Space Telescope and tell me one interesting fact.',
  'What is my favorite color, and what did you learn from your web search?',
  'Based on everything so far, write one short paragraph summarizing our conversation.',
];
const TURNS = process.env.TURNS_JSON ? JSON.parse(process.env.TURNS_JSON) : DEFAULT_TURNS;

const bridge = spawn('node', [new URL('./socks5_bridge.mjs', import.meta.url).pathname], { stdio: ['ignore', 'pipe', 'pipe'] });
bridge.stdout.on('data', d => console.log('[bridge]', String(d).trim()));
const backend = spawn('/home/z/aistudio-api/.venv/bin/python', [new URL('./mint_server.py', import.meta.url).pathname], { stdio: ['ignore', 'pipe', 'pipe'], cwd: '/home/z/aistudio-api' });
backend.stdout.on('data', d => console.log('[backend]', String(d).trim()));
backend.stderr.on('data', d => console.error('[backend-err]', String(d).trim()));

async function waitBackend() {
  for (let i = 0; i < 40; i++) {
    try { const r = await fetch('http://127.0.0.1:8765/health'); if (r.ok) return; } catch {}
    await new Promise(r => setTimeout(r, 500));
  }
  throw new Error('backend not ready');
}
await waitBackend();
console.log('[main] backend ready');

const AUTH = JSON.parse(fs.readFileSync(AUTH_PATH, 'utf8'));
// Cookie 装载要点: 域 .google.com / secure；__Secure-1PAPISID、__Secure-3PAPISID、
// SEARCH_SAMESITE 必须排除 httpOnly 才能注入。
const cookies = AUTH.cookies.map(c => ({
  name: c.name, value: c.value, domain: '.google.com', path: '/',
  expires: -1,
  httpOnly: !c.name.startsWith('__Secure-1PAPISID') && !c.name.startsWith('__Secure-3PAPISID') && c.name !== 'SEARCH_SAMESITE',
  secure: true, sameSite: 'Lax',
}));

const browser = await chromium.launch({
  headless: true,
  proxy: { server: 'socks5://127.0.0.1:11080' },
  args: ['--no-sandbox', '--disable-blink-features=AutomationControlled'],
});

let n = 0;
try {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    locale: 'zh-CN', timeZone: 'America/New_York',
  });
  await context.addCookies(cookies);
  const page = await context.newPage();
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  });

  // GenerateContent: 观察 + 代发 + 回填
  await page.route(/\$rpc.*GenerateContent/, async route => {
    const req = route.request();
    if (req.method() !== 'POST') return route.continue();
    const rawBody = req.postData();
    if (!rawBody) return route.continue();
    const url = req.url();

    n += 1;
    const seq = String(n).padStart(2, '0');
    // 观察目标 1: 页面自然发出的完整请求头
    fs.writeFileSync(`${OUT}/req_${seq}_natural_headers.json`, JSON.stringify(req.headers(), null, 1));
    // 观察目标 2: 页面自然发出的请求体 (contents / tools / functionCall / 签名)
    let bodyObj = null;
    try { bodyObj = JSON.parse(rawBody); } catch {}
    fs.writeFileSync(`${OUT}/req_${seq}_natural_body.json`, JSON.stringify(bodyObj, null, 1));
    console.log(`[observe] #${n} ${url.split('MakerSuiteService/')[1]} bodyLen=${rawBody.length}`);

    // proof 换成远程签名服务铸的 (浏览器内 BotGuard 的 proof 环境被标记为自动化)
    // 仅 GenerateContent 类 RPC 做 proof 替换, 其他 $rpc (CountTokens 等) 原样放行
    let finalBody = rawBody;
    try {
      if (/GenerateContent/.test(url) && bodyObj && Array.isArray(bodyObj) && bodyObj[0]) {
        const r = await fetch('http://127.0.0.1:8765/mint', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model: bodyObj[0], wireContents: bodyObj[1] }),
        });
        const data = await r.json();
        if (data.ok) {
          bodyObj[4] = data.proof; // SNAPSHOT 槽位
          finalBody = JSON.stringify(bodyObj);
          console.log(`[mint] #${n} proof swapped (${data.proof.length}B)`);
        } else {
          console.log(`[mint] #${n} FAILED: ${data.error} — 用页面原生 proof`);
        }
      }
    } catch (e) {
      console.error(`[mint] #${n} err: ${e.message}`);
    }

    // 代发 (httpx 传输层; 头集合由 mint_server /send 完全重建)
    try {
      const r = await fetch('http://127.0.0.1:8765/send', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url, headers: req.headers(), body: finalBody }),
      });
      const data = await r.json();
      if (data.ok) {
        fs.writeFileSync(`${OUT}/resp_${seq}_status${data.status}.json`, data.body || '');
        console.log(`[send] #${n} status=${data.status} len=${(data.body || '').length}`);
        await route.fulfill({
          status: data.status,
          contentType: data.headers?.['content-type'] || 'application/json+protobuf',
          body: data.body || '',
        });
      } else {
        console.error(`[send] #${n} backend error: ${data.error}`);
        await route.continue();
      }
    } catch (e) {
      console.error(`[send] #${n} fetch err: ${e.message}`);
      await route.continue();
    }
  });

  // batchexecute (会话持久化) 观察点
  let bx = 0;
  page.on('request', req => {
    const u = req.url();
    if (u.includes('batchexecute') && req.method() === 'POST' && req.postData()) {
      bx += 1;
      fs.writeFileSync(`${OUT}/batchexec_${String(bx).padStart(2, '0')}.txt`,
        u.split('?')[1]?.slice(0, 300) + '\n\n' + req.postData());
      console.log(`[batchexec] #${bx} len=${req.postData().length}`);
    }
  });

  let rc = 0;
  page.on('response', async resp => {
    if (!/GenerateContent/.test(resp.url())) return;
    rc += 1;
    console.log(`[page] sees response #${rc} status=${resp.status()}`);
  });

  await page.goto('https://aistudio.google.com/prompts/new_chat', { waitUntil: 'domcontentloaded', timeout: 90000 });
  await page.waitForTimeout(10000);
  console.log('[main] page ready:', page.url());

  const textarea = page.locator('textarea, rich-textarea div[contenteditable="true"]').first();
  await textarea.waitFor({ state: 'visible', timeout: 45000 });
  await textarea.click();

  for (let i = 0; i < TURNS.length; i++) {
    console.log(`[main] turn ${i + 1}/${TURNS.length}...`);
    await page.keyboard.type(TURNS[i], { delay: 8 });
    await page.keyboard.press('Control+Enter'); // AI Studio 提交键是 Ctrl+Enter
    await page.waitForTimeout(55000 + i * 10000);
    await page.screenshot({ path: `${OUT}/turn${i + 1}.png` });
  }

  console.log(`[done] observed=${n} pageResponses=${rc} batchexec=${bx}`);
} finally {
  await browser.close();
  bridge.kill();
  backend.kill();
}
