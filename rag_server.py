"""智云客服 RAG 服务。

把知识库加载、切块、检索、证据判定、提示词拼装和模型调用放在服务端，
页面只负责交互与展示。Key 写在本地 config.json，不进浏览器。

用法：双击 start-server.bat，然后打开 http://127.0.0.1:8900/
首次运行会生成 config.example.json 的副本 config.json，填好 Key 再启动。
"""

import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import db
import docparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KB_DIR = ROOT / '知识库样例'
CONFIG_PATH = ROOT / 'config.json'
EXAMPLE_PATH = ROOT / 'config.example.json'
VEC_PATH = ROOT / 'vectors.cache.json'

CHUNK_SIZE = 220
OVERLAP = 40
DOCS = []
CHUNKS = []
IDF = {}
VEC = {'ready': False, 'dim': 0, 'model': '', 'busy': False}
CONFIG = {}


def load_config():
    global CONFIG
    if not CONFIG_PATH.exists():
        sample = EXAMPLE_PATH.read_text(encoding='utf-8') if EXAMPLE_PATH.exists() else '{}'
        CONFIG_PATH.write_text(sample, encoding='utf-8')
    try:
        CONFIG = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    except Exception as e:
        print('配置读取失败：%s' % e, flush=True)
        CONFIG = {}
    CONFIG.setdefault('port', 8900)
    CONFIG.setdefault('chat', {})
    CONFIG.setdefault('emb', {})
    CONFIG.setdefault('fallback', {})
    # 配置文件没填 key 时，从环境变量取，避免把密钥写进文件
    if not CONFIG['chat'].get('key'):
        CONFIG['chat']['key'] = os.environ.get('SHANGTANG_API_KEY', '')
    if not CONFIG['emb'].get('key'):
        CONFIG['emb']['key'] = os.environ.get('SILICONFLOW_API_KEY', '')
    return CONFIG


