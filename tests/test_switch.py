
import threading

import pytest
from fastapi.testclient import TestClient

from app import app, init_db, BRANCH_A, BRANCH_B


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


@pytest.fixture()
def h():
    c = TestClient(app)
    tok = c.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})
    return c


def test_smoke_validate_paths(h):
    r = h.post("/api/ingress/validate", json={"subject": "订单入站", "payload_fields": "order_id,amount"})
    assert r.status_code == 200 and r.json()["result"] == "拒收"
    ok = h.post("/api/ingress/validate", json={"subject": "订单入站",
                                                "payload_fields": "order_id,amount,currency"})
    assert ok.json()["result"] == "通过"
    assert h.get("/api/reconcile").json()["passed"] >= 1


def _ingest(h, fields, expect=None):
    r = h.post("/api/ingress/validate", json={"subject": "订单入站", "payload_fields": fields})
    assert r.status_code == 200
    if expect:
        assert r.json()["result"] == expect
    return r.json()


# ---------- 甲支：同事务停用/启用 + 随即重裁 ----------

def test_branch_a_switch_transaction_then_readjudicate(h):
    # 旧必填 v3 下放行两批：一批通过、一批兼容缓冲（v3 严格，拒收批不进重裁）
    passed = _ingest(h, "order_id,amount,currency", "通过")
    buffered = _ingest(h, "order_id,amount,currency,channel", "通过")  # 新必填齐全
    short = _ingest(h, "order_id,amount", "拒收")                       # 已拒收，不重裁

    r = h.post("/api/contracts/switch", json={"subject": "订单入站",
                                              "branch": BRANCH_A, "target_version": "v4"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["from_version"] == "v3" and body["to_version"] == "v4"

    # 切版成功后入站裁决必须走新必填（v4 多了 channel）
    again = h.post("/api/ingress/validate",
                   json={"subject": "订单入站", "payload_fields": "order_id,amount,currency"})
    assert again.json()["result"] == "拒收" and again.json()["version"] == "v4"

    # 无双正式（按切版主题核对）
    contracts = {x["version"]: x for x in h.get("/api/contracts").json()["items"]}
    active = [v for v in contracts.values()
              if v["status"] == "启用" and v["track"] == "正式" and v["subject"] == "订单入站"]
    assert [x["version"] for x in active] == ["v4"]
    assert contracts["v3"]["status"] == "停用"
    assert contracts["v4"]["is_active"] == 1
    assert h.get("/api/reconcile").json()["dual_formal_subjects"] == 0

    # 重裁队列：旧放行批按新必填复核，原拒收批不动
    reviews = h.get("/api/reviews").json()["items"]
    by_batch = {x["batch_id"]: x for x in reviews}
    assert by_batch[passed["batch_id"]]["new_result"] == "拒收"
    assert "channel" in by_batch[passed["batch_id"]]["missing"]
    assert by_batch[buffered["batch_id"]]["new_result"] == "通过"
    assert short["batch_id"] not in by_batch

    # 详情卡：原判、裁决时契约、当前指针、重裁记录一卡对读
    d = h.get(f"/api/ingress/{passed['batch_id']}").json()
    assert d["batch"]["result"] == "通过"                      # 原判不可变
    assert d["adjudicated_contract"]["version"] == "v3"       # 裁决时是旧版
    assert d["active_pointer"]["version"] == "v4"             # 当前指针已换
    assert d["reviews"][0]["new_result"] == "拒收"

    # 复核闭环
    rid = by_batch[passed["batch_id"]]["id"]
    assert h.post(f"/api/reviews/{rid}/close").json()["result"] == "已复核"
    assert h.post(f"/api/reviews/{rid}/close").status_code == 404


def test_branch_a_requires_existing_target(h):
    r = h.post("/api/contracts/switch",
               json={"subject": "订单入站", "branch": BRANCH_A, "target_version": "v999"})
    assert r.status_code == 404
    # 拒绝后旧正式仍在，未产生生效流水
    assert h.get("/api/reconcile").json()["dual_formal_subjects"] == 0
    assert not [x for x in h.get("/api/switch/ledger").json()["items"] if x["status"] == "生效"]


# ---------- 乙支：待晋升草稿 + 确认无双正式 + 原子指针 ----------

def test_branch_b_draft_then_atomic_pointer(h):
    r = h.post("/api/contracts/switch", json={
        "subject": "订单入站", "branch": BRANCH_B, "version": "v5",
        "required_fields": "order_id,amount,currency,channel,buyer_id", "mode": "严格"})
    assert r.status_code == 200, r.text
    assert r.json()["to_version"] == "v5"

    contracts = {x["version"]: x for x in h.get("/api/contracts").json()["items"]}
    assert contracts["v5"]["status"] == "启用" and contracts["v5"]["track"] == "正式"
    assert contracts["v3"]["status"] == "停用"
    active = [v for v in contracts.values()
              if v["status"] == "启用" and v["track"] == "正式" and v["subject"] == "订单入站"]
    assert len(active) == 1 and active[0]["version"] == "v5"

    # 新必填立即生效
    rr = h.post("/api/ingress/validate",
                json={"subject": "订单入站", "payload_fields": "order_id,amount,currency,channel"})
    assert rr.json()["result"] == "拒收" and "buyer_id" in rr.json()["missing"]

    # 乙支不触发重裁（两支职责互斥，不混用）
    assert h.get("/api/reviews").json()["items"] == []


def test_branch_b_duplicate_version_rejected(h):
    r = h.post("/api/contracts/switch", json={
        "subject": "订单入站", "branch": BRANCH_B, "version": "v4",
        "required_fields": "order_id,amount", "mode": "严格"})
    assert r.status_code == 409
    contracts = h.get("/api/contracts").json()["items"]
    assert [x for x in contracts if x["version"] == "v4"][0]["status"] == "停用"


# ---------- 策略簿当天锁定：争用与偏了 ----------

def test_same_day_second_switch_contention_then_deviation(h):
    ok = h.post("/api/contracts/switch",
                json={"subject": "订单入站", "branch": BRANCH_A, "target_version": "v4"})
    assert ok.status_code == 200

    # 同支后到：争用，拒绝不生效
    contention = h.post("/api/contracts/switch",
                        json={"subject": "订单入站", "branch": BRANCH_A, "target_version": "v4"})
    assert contention.status_code == 409 and "争用" in contention.json()["detail"]

    # 异支后到：偏了，拒绝不生效
    deviation = h.post("/api/contracts/switch", json={
        "subject": "订单入站", "branch": BRANCH_B, "version": "v6",
        "required_fields": "order_id,amount", "mode": "严格"})
    assert deviation.status_code == 409 and "偏了" in deviation.json()["detail"]

    ledger = h.get("/api/switch/ledger").json()["items"]
    statuses = [x["status"] for x in ledger]
    assert statuses.count("生效") == 1
    assert "争用" in statuses and "偏了" in statuses
    # 被拒的 v6 草稿不得落库
    assert not [x for x in h.get("/api/contracts").json()["items"] if x["version"] == "v6"]
    # 指针仍是甲支切出的 v4
    active = [x for x in h.get("/api/contracts").json()["items"]
              if x["is_active"] == 1 and x["subject"] == "订单入站"]
    assert [x["version"] for x in active if x["track"] == "正式"] == ["v4"]


def test_other_subject_not_locked(h):
    h.post("/api/contracts/switch",
           json={"subject": "订单入站", "branch": BRANCH_A, "target_version": "v4"})
    # 另一主题当天仍可切（锁按主题隔离）
    r = h.post("/api/contracts/switch", json={
        "subject": "库存入站", "branch": BRANCH_B, "version": "v3-stock",
        "required_fields": "sku,qty,warehouse", "mode": "严格"})
    assert r.status_code == 200


# ---------- 并发抢点：排队，后到只记争用 ----------

def test_concurrent_switches_serialize(h, monkeypatch):
    # 用第二个值班账号模拟两名值班同时抢点
    client2 = TestClient(app)
    tok2 = client2.post("/api/login", json={"username": "viewer", "password": "view123"}).json()["token"]
    client2.headers.update({"Authorization": f"Bearer {tok2}"})

    results = []

    def fire(client, label):
        r = client.post("/api/contracts/switch",
                        json={"subject": "订单入站", "branch": BRANCH_A, "target_version": "v4"})
        results.append((label, r.status_code, r.json()))

    t1 = threading.Thread(target=fire, args=(h, "first"))
    t2 = threading.Thread(target=fire, args=(client2, "second"))
    t1.start(); t2.start(); t1.join(); t2.join()

    codes = sorted(x[1] for x in results)
    assert codes == [200, 409]
    loser = next(x for x in results if x[1] == 409)
    assert "争用" in loser[2]["detail"]

    ledger = h.get("/api/switch/ledger").json()["items"]
    assert [x["status"] for x in ledger].count("生效") == 1
    operators = {x["operator"] for x in ledger}
    assert operators == {"admin", "viewer"}   # 后到值班留痕但不默成
    assert h.get("/api/reconcile").json()["dual_formal_subjects"] == 0


# ---------- 数据库层兜底：禁止双正式并存 ----------

def test_db_unique_index_blocks_dual_formal(h):
    import sqlite3
    from app import DB_PATH
    conn = sqlite3.connect(DB_PATH)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE contracts SET status='启用' WHERE version='v4'")  # v3 仍启用
        conn.commit()
    conn.rollback(); conn.close()


# ---------- 切版页序甲可核：策略簿按时间留痕，入站批次可对读 ----------

def test_ledger_and_batches_cross_readable(h):
    b1 = _ingest(h, "order_id,amount,currency", "通过")
    h.post("/api/contracts/switch",
           json={"subject": "订单入站", "branch": BRANCH_A, "target_version": "v4"})
    b2 = _ingest(h, "order_id,amount,currency,channel", "通过")

    batches = {x["id"]: x for x in h.get("/api/ingress").json()["items"]}
    assert batches[b1["batch_id"]]["contract_version"] == "v3"   # 入库时裁决版本固化
    assert batches[b2["batch_id"]]["contract_version"] == "v4"

    ledger = h.get("/api/switch/ledger").json()["items"][0]
    assert ledger["from_version"] == "v3" and ledger["to_version"] == "v4"
    assert ledger["branch"] == "甲" and ledger["status"] == "生效"

    # 复核队列与批次 join 对读
    reviews = h.get("/api/reviews").json()["items"]
    joined = {x["batch_id"]: x for x in reviews}
    assert joined[b1["batch_id"]]["payload_fields"] == "order_id,amount,currency"
