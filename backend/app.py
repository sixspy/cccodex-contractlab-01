
import hashlib, secrets, sqlite3, threading
from datetime import date, datetime
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

# 切版是进程内互斥写路径：同主题抢点在此串行排队，数据库唯一索引再兜底
SWITCH_LOCK = threading.Lock()
BRANCH_A = "甲"  # 旧正式停用 + 新正式启用同一事务，提交后按新必填重裁
BRANCH_B = "乙"  # 先落待晋升草稿，确认无双正式后原子切正式指针
BRANCHES = (BRANCH_A, BRANCH_B)

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))

class LoginIn(BaseModel):
    username: str
    password: str

def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "入站契约"
SKELETON = "Schema 契约版本约束入站校验，破坏性变更进缓冲或拒收，并与落库结果对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS contracts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT UNIQUE NOT NULL,
          subject TEXT NOT NULL,
          mode TEXT NOT NULL,
          track TEXT NOT NULL,
          status TEXT NOT NULL,
          required_fields TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ingress_batches(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          subject TEXT NOT NULL,
          payload_fields TEXT NOT NULL,
          contract_version TEXT,
          result TEXT NOT NULL,
          detail TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS buffers(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          batch_id INTEGER NOT NULL,
          reason TEXT NOT NULL
        );
        -- 正式指针：切版写这里，入站裁决也只读这里，二者同源
        CREATE TABLE IF NOT EXISTS active_pointers(
          subject TEXT PRIMARY KEY,
          version TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        -- 切版策略簿：同主题当天只允许一条生效切版，分支锁定不许再换
        CREATE TABLE IF NOT EXISTS switch_ledger(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          subject TEXT NOT NULL,
          switch_date TEXT NOT NULL,
          branch TEXT NOT NULL,
          from_version TEXT,
          to_version TEXT,
          operator TEXT NOT NULL,
          status TEXT NOT NULL,
          detail TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        -- 切版重裁复核队列：原判与新判逐批对读
        CREATE TABLE IF NOT EXISTS reviews(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          batch_id INTEGER NOT NULL,
          subject TEXT NOT NULL,
          old_result TEXT NOT NULL,
          new_result TEXT NOT NULL,
          missing TEXT NOT NULL,
          contract_version TEXT NOT NULL,
          reason TEXT NOT NULL,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        -- 禁止双正式并存：同一主题至多一条启用中的正式契约
        CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_formal
          ON contracts(subject) WHERE track='正式' AND status='启用';
        -- 同主题当天至多一条生效切版，后来者只能记争用/偏了
        CREATE UNIQUE INDEX IF NOT EXISTS idx_one_effective_switch
          ON switch_ledger(subject, switch_date) WHERE status='生效';
        """)
        if conn.execute("SELECT COUNT(*) c FROM contracts").fetchone()["c"] == 0:
            conn.execute("INSERT INTO contracts(version,subject,mode,track,status,required_fields) VALUES (?,?,?,?,?,?)",
                         ("v3","订单入站","严格","正式","启用","order_id,amount,currency"))
            conn.execute("INSERT INTO contracts(version,subject,mode,track,status,required_fields) VALUES (?,?,?,?,?,?)",
                         ("v4-shadow","订单入站","兼容","影子","试跑","order_id,amount,currency,channel"))
            conn.execute("INSERT INTO contracts(version,subject,mode,track,status,required_fields) VALUES (?,?,?,?,?,?)",
                         ("v2","库存入站","严格","正式","启用","sku,qty"))
        # 待切版的新正式候选（停用态）：甲支的目标版本
        conn.execute("""INSERT OR IGNORE INTO contracts(version,subject,mode,track,status,required_fields)
                        VALUES (?,?,?,?,?,?)""",
                     ("v4", "订单入站", "严格", "正式", "停用", "order_id,amount,currency,channel"))
        # 旧库回填：若历史上出现过双正式，保留最新一条，其余停用
        dups = conn.execute("""SELECT subject, MAX(id) keep_id FROM contracts
                               WHERE track='正式' AND status='启用'
                               GROUP BY subject HAVING COUNT(*) > 1""").fetchall()
        for d in dups:
            conn.execute("""UPDATE contracts SET status='停用'
                            WHERE subject=? AND track='正式' AND status='启用' AND id<>?""",
                         (d["subject"], d["keep_id"]))
        # 指针回填：与当前启用正式对齐
        rows = conn.execute("""SELECT subject, version FROM contracts
                               WHERE track='正式' AND status='启用'""").fetchall()
        for r in rows:
            conn.execute("""INSERT INTO active_pointers(subject,version,updated_at) VALUES(?,?,?)
                            ON CONFLICT(subject) DO UPDATE SET version=excluded.version""",
                         (r["subject"], r["version"], datetime.now().isoformat(timespec="seconds")))

@app.on_event("startup")
def _s():
    init_db()

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM contracts").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "contracts": n, "db": str(DB_PATH)}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row: raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok]=row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

def split_fields(raw: str) -> set[str]:
    return {x.strip() for x in (raw or "").split(",") if x.strip()}

def active_contract(conn, subject: str):
    """入站裁决唯一入口：沿正式指针取契约，切版成功后不可能读到旧必填。"""
    return conn.execute("""SELECT c.* FROM active_pointers p
                           JOIN contracts c ON c.subject=p.subject AND c.version=p.version
                           WHERE p.subject=?""", (subject,)).fetchone()

@app.get("/api/contracts")
def contracts(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("""SELECT c.*, p.version AS active_version,
                            CASE WHEN p.version=c.version THEN 1 ELSE 0 END AS is_active
                            FROM contracts c LEFT JOIN active_pointers p ON p.subject=c.subject
                            ORDER BY c.id""").fetchall()
        return {"items": [dict(r) for r in rows]}

class IngressIn(BaseModel):
    subject: str
    payload_fields: str  # comma fields present

@app.post("/api/ingress/validate")
def validate(body: IngressIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    fields = split_fields(body.payload_fields)
    with connect() as c:
        ctr = active_contract(c, body.subject)
        if not ctr:
            raise HTTPException(404, "无启用正式契约")
        need = split_fields(ctr["required_fields"])
        missing = sorted(need - fields)
        now = datetime.now().isoformat(timespec="seconds")
        if missing:
            if ctr["mode"] == "兼容":
                cur = c.execute("INSERT INTO ingress_batches(subject,payload_fields,contract_version,result,detail,created_at) VALUES (?,?,?,?,?,?)",
                                (body.subject, body.payload_fields, ctr["version"], "缓冲", "缺字段:"+ ",".join(missing), now))
                c.execute("INSERT INTO buffers(batch_id,reason) VALUES (?,?)", (cur.lastrowid, "缺字段进双写缓冲"))
                return {"result":"缓冲", "version": ctr["version"], "missing": missing, "batch_id": cur.lastrowid}
            cur = c.execute("INSERT INTO ingress_batches(subject,payload_fields,contract_version,result,detail,created_at) VALUES (?,?,?,?,?,?)",
                            (body.subject, body.payload_fields, ctr["version"], "拒收", "缺字段:"+ ",".join(missing), now))
            return {"result":"拒收", "version": ctr["version"], "missing": missing, "batch_id": cur.lastrowid}
        cur = c.execute("INSERT INTO ingress_batches(subject,payload_fields,contract_version,result,detail,created_at) VALUES (?,?,?,?,?,?)",
                        (body.subject, body.payload_fields, ctr["version"], "通过", "字段齐全", now))
        return {"result":"通过", "version": ctr["version"], "batch_id": cur.lastrowid}

@app.get("/api/ingress")
def ingress(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM ingress_batches ORDER BY id DESC").fetchall()]}

@app.get("/api/ingress/{batch_id}")
def ingress_detail(batch_id: int, authorization: str | None = Header(default=None)):
    """详情卡：批次原判、裁决时契约、当前正式指针、历次复核一卡对读。"""
    require_user(authorization)
    with connect() as c:
        batch = c.execute("SELECT * FROM ingress_batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise HTTPException(404, "批次不存在")
        ctr = c.execute("SELECT * FROM contracts WHERE version=?", (batch["contract_version"],)).fetchone()
        pointer = c.execute("SELECT * FROM active_pointers WHERE subject=?", (batch["subject"],)).fetchone()
        revs = c.execute("SELECT * FROM reviews WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
        return {
            "batch": dict(batch),
            "adjudicated_contract": dict(ctr) if ctr else None,
            "active_pointer": dict(pointer) if pointer else None,
            "reviews": [dict(r) for r in revs],
        }

@app.get("/api/buffers")
def buffers(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("""SELECT b.*, i.subject, i.payload_fields FROM buffers b JOIN ingress_batches i ON i.id=b.batch_id ORDER BY b.id DESC""").fetchall()
        return {"items":[dict(r) for r in rows]}

class SwitchIn(BaseModel):
    subject: str
    branch: str                      # 甲 / 乙，互斥写路径只许选一支
    target_version: str | None = None   # 甲支：已存在的正式候选版本
    version: str | None = None          # 乙支：待晋升草稿版本
    required_fields: str | None = None  # 乙支：草稿必填字段
    mode: str | None = "严格"

def _ledger_insert(conn, subject, branch, operator, status, detail, from_version=None, to_version=None):
    conn.execute("""INSERT INTO switch_ledger(subject,switch_date,branch,from_version,to_version,operator,status,detail,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                 (subject, date.today().isoformat(), branch, from_version, to_version,
                  operator, status, detail, datetime.now().isoformat(timespec="seconds")))

def _reject_locked(conn, detail):
    conn.commit()
    conn.close()
    http_exc = HTTPException(409, detail)
    http_exc.handled = True
    raise http_exc

def readjudicate(conn, subject, ctr):
    """甲支提交后随即按新必填重裁：只重判旧必填下放行的批次（通过/缓冲），
    原批次不可变，结论进复核队列排队。"""
    need = split_fields(ctr["required_fields"])
    rows = conn.execute("""SELECT * FROM ingress_batches
                           WHERE subject=? AND result IN ('通过','缓冲') ORDER BY id""", (subject,)).fetchall()
    now = datetime.now().isoformat(timespec="seconds")
    for b in rows:
        missing = sorted(need - split_fields(b["payload_fields"]))
        if not missing:
            new_result = "通过"
        elif ctr["mode"] == "兼容":
            new_result = "缓冲"
        else:
            new_result = "拒收"
        conn.execute("""INSERT INTO reviews(batch_id,subject,old_result,new_result,missing,contract_version,reason,status,created_at)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                     (b["id"], subject, b["result"], new_result, ",".join(missing),
                      ctr["version"], "切版重裁", "待复核", now))

@app.post("/api/contracts/switch")
def switch_contract(body: SwitchIn, authorization: str | None = Header(default=None)):
    operator = require_user(authorization)
    if body.branch not in BRANCHES:
        raise HTTPException(400, "切版分支只允许 甲 或 乙，且一支到底不得混用")
    today = date.today().isoformat()
    with SWITCH_LOCK:  # 同主题两名值班抢点：先到进事务，后到排队后只落争用
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            eff = conn.execute("""SELECT * FROM switch_ledger
                                  WHERE subject=? AND switch_date=? AND status='生效'""",
                               (body.subject, today)).fetchone()
            if eff:
                if eff["branch"] != body.branch:
                    _ledger_insert(conn, body.subject, body.branch, operator, "偏了",
                                   f"切版偏了：策略簿当天已锁定{eff['branch']}支，不得混用其它支",
                                   eff["to_version"], None)
                    _reject_locked(conn, "切版偏了：当天策略簿分支不可混用")
                _ledger_insert(conn, body.subject, body.branch, operator, "争用",
                               f"切版争用：当天已有{eff['branch']}支生效切版，后到请求排队不生效",
                               eff["to_version"], None)
                _reject_locked(conn, "切版争用：同主题当天已有人切版，后到不生效")

            now = datetime.now().isoformat(timespec="seconds")
            if body.branch == BRANCH_A:
                if not body.target_version:
                    raise HTTPException(400, "甲支切版必须指定已存在的 target_version")
                cur = active_contract(conn, body.subject)
                if not cur:
                    raise HTTPException(404, "主题无启用正式契约，甲支无可停用的旧正式")
                tgt = conn.execute("""SELECT * FROM contracts
                                      WHERE subject=? AND version=? AND track='正式'""",
                                   (body.subject, body.target_version)).fetchone()
                if not tgt:
                    raise HTTPException(404, "目标正式契约不存在")
                if tgt["version"] == cur["version"]:
                    raise HTTPException(400, "目标版本已是当前正式，无需切版")
                if tgt["status"] == "启用":
                    raise HTTPException(409, "目标已启用，继续切版会造成双正式并存")
                # 甲支：旧正式停用与新正式启用同一事务提交
                conn.execute("UPDATE contracts SET status='停用' WHERE id=?", (cur["id"],))
                conn.execute("UPDATE contracts SET status='启用' WHERE id=?", (tgt["id"],))
                conn.execute("""INSERT INTO active_pointers(subject,version,updated_at) VALUES(?,?,?)
                                ON CONFLICT(subject) DO UPDATE SET version=excluded.version,
                                updated_at=excluded.updated_at""",
                             (body.subject, tgt["version"], now))
                _ledger_insert(conn, body.subject, BRANCH_A, operator, "生效",
                               "同事务停用旧正式并启用新正式，提交后按新必填重裁",
                               cur["version"], tgt["version"])
                conn.commit()
                # 事务提交后随即重裁（重裁本身独立事务，绝不回灌切版事务）
                with connect() as rc:
                    readjudicate(rc, body.subject, tgt)
                return {"result": "切版生效", "branch": BRANCH_A,
                        "from_version": cur["version"], "to_version": tgt["version"]}

            # 乙支：先落待晋升草稿
            if not body.version or not body.required_fields:
                raise HTTPException(400, "乙支切版必须提供草稿 version 与 required_fields")
            exists = conn.execute("SELECT 1 FROM contracts WHERE version=?", (body.version,)).fetchone()
            if exists:
                raise HTTPException(409, "草稿版本号已存在")
            cur = active_contract(conn, body.subject)
            conn.execute("""INSERT INTO contracts(version,subject,mode,track,status,required_fields)
                            VALUES (?,?,?,?,?,?)""",
                         (body.version, body.subject, body.mode or "严格", "正式",
                          "待晋升", body.required_fields))
            # 确认无双正式：当前启用正式至多一条
            n = conn.execute("""SELECT COUNT(*) c FROM contracts
                                WHERE subject=? AND track='正式' AND status='启用'""",
                             (body.subject,)).fetchone()["c"]
            if n > 1:
                conn.rollback()
                raise HTTPException(409, "存在双正式并存，乙支拒绝晋升")
            old_version = cur["version"] if cur else None
            if cur:
                conn.execute("UPDATE contracts SET status='停用' WHERE id=?", (cur["id"],))
            conn.execute("UPDATE contracts SET status='启用' WHERE version=? AND track='正式'",
                         (body.version,))
            # 原子切正式指针：一条 upsert 定稿，裁决随指针换源
            conn.execute("""INSERT INTO active_pointers(subject,version,updated_at) VALUES(?,?,?)
                            ON CONFLICT(subject) DO UPDATE SET version=excluded.version,
                            updated_at=excluded.updated_at""",
                         (body.subject, body.version, now))
            _ledger_insert(conn, body.subject, BRANCH_B, operator, "生效",
                           "待晋升草稿确认无双正式后原子切换正式指针",
                           old_version, body.version)
            conn.commit()
            return {"result": "切版生效", "branch": BRANCH_B,
                    "from_version": old_version, "to_version": body.version}
        except HTTPException as e:
            if not getattr(e, "handled", False):
                conn.rollback()
            raise
        finally:
            try:
                conn.close()
            except sqlite3.ProgrammingError:
                pass

@app.get("/api/switch/ledger")
def switch_ledger(authorization: str | None = Header(default=None)):
    """策略附页数据源：切版策略簿全量流水。"""
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM switch_ledger ORDER BY id DESC").fetchall()
        return {"items": [dict(r) for r in rows]}

@app.get("/api/reviews")
def reviews(authorization: str | None = Header(default=None)):
    """复核队列：切版重裁结论，可与入库批次逐行对读。"""
    require_user(authorization)
    with connect() as c:
        rows = c.execute("""SELECT r.*, i.payload_fields, i.detail AS batch_detail, i.created_at AS batch_at
                            FROM reviews r JOIN ingress_batches i ON i.id=r.batch_id
                            ORDER BY r.id DESC""").fetchall()
        return {"items": [dict(r) for r in rows]}

@app.post("/api/reviews/{review_id}/close")
def close_review(review_id: int, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        cur = c.execute("UPDATE reviews SET status='已复核' WHERE id=? AND status='待复核'", (review_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "待复核记录不存在或已闭环")
        return {"result": "已复核", "id": review_id}

@app.get("/api/reconcile")
def reconcile(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        total = c.execute("SELECT COUNT(*) c FROM ingress_batches").fetchone()["c"]
        passed = c.execute("SELECT COUNT(*) c FROM ingress_batches WHERE result='通过'").fetchone()["c"]
        buffered = c.execute("SELECT COUNT(*) c FROM ingress_batches WHERE result='缓冲'").fetchone()["c"]
        rejected = c.execute("SELECT COUNT(*) c FROM ingress_batches WHERE result='拒收'").fetchone()["c"]
        buf_rows = c.execute("SELECT COUNT(*) c FROM buffers").fetchone()["c"]
        dual = c.execute("""SELECT COUNT(*) c FROM (SELECT subject FROM contracts
                            WHERE track='正式' AND status='启用' GROUP BY subject HAVING COUNT(*)>1)""").fetchone()["c"]
        open_reviews = c.execute("SELECT COUNT(*) c FROM reviews WHERE status='待复核'").fetchone()["c"]
    return {"ingress_total": total, "passed": passed, "buffered": buffered, "rejected": rejected,
            "buffer_rows": buf_rows, "aligned": buffered == buf_rows,
            "dual_formal_subjects": dual, "open_reviews": open_reviews}

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