def save_config(data):
    global CONFIG
    for k in ('chat', 'emb', 'fallback'):
        if isinstance(data.get(k), dict):
            CONFIG[k].update(data[k])
    if isinstance(data.get('port'), int):
        CONFIG['port'] = data['port']
    CONFIG_PATH.write_text(json.dumps(CONFIG, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def public_config():
    def view(x):
        return {'base': x.get('base', ''), 'model': x.get('model', ''),
                'key_set': bool(x.get('key'))}
    return {'chat': view(CONFIG.get('chat', {})),
            'emb': view(CONFIG.get('emb', {})),
            'fallback': view(CONFIG.get('fallback', {})),
            'port': CONFIG.get('port', 8900)}


# ---------- 知识库与索引 ----------

ORIGINAL_DOCS = {}


def load_docs():
    global DOCS, CHUNKS, IDF, ORIGINAL_DOCS
    DOCS = []
    for f in sorted(list(KB_DIR.glob('*.md')) + list(KB_DIR.glob('*.txt'))):
        DOCS.append({'id': f.stem, 'title': f.stem.split('-', 1)[-1],
                     'text': f.read_text(encoding='utf-8')})
    if not ORIGINAL_DOCS:
        ORIGINAL_DOCS = {d['id']: d['text'] for d in DOCS}
    CHUNKS = split_chunks()
    IDF = build_index()
    try:
        db.sync_documents(DOCS, [c['doc']['id'] for c in CHUNKS])
    except Exception as e:
        print('资料入库失败：%s' % e, flush=True)
    print('知识库：%d 份文档，%d 个切块' % (len(DOCS), len(CHUNKS)), flush=True)


def doc_detail(doc_id):
    d = next((x for x in DOCS if x['id'] == doc_id), None)
    if not d:
        return None
    cs = [{'idx': c['idx'] + 1, 'section': c.get('section', ''), 'text': c['text']}
          for c in CHUNKS if c['doc']['id'] == doc_id]
    return {'id': d['id'], 'title': d['title'], 'chars': len(d['text']),
            'text': d['text'], 'chunks': cs}


def save_doc(title, text):
    name = re.sub(r'[\\/:*?"<>|]', '_', title).strip() or '未命名'
    f = KB_DIR / (name + '.md')
    f.write_text(text, encoding='utf-8')
    load_docs()
    return doc_detail(f.stem)


def delete_doc(doc_id):
    f = KB_DIR / (doc_id + '.md')
    if not f.exists():
        return False
    f.unlink()
    if VEC_PATH.exists():
        VEC_PATH.unlink()
    load_docs()
    return True


def reset_docs():
    for f in KB_DIR.glob('*.md'):
        f.unlink()
    for doc_id, text in ORIGINAL_DOCS.items():
        (KB_DIR / (doc_id + '.md')).write_text(text, encoding='utf-8')
    if VEC_PATH.exists():
        VEC_PATH.unlink()
    load_docs()
    return len(DOCS)


def split_chunks():
    out = []
    for d in DOCS:
        # 文档第一行是标题，其余按「一、二、三」这类中文序号分节
        lines = d['text'].split('\n')
        doc_title = lines[0].strip() if lines else ''
        rest = '\n'.join(lines[1:]).strip()
        secs = [x.strip() for x in re.split(r'\n(?=[一二三四五六七八九十]+、)', rest) if x.strip()]
        idx = 0
        for sec in secs:
            head, sep, tail = sec.partition('\n')
            if sep and re.match(r'^[一二三四五六七八九十]+、', head.strip()):
                title = head.strip()
                body = tail.strip()
            else:
                title = doc_title
                body = sec
            if not body.strip():
                body = sec
            sents = [x for x in re.split(r'(?<=[。！？\n])\s*', body) if x.strip()]
            buf = ''

            def push():
                nonlocal buf, idx
                if buf.strip():
                    out.append({'doc': d, 'idx': idx, 'section': title,
                                'text': buf.strip(), 'vec': None})
                    idx += 1
                    buf = ''

            for x in sents:
                if len(buf + x) > CHUNK_SIZE and buf:
                    push()
                    buf = buf[-OVERLAP:] + x
                else:
                    buf += x
            push()
    return out


def tokenize(text):
    t = text.lower()
    t = re.sub(r'[^\u4e00-\u9fa5a-z0-9]', ' ', t)
    out = re.findall(r'[a-z0-9]{2,}', t)
    cn = re.sub(r'[^\u4e00-\u9fa5]', '', t)
    out += [cn[i:i + 2] for i in range(len(cn) - 1)]
    return out


def build_index():
    df = {}
    for c in CHUNKS:
        c['tf'] = {}
        for w in tokenize(c['text']):
            c['tf'][w] = c['tf'].get(w, 0) + 1
        for w in c['tf']:
            df[w] = df.get(w, 0) + 1
    n = len(CHUNKS) or 1
    idf = {w: math.log((n + 1) / (df[w] + 0.5)) for w in df}
    for c in CHUNKS:
        s = sum(((1 + math.log(v)) * idf[w]) ** 2 for w, v in c['tf'].items())
        c['norm'] = math.sqrt(s) or 1
    return idf


def tfidf_rank(q):
    qtf = {}
    for w in tokenize(q):
        qtf[w] = qtf.get(w, 0) + 1
    qv = {}
    qn = 0.0
    for w, n in qtf.items():
        if w not in IDF:
            continue
        v = (1 + math.log(n)) * IDF[w]
        qv[w] = v
        qn += v * v
    qn = math.sqrt(qn) or 1
    out = []
    for c in CHUNKS:
        dot = 0.0
        for w, v in qv.items():
            if w in c['tf']:
                dot += v * (1 + math.log(c['tf'][w])) * IDF[w]
        out.append({'chunk': c, 'sim': dot / (qn * c['norm']),
                    'words': list(qv.keys()), 'byVec': False})
    return sorted(out, key=lambda x: -x['sim'])


def cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    return dot / (math.sqrt(na) * math.sqrt(nb) or 1)


def vec_sig():
    return '|'.join(d['id'] + ':' + str(len(d['text'])) for d in DOCS) \
        + '#' + str(len(CHUNKS)) + '#' + CONFIG.get('emb', {}).get('model', '')


def normalize_base(base):
    b = (base or '').strip().rstrip('/')
    return re.sub(r'/(chat/completions|embeddings|models)$', '', b)


def api_ready(which='chat'):
    cfg = CONFIG.get(which, {})
    return bool(normalize_base(cfg.get('base'))) and bool(cfg.get('model')) and bool(cfg.get('key'))


def http_json(url, payload, key, timeout=90):
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'),
                                 headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def embed_batch(texts):
    cfg = CONFIG.get('emb', {})
    url = normalize_base(cfg.get('base')) + '/embeddings'
    key = cfg.get('key') or CONFIG.get('chat', {}).get('key', '')
    out = []
    for i in range(0, len(texts), 16):
        part = texts[i:i + 16]
        d = http_json(url, {'model': cfg.get('model'), 'input': part}, key, timeout=60)
        rows = d.get('data') or []
        if not rows:
            raise ValueError('向量接口没有返回数据')
        rows.sort(key=lambda x: x.get('index', 0))
        out.extend(x['embedding'] for x in rows)
    return out


def build_vectors():
    global VEC
    if not api_ready('emb') and not (CONFIG.get('emb', {}).get('key') or CONFIG.get('chat', {}).get('key')):
        return {'ok': False, 'error': '未配置向量模型'}
    VEC['busy'] = True
    try:
        vecs = embed_batch([c['text'] for c in CHUNKS])
        for c, v in zip(CHUNKS, vecs):
            c['vec'] = v
        VEC = {'ready': True, 'dim': len(vecs[0]), 'model': CONFIG['emb'].get('model', ''), 'busy': False}
        VEC_PATH.write_text(json.dumps({'sig': vec_sig(), 'dim': VEC['dim'],
                                        'model': VEC['model'], 'vecs': vecs}, ensure_ascii=False),
                            encoding='utf-8')
        return vector_status()
    except Exception as e:
        VEC = {'ready': False, 'dim': 0, 'model': '', 'busy': False}
        return {'ok': False, 'error': str(e)[:160]}
    finally:
        VEC['busy'] = False


def load_vectors():
    global VEC
    if not VEC_PATH.exists():
        return
    try:
        d = json.loads(VEC_PATH.read_text(encoding='utf-8'))
        if d.get('sig') != vec_sig() or len(d.get('vecs', [])) != len(CHUNKS):
            return
        for c, v in zip(CHUNKS, d['vecs']):
            c['vec'] = v
        VEC = {'ready': True, 'dim': d.get('dim', len(d['vecs'][0])),
               'model': d.get('model', ''), 'busy': False}
        print('向量缓存：%d 块 × %d 维' % (len(CHUNKS), VEC['dim']), flush=True)
    except Exception:
        pass


def vector_status():
    return {'ok': VEC['ready'], 'ready': VEC['ready'], 'dim': VEC['dim'],
            'model': VEC['model'], 'busy': VEC['busy'],
            'configured': bool(normalize_base(CONFIG.get('emb', {}).get('base')) and CONFIG.get('emb', {}).get('model'))}


async_rank = None  # 兼容旧调用，保留空位


def rank(q):
    if VEC['ready'] and api_ready('emb'):
        try:
            qv = embed_batch([q])[0]
            ranked = ({'chunk': c, 'sim': cosine(qv, c['vec']), 'words': [], 'byVec': True}
                      for c in CHUNKS)
            return sorted(ranked, key=lambda x: -x['sim'])
        except Exception:
            pass
    return tfidf_rank(q)


# ---------- 对话链路 ----------

SYS = """你是「小云」，云策的在线客服。公司在卖一套在线客服与工单系统，含网页客服端、管理后台和开放接口。

说话方式：
- 像真人在聊天窗口打字。短句，一次两到四句，别写成文档
- 称呼用「您」，语气平和，不用"亲""呢~"这类
- 不列"首先其次总结"，不加粗，不写小标题，不写编号
- 用户急或生气，直接给能用的信息，别描述自己正在做什么
- 不复述用户的话，不说"根据资料显示""为您解答如下"

怎么回答：
- 功能、价格、售后、部署、账号权限，看这一轮给你的资料，照资料说。数字、时限、条件必须准确
- 资料里没有的，直说这边查不到，不要猜。然后给一个下一步：换个问法、转人工、或者留联系方式让同事回
- 绝不编价格、时限、条款、人名、单号
- 不说"知识库""检索""相似度""阈值""资料中没有提到"这类内部说法，要说"这个我这边查不到"
- 被问是不是机器人，承认，别装人
- 用户接着上一轮追问，按上一轮的主题回答，别换成别的话题
- 只答用户问的，不主动推销。答完就停，不加「希望对您有帮助」「如需…可以…」这类收尾，不反问「还有别的吗」
- 别编你看不到的东西：排队人数、等待时长、按钮在哪个位置、工单号、同事姓名
- 用户要转人工，就说人工的工作时间，让他留个联系方式，别说"已为您排队第 3 位"这种"""

INTENT = {
    'greet': r'^(你好|您好|hi|hello|嗨|哈喽|在吗|在不在|有人吗|早上好|中午好|下午好|晚上好)[\s!！。.~，,]*$',
    'thanks': r'^(谢谢|感谢|多谢|thanks|thank you|thx)[\s!！。.~，,]*$',
    'bye': r'^(再见|拜拜|bye|结束|没事了|不用了|好的谢谢)[\s!！。.~，,]*$',
    'human': r'(转人工|人工客服|找人工|真人客服|人工服务|转客服)',
    'identity': r'(你是谁|你叫什么|你是什么|你是机器人|你是真人|你是人工|你是ai|你是AI|你哪位)'
}
REPLY = {
    'greet': '您好，我是智能客服。可以问我产品功能、售后政策、部署运维、账号权限这几类问题，也可以直接点下面的常见问题。',
    'thanks': '不客气。还有其他问题随时问。',
    'bye': '好的，祝您顺利。有需要再找我。',
    'human': '好的，正在为您转接人工客服。工作时间 9:00 至 18:00，非工作时间请留言，次日回复。',
    'identity': '我是智云客服的智能助手，负责产品功能、售后政策、部署运维和账号权限这几类问题。人工客服工作日 9:00 至 18:00 在线。'
}


def detect_intent(q):
    for k in ('greet', 'thanks', 'bye', 'human', 'identity'):
        if re.search(INTENT[k], q.strip(), re.I):
            return k
    return 'rag'


def call_chat(messages, temperature=0, max_tokens=800, cfg=None):
    cfg = cfg or CONFIG.get('chat', {})
    url = normalize_base(cfg.get('base')) + '/chat/completions'
    payload = {'model': cfg.get('model'), 'messages': messages,
               'temperature': temperature, 'max_tokens': max_tokens}
    d = http_json(url, payload, cfg.get('key', ''), timeout=90)
    msg = (d.get('choices') or [{}])[0].get('message', {})
    txt = re.sub(r'</?think>', '', str(msg.get('content') or '')).strip()
    if not txt:
        raise ValueError('模型没有返回正文')
    return txt


def ask_agent(messages, opt=None):
    opt = opt or {}
    try:
        return call_chat(messages, opt.get('temperature', 0), opt.get('max_tokens', 800))
    except Exception as e:
        fb = CONFIG.get('fallback', {})
        if fb.get('key') and fb.get('model') and \
                normalize_base(fb.get('base')) and fb.get('model') != CONFIG.get('chat', {}).get('model'):
            try:
                return call_chat(messages, opt.get('temperature', 0), opt.get('max_tokens', 800), fb)
            except Exception:
                pass
        raise e


def history_text(history):
    if not history:
        return ''
    return '\n'.join('用户：%s\n客服：%s' % (h.get('q', ''), h.get('a', ''))
                     for h in history[-3:])


def rewrite_query(q, history):
    hist = history_text(history)
    prompt = ((('最近的对话：\n' + hist + '\n\n') if hist else '')
              + '把用户的问题改写成一句完整、适合在知识库里检索的话。\n'
              + ('如果这句话里有"这个""那个""它""那"这类指代，用上面的对话补全成具体对象。\n' if hist else '')
              + '规则：\n'
              + '1. 用户只说"多久到""几天""多少钱""能不能"这类省略句时，用上面客服最后说的那件事补全成完整问题\n'
              + '2. 只补全省略的主语宾语、只做同义替换，不增加原句没有的事实\n'
              + '3. 不回答问题，不解释，不加引号\n'
              + '4. 不超过 30 个字，只输出这一句\n\n'
              + '示例：\n问：买完后悔了\n改写：商品购买后想退货怎么处理\n'
              + '问：能开专票吗\n改写：增值税专用发票怎么开具\n'
              + '问：东西坏了\n改写：商品质量问题怎么退换\n'
              + '上面聊的是发票开具，问：多久到\n改写：发票多长时间能寄到\n\n'
              + '问：' + q + '\n改写：')
    try:
        r = ask_agent([{'role': 'user', 'content': prompt}],
                      {'temperature': 0, 'max_tokens': 600})
        r = (r.split('\n')[0] if r.split('\n') else '').strip()
        r = re.sub(r'^(改写|答|输出)[：:]\s*', '', r).strip('"\'「」『』 ').strip()
        if not r or len(r) > 60 or re.search(r'抱歉|无法|不知道|作为|以下', r) or r == q:
            return ''
        return r
    except Exception:
        return ''


def grade_evidence(q, cands):
    if not api_ready('chat') or not cands:
        return {'ok': False, 'use': [], 'missing': '', 'skip': True, 'sentiment': '平静'}
    listing = '\n\n'.join('[%d] %s · %s\n%s' % (
        i + 1, x['chunk']['doc']['title'], x['chunk'].get('section', ''), x['chunk']['text'])
        for i, x in enumerate(cands))
    prompt = ('问题：' + q + '\n\n候选资料：\n' + listing + '\n\n'
              + '判断这些资料能否回答问题，同时判断用户情绪。只输出 JSON，不要解释：\n'
              + '{"sufficient": true 或 false, "use": [能回答问题的资料编号], '
              + '"missing": "还缺什么信息，不缺就填空字符串", "sentiment": "平静 或 着急 或 不满 或 生气"}\n'
              + '注意：主题相近但答不了这个问题的，不算能用。情绪按用户这句话的措辞判断，拿不准就写"平静"。')
    try:
        r = ask_agent([{'role': 'user', 'content': prompt}], {'temperature': 0, 'max_tokens': 600})
        m = re.search(r'\{[\s\S]*\}', r)
        if not m:
            return {'ok': False, 'use': [], 'missing': '', 'skip': True, 'sentiment': '平静'}
        j = json.loads(m.group(0))
        use = []
        for n in (j.get('use') or []):
            try:
                i = int(n) - 1
            except Exception:
                continue
            if 0 <= i < len(cands):
                use.append(i)
        senti = j.get('sentiment') if j.get('sentiment') in ('平静', '着急', '不满', '生气') else '平静'
        return {'ok': bool(j.get('sufficient')), 'use': use,
                'missing': str(j.get('missing') or '')[:60], 'skip': False, 'sentiment': senti}
    except Exception as e:
        return {'ok': False, 'use': [], 'missing': '', 'skip': True,
                'sentiment': '平静', 'err': str(e)[:100]}


def local_answer(top, q):
    if not top:
        return '资料中没有找到相关内容。'
    words = tokenize(q)
    best, best_score = [], -1
    for x in top:
        for s in re.split(r'(?<=[。！？])', x['chunk']['text']):
            if not s.strip():
                continue
            sc = sum(1 for w in words if w in s)
            if sc > best_score:
                best_score, best = sc, [s.strip()]
            elif sc == best_score and sc > 0:
                best.append(s.strip())
    return ''.join(best[:2]) or '资料中没有找到相关内容。'


def hit_view(x):
    c = x['chunk']
    return {'doc': c['doc']['title'], 'section': c.get('section', ''),
            'idx': c['idx'] + 1, 'text': c['text'], 'sim': round(x['sim'], 4),
            'words': x.get('words', [])[:20], 'byVec': x.get('byVec', False)}


SEED_SESSIONS = [
    {'id': 'c1', 'visitor': '王女士', 'company': '杭州云栖贸易', 'channel': '网页', 'status': 'active',
     'tags': '合约中,开过票', 'memo': '问过坐席名额，说要跟主管商量。', 'host': '小云 · 自动',
     'msgs': [{'role': 'visitor', 'content': '专票能开吗', 'at': '2026-11-20 14:31'},
              {'role': 'agent', 'content': '能开。下单时写一句，或者成交后九十天内补申请。把开票信息备齐，我们审完三个工作日寄出。', 'at': '2026-11-20 14:32'}]},
    {'id': 'c2', 'visitor': '李先生', 'company': '宁波甬信物流', 'channel': '微信', 'status': 'active',
     'tags': '试用中', 'memo': '试用第五天，问过怎么部署。', 'host': '小云 · 自动',
     'msgs': [{'role': 'visitor', 'content': '部署要几台机器', 'at': '2026-11-20 14:19'},
              {'role': 'agent', 'content': '五十人以内一台就够，八核十六G，装常见 Linux 发行版。你们大概多少人用？', 'at': '2026-11-20 14:20'}]},
    {'id': 'c3', 'visitor': '陈经理', 'company': '绍兴柯桥纺织', 'channel': '网页', 'status': 'active',
     'tags': '', 'memo': '', 'host': '小云 · 自动',
     'msgs': [{'role': 'visitor', 'content': '先了解下，有问题再找你', 'at': '2026-11-20 13:58'}]},
    {'id': 'c4', 'visitor': '赵女士', 'company': '', 'channel': '网页', 'status': 'waiting',
     'tags': '', 'memo': '', 'host': '', 'msgs': []},
    {'id': 'c5', 'visitor': '周先生', 'company': '', 'channel': '网页', 'status': 'waiting',
     'tags': '', 'memo': '', 'host': '', 'msgs': []},
]


def seed_db():
    n = db.seed_sessions(SEED_SESSIONS)
    if n:
        print('演示会话已入库：%d 条' % n, flush=True)

def chat(q, history=None, rewrite_on=False):
    t0 = time.time()
    history = history or []
    intent = detect_intent(q)
    trace = {'intent': intent, 'rewrite': '', 'verdict': None, 'retried': '', 'by': 'tfidf'}
    messages = [{'role': 'system', 'content': SYS}]
    for h in history[-3:]:
        messages.append({'role': 'user', 'content': h.get('q', '')})
        messages.append({'role': 'assistant', 'content': h.get('a', '')})

    if intent != 'rag':
        answer = ''
        if api_ready('chat'):
            try:
                answer = ask_agent(messages + [{'role': 'user', 'content': q}],
                                   {'temperature': 0.6, 'max_tokens': 600})
            except Exception:
                pass
        answer = answer or REPLY[intent]
        return {'answer': answer, 'hits': [], 'trace': trace,
                'model': '规则' if not api_ready('chat') else CONFIG['chat'].get('model'),
                'timing': round(time.time() - t0, 1)}

    rw = rewrite_query(q, history) if (rewrite_on and api_ready('chat')) else ''
    ask_q = rw or q
    trace['rewrite'] = rw

    ranked = rank(q)
    if rw:
        r2 = rank(rw)
        if (r2[0]['sim'] if r2 else 0) - (ranked[0]['sim'] if ranked else 0) > 0.03:
            ranked = r2
        else:
            rw = ''
            trace['rewrite'] = ''
            trace['retried'] = '改写没提高命中，按原句检索'
    trace['by'] = 'vector' if (ranked and ranked[0].get('byVec')) else 'tfidf'

    pool = [x for x in ranked[:5] if x['sim'] > 0.01]
    top = pool[:3]
    verdict = {'ok': False, 'use': [], 'missing': '', 'skip': True, 'sentiment': '平静'}
    if pool and api_ready('chat'):
        verdict = grade_evidence(ask_q, pool)
        if verdict['use']:
            top = [pool[i] for i in verdict['use'][:3]]
        if not verdict['ok'] and verdict['missing']:
            retried = (ask_q + ' ' + verdict['missing']).strip()
            p2 = [x for x in rank(retried)[:5] if x['sim'] > 0.01]
            v2 = grade_evidence(ask_q, p2)
            if v2['use']:
                pool, top, verdict = p2, [p2[i] for i in v2['use'][:3]], v2
                trace['retried'] = '按「%s」重查了一次' % retried
            else:
                trace['retried'] = '补关键词重查一次，还是没有'
    if not verdict['use'] and len(pool) > len(top):
        top = pool[:5]
    trace['verdict'] = verdict

    ctx = '\n\n'.join('[%d] %s · %s\n%s' % (
        i + 1, x['chunk']['doc']['title'], x['chunk'].get('section', ''), x['chunk']['text'])
        for i, x in enumerate(top))
    if not top:
        ev = '这轮没有检索到任何资料。'
    elif verdict['use'] and verdict['ok']:
        ev = '检索到下面几段，可以用：\n\n' + ctx
    elif verdict['use']:
        ev = '只找到部分相关内容：\n\n' + ctx + '\n\n（不一定答全，缺的部分别猜。）'
    else:
        ev = '检索排在前面的几段如下，不一定对路，你自己判断用不用：\n\n' + ctx + '\n\n（能用就用，用不上就说查不到，别硬套。）'
    turn = ('【这一轮检索到的资料】\n' + ev + '\n\n【用户这句话】\n' + q
            + ('\n【结合前面的对话，这句话问的是】\n' + ask_q if ask_q != q else ''))
    full_messages = [{'role': 'system', 'content': SYS}] + messages[1:] + [{'role': 'user', 'content': turn}]

    notice = ''
    if api_ready('chat'):
        try:
            answer = ask_agent(full_messages, {'temperature': 0.35, 'max_tokens': 900})
            model = CONFIG['chat'].get('model')
        except Exception as e:
            answer = '这条没发出去，请稍后再说一次。（%s）' % str(e)[:60]
            model = CONFIG['chat'].get('model')
    else:
        answer = local_answer(top, q)
        model = '本地抽取'
        notice = '还没接对话模型，现在是把查到的原文直接拼给你看。要接的话，配置写在项目说明里。'

    return {'answer': answer, 'hits': [hit_view(x) for x in top], 'trace': trace,
            'prompt': turn, 'model': model, 'notice': notice,
            'timing': round(time.time() - t0, 1)}


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def _send(self, status, obj, ctype='application/json; charset=utf-8'):
        data = json.dumps(obj, ensure_ascii=False).encode('utf-8') if not isinstance(obj, bytes) else obj
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self._send(200, b'')

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode('utf-8'))
        except Exception:
            return {}

    def do_GET(self):
        from urllib.parse import unquote
        path = unquote(self.path.split('?')[0])
        if path == '/api/health':
            return self._send(200, {'ok': True, 'docs': len(DOCS), 'chunks': len(CHUNKS),
                                    'vector': vector_status(), 'chat': api_ready('chat')})
        if path == '/api/config':
            return self._send(200, public_config())
        if path == '/api/docs':
            return self._send(200, {'docs': [
                {'id': d['id'], 'title': d['title'], 'chars': len(d['text']),
                 'chunks': sum(1 for c in CHUNKS if c['doc']['id'] == d['id'])}
                for d in DOCS]})
        if path == '/api/doc':
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            d = doc_detail((qs.get('id') or [''])[0])
            return self._send(200, d) if d else self._send(404, {'error': '文档不存在'})
        if path == '/api/sessions':
            return self._send(200, db.list_sessions())
        if path == '/api/session':
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            d = db.get_session((qs.get('id') or [''])[0])
            return self._send(200, d) if d else self._send(404, {'error': '会话不存在'})
        if path == '/api/qa':
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            try:
                n = int((qs.get('limit') or ['20'])[0])
            except ValueError:
                n = 20
            return self._send(200, {'logs': db.recent_qa(min(max(n, 1), 100))})
        if path == '/api/stats':
            return self._send(200, db.stats())
        if path == '/api/db':
            info = db.info()
            try:
                info['counts'] = db.stats()
            except Exception:
                info['counts'] = {}
            return self._send(200, info)
        # 静态页面与资源：页面 + assets/ 下的样式、脚本、图片
        PAGES = {'/': '客户页.html', '/客户页.html': '客户页.html', '/用户端.html': '用户端.html',
                 '/坐席页.html': '坐席页.html', '/运维页.html': '运维页.html'}
        if path in PAGES:
            f = ROOT / PAGES[path]
            if f.exists():
                return self._send(200, f.read_bytes(), 'text/html; charset=utf-8')
            return self._send(404, {'error': 'not found'})
        if path.startswith('/assets/'):
            f = ROOT / path.lstrip('/')
            if f.is_file() and ROOT in f.resolve().parents:
                ext = f.suffix.lower()
                ctype = {
                    '.css': 'text/css; charset=utf-8',
                    '.js': 'application/javascript; charset=utf-8',
                    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                    '.gif': 'image/gif', '.svg': 'image/svg+xml', '.webp': 'image/webp',
                    '.ico': 'image/x-icon',
                }.get(ext, 'application/octet-stream')
                return self._send(200, f.read_bytes(), ctype)
        return self._send(404, {'error': 'not found'})

    def do_POST(self):
        path = self.path.split('?')[0]
        body = self._body()
        if path == '/api/config':
            save_config(body)
            return self._send(200, public_config())
        if path == '/api/search':
            q = str(body.get('q') or '').strip()
            if not q:
                return self._send(400, {'error': 'q 不能为空'})
            ranked = rank(q)
            return self._send(200, {'q': q, 'by': 'vector' if VEC['ready'] else 'tfidf',
                                    'hits': [hit_view(x) for x in ranked[:8]]})
        if path == '/api/vectors/build':
            return self._send(200, build_vectors())
        if path == '/api/upload':
            name = str(body.get('name') or '')
            raw64 = str(body.get('data') or '')
            try:
                raw = base64.b64decode(raw64.split(',', 1)[-1])
            except Exception:
                return self._send(400, {'error': '文件读取失败'})
            if not raw:
                return self._send(400, {'error': '文件是空的'})
            if len(raw) > 8 * 1024 * 1024:
                return self._send(400, {'error': '文件超过 8MB'})
            text = docparse.extract(name, raw)
            if len(text.strip()) < 10:
                return self._send(400, {'error': '这个文件里没读出文字，可能是扫描件或加密文档'})
            title = name.rsplit('.', 1)[0] if '.' in name else name
            d = save_doc(title, text)
            return self._send(200, {'ok': True, 'title': d['title'], 'chars': len(text)})
        if path == '/api/doc':
            d = save_doc(str(body.get('title') or ''), str(body.get('text') or ''))
            return self._send(200, d)
        if path == '/api/doc/delete':
            ok = delete_doc(str(body.get('id') or ''))
            return self._send(200, {'ok': ok})
        if path == '/api/docs/reset':
            return self._send(200, {'ok': True, 'docs': reset_docs()})
        if path == '/api/session/take':
            sid = str(body.get('id') or '')
            if not sid:
                return self._send(400, {'error': '缺 id'})
            db.take_session(sid)
            return self._send(200, db.get_session(sid) or {})
        if path == '/api/session/end':
            sid = str(body.get('id') or '')
            if not sid:
                return self._send(400, {'error': '缺 id'})
            db.end_session(sid)
            return self._send(200, {'ok': True})
        if path == '/api/session/message':
            sid = str(body.get('id') or '')
            text = str(body.get('content') or '').strip()
            role = str(body.get('role') or 'agent')
            if not sid or not text:
                return self._send(400, {'error': '缺 id 或内容'})
            db.add_message(sid, role, text)
            return self._send(200, {'ok': True})
        if path == '/api/chat':
            q = str(body.get('q') or '').strip()
            if not q:
                return self._send(400, {'error': 'q 不能为空'})
            try:
                res = chat(q, body.get('history') or [], bool(body.get('rewrite')))
                try:
                    db.log_qa(q, (res.get('answer') or '')[:2000], len(res.get('hits') or []),
                              res.get('model') or '', int(float(res.get('timing') or 0) * 1000))
                except Exception as e:
                    print('问答记录写入失败：%s' % e, flush=True)
                return self._send(200, res)
            except Exception as e:
                return self._send(500, {'error': str(e)[:200]})
        return self._send(404, {'error': 'not found'})

    def log_message(self, fmt, *args):
        if '/api/' in self.path:
            print('  %s %s' % (self.command, self.path.split('?')[0]), flush=True)


def main():
    load_config()
    db.configure(CONFIG.get('db'))
    try:
        db.init()
    except Exception as e:
        print('数据库连不上（%s），先按本地文件模式记录。原因：%s' % (db.engine(), e), flush=True)
    seed_db()
    load_docs()
    load_vectors()
    port = int(CONFIG.get('port') or 8900)
    print('RAG 服务已启动：http://127.0.0.1:%d/' % port, flush=True)
    print('知识库 %d 份 / %d 块；对话模型 %s；向量 %s' % (
        len(DOCS), len(CHUNKS), CONFIG['chat'].get('model') or '未配置',
        VEC['model'] or '未建'), flush=True)
    if not api_ready('chat'):
        print('未在 config.json 填对话模型 Key，当前走本地摘录降级。', flush=True)
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()


if __name__ == '__main__':
    main()
