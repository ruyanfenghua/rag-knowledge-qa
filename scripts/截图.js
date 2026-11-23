/*
 * 截图脚本：分别打开客户、坐席、运维三个页面截图。
 * 用法：node scripts/截图.js <临时目录> <页面所在目录> docs/images
 */
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');

const TMP = process.argv[2];
const DIR = process.argv[3];
const OUT = process.argv[4];
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const PORT = 9336;
const sleep = ms => new Promise(r => setTimeout(r, ms));
const chrome = spawn(CHROME, [
  '--headless=new', '--no-first-run', '--no-default-browser-check',
  '--remote-debugging-port=' + PORT, '--user-data-dir=' + path.join(TMP, 'profile2'),
  '--allow-file-access-from-files', '--window-size=1440,900', '--disable-gpu',
  'about:blank'
], { stdio: 'ignore' });
const watchdog = setTimeout(() => { console.error('总超时'); try { chrome.kill(); } catch (e) {} process.exit(2); }, 180000);

let ws, seq = 0; const pending = new Map();
function send(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++seq; pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
}
async function ev(expr) {
  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.text);
  return r.result && r.result.value;
}

const shots = [
  ['客户页.html', '客户侧.png'],
  ['坐席页.html', '坐席工作台.png'],
  ['运维页.html', '运维调试.png']
];

(async () => {
  let target = null;
  for (let i = 0; i < 80; i++) {
    try {
      const list = await fetch('http://127.0.0.1:' + PORT + '/json/list').then(r => r.json());
      target = list.find(t => t.type === 'page');
      if (target) break;
    } catch (e) {}
    await sleep(400);
  }
  if (!target) throw new Error('找不到页面');
  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  ws.onmessage = e => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) {
      const p = pending.get(m.id); pending.delete(m.id);
      m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result);
    }
  };
  await send('Runtime.enable');
  await send('Page.enable');
  fs.mkdirSync(OUT, { recursive: true });

  for (const [file, name] of shots) {
    const url = pathToFileURL(path.join(DIR, file)).href;
    await send('Page.navigate', { url });
    for (let i = 0; i < 120; i++) {
      const v = await ev("(()=>{try{return document.readyState==='complete' && typeof run==='function'}catch(e){return false}})()");
      if (v === true) break;
      await sleep(400);
    }
    await sleep(1500);
    if (file === '客户页.html') {
      await ev("(()=>{document.getElementById('q').value='退款多久到账';run();return 1})()");
      await sleep(3500);
    }
    await ev("(()=>{switchChat('c1');renderThread();renderChatList();updateSide();scrollThread();return 1})()");
    await sleep(500);
    const r = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
    fs.writeFileSync(path.join(OUT, name), Buffer.from(r.data, 'base64'));
    console.log('已截', name);
  }
})().catch(e => {
  console.error('失败', String(e));
  process.exitCode = 1;
}).finally(() => {
  clearTimeout(watchdog);
  try { ws && ws.close(); } catch (e) {}
  try { chrome.kill(); } catch (e) {}
  setTimeout(() => process.exit(process.exitCode || 0), 500);
});
