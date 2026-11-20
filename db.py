# -*- coding: utf-8 -*-
"""数据层：默认 SQLite，可切 MySQL，两句配置就能换。

换库方式：config.json 里 db.type 改成 mysql，填好 host/port/user/password/database，
启动 MySQL 服务后重启本项目即可。表结构、字段、索引两套一致，业务代码不用改。

四张表：
documents  资料元数据
sessions   会话
messages   消息
qa_logs    问答记录
"""
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data.db"
_lock = threading.Lock()

STATE = {'type': 'sqlite', 'mysql': {}, 'error': ''}

DDL_SQLITE = [
    """CREATE TABLE IF NOT EXISTS documents (
        id TEXT PRIMARY KEY, title TEXT NOT NULL,
        chars INTEGER DEFAULT 0, chunks INTEGER DEFAULT 0, updated_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY, visitor TEXT, company TEXT, channel TEXT,
        status TEXT NOT NULL DEFAULT 'active', tags TEXT, memo TEXT, host TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status, updated_at)",
    """CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
        role TEXT NOT NULL, content TEXT, created_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id)",
    """CREATE TABLE IF NOT EXISTS qa_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT NOT NULL, answer TEXT,
        hits INTEGER DEFAULT 0, model TEXT, cost_ms INTEGER DEFAULT 0, created_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_qa_created ON qa_logs(created_at)",
]

DDL_MYSQL = [
    """CREATE TABLE IF NOT EXISTS documents (
        id VARCHAR(120) PRIMARY KEY, title VARCHAR(255) NOT NULL,
        chars INT DEFAULT 0, chunks INT DEFAULT 0, updated_at VARCHAR(32)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS sessions (
        id VARCHAR(64) PRIMARY KEY, visitor VARCHAR(120), company VARCHAR(180), channel VARCHAR(40),
        status VARCHAR(20) NOT NULL DEFAULT 'active', tags VARCHAR(255), memo TEXT, host VARCHAR(80),
        created_at VARCHAR(32) NOT NULL, updated_at VARCHAR(32) NOT NULL,
        KEY idx_sessions_status (status, updated_at)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS messages (
        id INT AUTO_INCREMENT PRIMARY KEY, session_id VARCHAR(64) NOT NULL,
        role VARCHAR(20) NOT NULL, content TEXT, created_at VARCHAR(32) NOT NULL,
        KEY idx_messages_session (session_id, id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS qa_logs (
        id INT AUTO_INCREMENT PRIMARY KEY, question TEXT NOT NULL, answer MEDIUMTEXT,
        hits INT DEFAULT 0, model VARCHAR(80), cost_ms INT DEFAULT 0, created_at VARCHAR(32) NOT NULL,
        KEY idx_qa_created (created_at)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]


class Conn:
    """把 sqlite3 和 pymysql 的用法抹平：都支持 execute / executescript / with。"""

    def __init__(self, raw, kind):
        self.raw = raw
        self.kind = kind

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type:
                self.raw.rollback()
            else:
                self.raw.commit()
        except Exception:
            pass
        finally:
            try:
                self.raw.close()
            except Exception:
                pass

    def execute(self, sql, args=()):
        cur = self.raw.cursor()
        cur.execute(sql, args)
        return cur

    def executescript(self, statements):
        cur = self.raw.cursor()
        for stmt in statements:
            cur.execute(stmt)


def configure(cfg):
    if not isinstance(cfg, dict):
        return
    STATE['type'] = str(cfg.get('type') or 'sqlite').lower()
    STATE['mysql'] = cfg.get('mysql') or {}


def engine():
    return STATE['type']


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _connect():
    if STATE['type'] == 'mysql':
        import pymysql
        m = STATE['mysql']
        raw = pymysql.connect(
            host=m.get('host') or '127.0.0.1',
            port=int(m.get('port') or 3306),
            user=m.get('user') or 'root',
            password=m.get('password') or '',
            database=m.get('database') or 'yunce',
            charset='utf8mb4',
            autocommit=False,
            cursorclass=pymysql.cursors.DictCursor,
        )
        return Conn(raw, 'mysql')
    raw = sqlite3.connect(DB_PATH, timeout=10)
    raw.row_factory = sqlite3.Row
    return Conn(raw, 'sqlite')


def conn():
    return _connect()


def init():
    statements = DDL_MYSQL if STATE['type'] == 'mysql' else DDL_SQLITE
    with _lock:
        c = _connect()
        try:
            c.executescript(statements)
            c.raw.commit()
            STATE['error'] = ''
        finally:
            c.raw.close()
    return {'engine': STATE['type'], 'ok': True}


def ping():
    try:
        with conn() as c:
            c.execute("SELECT 1")
        STATE['error'] = ''
        return True
    except Exception as e:
        STATE['error'] = str(e)[:160]
        return False


def info():
    ok = ping()
    return {
        'engine': STATE['type'],
        'ok': ok,
        'error': '' if ok else STATE['error'],
        'where': (STATE['mysql'].get('host', '') + '/' + STATE['mysql'].get('database', ''))
        if STATE['type'] == 'mysql' else str(DB_PATH),
    }


# ---------- 资料 ----------
def sync_documents(docs, chunk_ids):
    upsert = ("REPLACE INTO documents(id,title,chars,chunks,updated_at) VALUES(?,?,?,?,?)"
              if STATE['type'] == 'mysql'
              else "INSERT OR REPLACE INTO documents(id,title,chars,chunks,updated_at) VALUES(?,?,?,?,?)")
    with _lock, conn() as c:
        for d in docs:
            cnt = sum(1 for x in chunk_ids if x == d['id'])
            c.execute(upsert, (d['id'], d['title'], len(d['text']), cnt, now()))


def list_documents():
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id,title,chars,chunks,updated_at FROM documents ORDER BY id").fetchall()]


# ---------- 会话 ----------
def seed_sessions(rows):
    with _lock, conn() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()['n']
        if n:
            return 0
        for r in rows:
            c.execute(
                "INSERT INTO sessions(id,visitor,company,channel,status,tags,memo,host,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (r['id'], r['visitor'], r['company'], r['channel'], r['status'],
                 r.get('tags', ''), r.get('memo', ''), r.get('host', ''), now(), now()))
            for m in r.get('msgs', []):
                c.execute("INSERT INTO messages(session_id,role,content,created_at) VALUES(?,?,?,?)",
                          (r['id'], m['role'], m['content'], m.get('at') or now()))
        return len(rows)


def list_sessions():
    sql = """
    SELECT s.*, (
        SELECT content FROM messages m WHERE m.session_id = s.id ORDER BY m.id DESC LIMIT 1
    ) AS last_message, (
        SELECT created_at FROM messages m WHERE m.session_id = s.id ORDER BY m.id DESC LIMIT 1
    ) AS last_at
    FROM sessions s
    WHERE s.status IN ('active','waiting')
    ORDER BY s.updated_at DESC
    """
    with conn() as c:
        rows = [dict(r) for r in c.execute(sql).fetchall()]
    return {'active': [r for r in rows if r['status'] == 'active'],
            'waiting': [r for r in rows if r['status'] == 'waiting']}


def get_session(sid):
    with conn() as c:
        s = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if not s:
            return None
        msgs = [dict(r) for r in c.execute(
            "SELECT role,content,created_at FROM messages WHERE session_id=? ORDER BY id", (sid,)).fetchall()]
    d = dict(s)
    d['messages'] = msgs
    return d


def touch_session(sid, **fields):
    if not fields:
        return
    sets = ', '.join('%s=?' % k for k in fields)
    vals = list(fields.values()) + [now(), sid]
    with _lock, conn() as c:
        c.execute("UPDATE sessions SET %s, updated_at=? WHERE id=?" % sets, vals)


def take_session(sid):
    touch_session(sid, status='active')
    add_message(sid, 'system', '会话已接入')


def end_session(sid):
    touch_session(sid, status='closed')
    add_message(sid, 'system', '会话已结束')


def add_message(sid, role, content):
    with _lock, conn() as c:
        c.execute("INSERT INTO messages(session_id,role,content,created_at) VALUES(?,?,?,?)",
                  (sid, role, content, now()))
        c.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now(), sid))


# ---------- 问答记录 ----------
def log_qa(question, answer, hits, model, cost_ms):
    with _lock, conn() as c:
        c.execute("INSERT INTO qa_logs(question,answer,hits,model,cost_ms,created_at) VALUES(?,?,?,?,?,?)",
                  (question, answer, hits, model, cost_ms, now()))


def recent_qa(limit=20):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM qa_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def stats():
    with conn() as c:
        return {
            'sessions': c.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()['n'],
            'active': c.execute("SELECT COUNT(*) AS n FROM sessions WHERE status='active'").fetchone()['n'],
            'messages': c.execute("SELECT COUNT(*) AS n FROM messages").fetchone()['n'],
            'qa': c.execute("SELECT COUNT(*) AS n FROM qa_logs").fetchone()['n'],
            'docs': c.execute("SELECT COUNT(*) AS n FROM documents").fetchone()['n'],
        }
