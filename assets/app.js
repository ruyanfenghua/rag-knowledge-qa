/* 公共脚本：调用服务端 API + 渲染消息气泡。不含知识库、切块、检索、模型调用。 */
const BASE = location.origin.startsWith('http') ? '' : 'http://127.0.0.1:8900';
const VIEW = document.body.dataset.view || 'client';

async function api(path, opt){
  const o = Object.assign({}, opt);
  o.headers = Object.assign({'Content-Type':'application/json'}, opt && opt.headers);
  if(o.body && typeof o.body !== 'string') o.body = JSON.stringify(o.body);
  const r = await fetch(BASE + path, o);
  const d = await r.json().catch(()=>({error:'返回的内容看不懂'}));
  if(!r.ok) throw new Error(d.error || ('HTTP '+r.status));
  return d;
}

function esc(t){
  return String(t == null ? '' : t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function md(t){
  // 本地摘录会带出 Markdown 表格：表头做成小标题，数据行做成两列对齐
  const lines = String(t==null?'':t).split('\n');
  const isSep = (s)=>/^\|[\s:\-|]+\|$/.test(s);
  const out = [];
  let buf = [];
  const flush = ()=>{
    if(!buf.length) return;
    out.push(esc(buf.join('\n')).replace(/\n\n/g,'<br><br>').replace(/\n/g,'<br>'));
    buf = [];
  };
  for(let i=0;i<lines.length;i++){
    const s = lines[i].trim();
    if(isSep(s)) continue;
    if(s.startsWith('|') && s.endsWith('|')){
      const cells = s.slice(1,-1).split('|').map(x=>x.trim()).filter(Boolean);
      if(cells.length>=2){
        flush();
        if(lines[i+1] && isSep(lines[i+1])){
          out.push('<span class="kvt">'+esc(cells.join(' · '))+'</span>');
        } else {
          out.push('<span class="kv"><i>'+esc(cells[0])+'</i><b>'+esc(cells[1])+'</b></span>');
        }
        continue;
      }
    }
    buf.push(lines[i]);
  }
  flush();
  return out.join('');
}
function nowHM(){
  const d = new Date();
  return ('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2);
}
function toast(msg){
  const el = document.getElementById('toast');
  if(!el) return;
  el.textContent = msg; el.classList.add('on');
  clearTimeout(el._t); el._t = setTimeout(()=>el.classList.remove('on'), 2600);
}
function scrollThread(){
  const t = document.getElementById('thread');
  if(t) t.scrollTop = t.scrollHeight + 200;
}

const TYPING = '<span class="typing"><i></i><i></i><i></i></span>';
let SESSIONS = {active: [], waiting: []};   // 来自 /api/sessions
let CUR = null;                             // 当前会话 id
let CUR_MSGS = [];                          // 当前会话消息
let CUR_INFO = null;                        // 当前会话详情
let HISTORY = [];
const MAX_TURNS = 3;
let WAITING = false;

function waitText(at){
  if(!at) return '刚进来';
  const t = new Date(String(at).replace(/-/g, '/')).getTime();
  if(!t) return '刚进来';
  const m = Math.max(1, Math.round((Date.now() - t) / 60000));
  return '排了 ' + m + ' 分钟';
}
function hhmm(at){ return String(at || '').slice(11, 16); }

function sessionTurns(){
  return curMsgs().filter(m => m.role==='me' && m.text)
    .slice(-MAX_TURNS).map(m=>({q:m.text, a:''}));
}
function historyText(){
  return HISTORY.slice(-MAX_TURNS).map(h=>'问：'+h.q+'\n答：'+h.a).join('\n');
}

const ADMIN_MSGS = [];   // 运维页的调试对话，与坐席会话分开
const CLIENT_MSGS = [{role:'other', html:'我是小云，说说看遇到什么了。', t:''}];
function curMsgs(){
  if(VIEW==='admin') return ADMIN_MSGS;
  if(VIEW==='client') return CLIENT_MSGS;
  return CUR_MSGS;
}

function renderThread(){
  const t = document.getElementById('thread');
  if(!t) return;
  const list = curMsgs();
  t.innerHTML = list.length ? list.map(m=>{
    if(m.role==='sys') return '<div class="sysline">'+esc(m.text)+'</div>';
    const tm = '<div class="t">'+esc(m.t||'')+'</div>';
    if(m.role==='me') return '<div class="msg me"><div class="bub"><div class="say">'+esc(m.text)+'</div>'+tm+'</div></div>';
    return '<div class="msg bot"><div class="bub"><div class="say">'+(m.html||'')+'</div>'+tm+'</div></div>';
  }).join('') : '<div class="sysline">'+(VIEW==='admin'?'问一句，下面会列出查到的段落和拼好的话':'这儿还空着')+'</div>';
  scrollThread();
}
function addUser(text){ curMsgs().push({role:'me',text,t:nowHM()}); renderThread(); }
function addBot(html){ const list=curMsgs(); list.push({role:'bot',html,t:nowHM()}); renderThread(); return list.length-1; }
function finishBot(i, html){ const list=curMsgs(); if(list[i]) list[i].html=html; renderThread(); }
function setWaiting(on){
  WAITING = on;
  const sub = document.getElementById('peer-sub');
  if(sub && VIEW==='client') sub.textContent = on ? '对方在打字…' : '在，问吧';
}
function renderChatList(){
  if(VIEW!=='agent') return;
  const act = SESSIONS.active || [], q = SESSIONS.waiting || [];
  const box = document.getElementById('chatlist');
  if(box) box.innerHTML = act.length ? act.map(c=>{
    const last = c.last_message || '还没开口';
    return `<div class="litem${c.id===CUR?' on':''}" onclick="switchChat('${c.id}')">
      <div class="lt"><span class="ln">${esc(c.visitor||'访客')}</span><span class="ltt">${hhmm(c.last_at||c.updated_at)}</span></div>
      <div class="lb"><span class="lp">${esc(last)}</span><span class="ls">${esc(c.channel||'网页')}</span></div>
    </div>`;
  }).join('') : '<div class="lp" style="padding:12px 16px">没有正在进行的会话</div>';
  const qbox = document.getElementById('queuelist');
  if(qbox) qbox.innerHTML = q.length ? q.map(c=>`<div class="qi" onclick="takeChat('${c.id}')">${esc(c.visitor||'访客')} · ${waitText(c.created_at)}</div>`).join('') : '<div class="qi">没有排队</div>';
  const na = document.getElementById('n-active'); if(na) na.textContent = act.length;
  const na2 = document.getElementById('n-active2'); if(na2) na2.textContent = act.length;
  const nq = document.getElementById('n-queue'); if(nq) nq.textContent = q.length;
}

async function loadSessions(){
  if(VIEW!=='agent') return;
  try{
    SESSIONS = await api('/api/sessions');
  }catch(e){
    SESSIONS = {active: [], waiting: []};
  }
  if(!CUR && (SESSIONS.active||[]).length) CUR = SESSIONS.active[0].id;
  renderChatList();
}
function filterChats(kw){
  kw = (kw||'').trim().toLowerCase();
  document.querySelectorAll('#chatlist .litem, #queuelist .qi').forEach(function(el){
    const hit = !kw || el.textContent.toLowerCase().indexOf(kw) >= 0;
    el.style.display = hit ? '' : 'none';
  });
}
function renderSidebar(d){
  const set=(k,v)=>{const e=document.getElementById(k); if(e)e.textContent=v||'—';};
  set('sidebar-name', d.visitor);
  set('sidebar-company', d.company);
  set('sidebar-visits', d.channel);
  set('side-host', d.host);
  set('sidebar-memo', d.memo || '无');
  const tags=document.getElementById('sidebar-tags');
  const list=String(d.tags||'').split(',').map(x=>x.trim()).filter(Boolean);
  if(tags) tags.innerHTML = list.length ? list.map(t=>'<span>'+esc(t)+'</span>').join('') : '—';
}

async function switchChat(id){
  CUR = id;
  HISTORY = [];
  try{
    const d = await api('/api/session?id=' + encodeURIComponent(id));
    CUR_INFO = d;
    CUR_MSGS = (d.messages || []).map(m=>{
      const t = hhmm(m.created_at);
      if(m.role === 'system') return {role:'sys', text:m.content, t};
      if(VIEW === 'agent'){
        return m.role === 'agent'
          ? {role:'me', text:m.content, t}
          : {role:'other', html:esc(m.content), t};
      }
      return m.role === 'visitor'
        ? {role:'me', text:m.content, t}
        : {role:'other', html:esc(m.content), t};
    });
    renderThread();
    renderSidebar(d);
    renderChatList();
    applyHeader();
  }catch(e){
    toast(e.message || '会话打不开');
  }
}

async function takeChat(id){
  try{
    await api('/api/session/take',{method:'POST',body:{id}});
    await loadSessions();
    await switchChat(id);
    toast('接上了');
  }catch(e){ toast(e.message || '接入失败'); }
}

async function endChat(){
  if(!CUR){ toast('先选一个会话'); return; }
  try{
    await api('/api/session/end',{method:'POST',body:{id:CUR}});
    toast('这轮结束了');
    const done = CUR;
    CUR = null; CUR_MSGS = []; CUR_INFO = null;
    await loadSessions();
    const next = (SESSIONS.active || []).find(x=>x.id !== done);
    if(next) await switchChat(next.id);
    else { renderThread(); applyHeader(); }
  }catch(e){ toast(e.message || '结束失败'); }
}

function fillReply(text){ const q=document.getElementById('q'); if(q){q.value=text;q.focus();} }

function applyHeader(){
  const name=document.getElementById('peer-name'), sub=document.getElementById('peer-sub');
  if(!name) return;
  if(VIEW==='agent'){
    name.textContent = (CUR_INFO && CUR_INFO.visitor) || '—';
    sub.textContent = CUR_INFO ? (CUR_INFO.channel||'网页') + '来的 · ' + (CUR_INFO.status==='waiting'?'等待接入':'正在接待') : '—';
  } else {
    name.textContent='小云';
    sub.textContent=WAITING?'对方在打字…':(VIEW==='client'?'在，问吧':'这轮调试');
  }
}

async function sendQuestion(q){
  const input=document.getElementById('q'), btn=document.getElementById('btn');
  if(btn) btn.disabled=true;
  addUser(q); SESSION.turns++;
  const bi=addBot(TYPING); setWaiting(true);
  try{
    const d=await api('/api/chat',{method:'POST',body:{q,history:sessionTurns().slice(0,-1),rewrite:false}});
    finishBot(bi, md(d.answer||''));
    HISTORY.push({q,a:String(d.answer||'').slice(0,300)});
    if(HISTORY.length>MAX_TURNS) HISTORY.shift();
    renderChatList();
    if(VIEW==='admin'){ renderDebug(d, q); loadQaLogs(); }
  }catch(e){
    const why=e.name==='TimeoutError'?'等了两分钟没动静':e.message;
    finishBot(bi,'<span class="text-danger">没等到回复，过会儿再试一次。（'+esc(why.slice(0,40))+'）</span>');
    toast('没发出去：'+why.slice(0,40));
  }finally{
    setWaiting(false); if(btn) btn.disabled=false;
  }
}

const SESSION={turns:0};
async function run(){
  const input=document.getElementById('q');
  if(!input) return;
  const q=input.value.trim();
  if(!q){ toast('写点什么再发'); return; }
  input.value='';
  if(VIEW==='agent'){
    if(!CUR){ toast('先选一个会话'); return; }
    addUser(q);
    try{
      await api('/api/session/message',{method:'POST',body:{id:CUR, role:'agent', content:q}});
      await loadSessions();
    }catch(e){ toast(e.message || '没发出去'); }
    return;
  }
  sendQuestion(q);
}
function askSample(q){ const i=document.getElementById('q'); if(i){i.value=q;run();} }

async function boot(){
  try{ const d=await api('/api/health'); HEALTH=d; }
  catch(e){ HEALTH=null; }
  const st=document.getElementById('agent-state');
  if(st){
    st.textContent = HEALTH ? '连接正常' : '连接中断';
    const dot=document.getElementById('agent-dot');
    if(dot) dot.style.background = HEALTH ? 'var(--ok)' : 'var(--err)';
  }
  renderThread(); applyHeader();
  if(VIEW==='agent'){
    await loadSessions();
    if(CUR) await switchChat(CUR);
    else renderChatList();
  }
  if(VIEW==='admin'){ await bootAdmin(); }
}
let HEALTH=null;

/* 运维页 */
async function bootAdmin(){
  await loadDocs();
  await loadQaLogs();
  const el=document.getElementById('sys-state');
  if(el) el.textContent = HEALTH ? '连上了' : '连不上';
  const dbEl=document.getElementById('db-state');
  if(dbEl){
    try{
      const d=await api('/api/db');
      const name = d.engine === 'mysql' ? 'MySQL' : 'SQLite';
      dbEl.textContent = '数据：' + name + (d.ok ? '' : '（未连上）');
      dbEl.title = d.where || '';
    }catch(e){ dbEl.textContent='数据：—'; }
  }
}

async function loadQaLogs(){
  const box=document.getElementById('qalog');
  if(!box) return;
  try{
    const d=await api('/api/qa?limit=8');
    const logs=d.logs||[];
    box.innerHTML = logs.map(x=>{
      const t=String(x.created_at||'').slice(11,16);
      return '<div class="row"><i>'+esc(t)+'</i><b>'+esc(x.question||'')+'</b></div>';
    }).join('');
    const cnt=document.getElementById('qa-count');
    if(cnt) cnt.textContent = logs.length ? '最近 '+logs.length+' 条' : '';
  }catch(e){ box.innerHTML=''; }
}
async function uploadDoc(input){
  const f = input.files && input.files[0];
  if(!f) return;
  if(f.size > 8*1024*1024){ toast('文件超过 8MB'); input.value=''; return; }
  toast('正在读 '+f.name);
  try{
    const buf = await f.arrayBuffer();
    const bytes = new Uint8Array(buf);
    let bin = '';
    for(let i=0;i<bytes.length;i+=0x8000){
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i+0x8000));
    }
    const d = await api('/api/upload',{method:'POST',body:{name:f.name, data:btoa(bin)}});
    toast('已加入：'+d.title+'，共 '+d.chars+' 字');
    await loadDocs();
  }catch(e){
    toast(e.message || '上传失败');
  }
  input.value='';
}

async function loadDocs(){
  try{
    const d=await api('/api/docs');
    const box=document.getElementById('doclist');
    if(box) box.innerHTML=d.docs.map(x=>`<div class="doc" onclick="showDoc('${x.id}')">${esc(x.title)}</div>`).join('');
  }catch(e){ toast('资料没读出来'); }
}
async function showDoc(id){
  try{
    const d=await api('/api/doc?id='+encodeURIComponent(id));
    currentDoc=id;
    const box=document.getElementById('doc-body');
    if(box) box.value=d.text;
    const title=document.getElementById('doc-title'); if(title) title.textContent=d.title;
    document.querySelectorAll('#doclist .doc').forEach(e=>e.classList.toggle('on',e.getAttribute('onclick').includes("'"+id+"'")));
  }catch(e){ toast(e.message); }
}
let currentDoc=null;
async function saveCurrentDoc(){
  if(!currentDoc){ toast('先从左边挑一份'); return; }
  const text=document.getElementById('doc-body').value;
  await api('/api/doc',{method:'POST',body:{title:currentDoc,text}});
  toast('存好了，回答已刷新'); await loadDocs();
}
async function resetAllDocs(){
  if(!confirm('要还原成初始资料吗？现在的改动会被盖掉。')) return;
  await api('/api/docs/reset',{method:'POST',body:{}});
  currentDoc=null; const b=document.getElementById('doc-body'); if(b)b.value='';
  await loadDocs(); toast('已经还原');
}
function snippet(t, n){
  // 检索片段里会带 Markdown 表格，压成「项目：值」的一段话再展示
  const lines = String(t==null?'':t).split('\n');
  const out = [];
  for(const raw of lines){
    const s = raw.trim();
    if(!s) continue;
    if(/^\|[\s:\-|]+\|$/.test(s)) continue;
    if(s.startsWith('|') && s.endsWith('|')){
      const cells = s.slice(1,-1).split('|').map(x=>x.trim()).filter(Boolean);
      if(cells.length>=2){ out.push(cells.join('：')); continue; }
    }
    out.push(s);
  }
  const text = out.join('  ');
  return text.length > n ? text.slice(0, n) + '…' : text;
}

function renderDebug(d, q){
  const tr=d.trace||{};
  const badge=document.getElementById('ans-badge'); if(badge) badge.textContent=d.model||'服务端';
  const hits=document.getElementById('hits');
  if(hits) hits.innerHTML=(d.hits||[]).map((x,i)=>`<div class="hit"><div class="ht"><b>${i+1}. ${esc(x.doc)}${x.section?' · '+esc(x.section):''}</b><span>${(x.sim*100).toFixed(1)}%</span></div><div class="hx">${esc(snippet(x.text, 180))}</div></div>`).join('')||'<div class="sysline">没找到对应内容</div>';
  const pt=document.getElementById('prompt'); if(pt) pt.textContent=d.prompt||'';
  const note=document.getElementById('hit-note');
  if(note) note.textContent=(tr.by==='vector'?'向量查找':'关键词查找')+(tr.intent&&tr.intent!=='rag'?' · 闲聊，没查资料':'');
  const nt=document.getElementById('ans-notice'); if(nt) nt.textContent=d.notice||'';
}
document.addEventListener('DOMContentLoaded', boot);
