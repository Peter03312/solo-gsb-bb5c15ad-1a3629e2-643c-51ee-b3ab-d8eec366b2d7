"""并发写入测试：同一货物的多笔并发交接必须串行化，保管链不得损坏。

本用例需要“文件型 SQLite（每线程独立连接）”的引擎，运行方式：

    DATABASE_URL="sqlite:////tmp/coldchain_conc.db" pytest tests/test_concurrency.py

（默认的内存 StaticPool 配置下本用例自动跳过。）
"""
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.database import Base, engine
from app.main import app

pytestmark = pytest.mark.skipif(
    engine.pool.__class__.__name__ == "StaticPool",
    reason="StaticPool（内存库）不支持多线程并发；"
    "请用文件型 sqlite 单独运行："
    "DATABASE_URL='sqlite:////tmp/x.db' pytest tests/test_concurrency.py",
)


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as c:
        yield c
    Base.metadata.drop_all(bind=engine)


def test_concurrent_handovers_preserve_chain(client):
    client.post(
        "/cargo",
        json={
            "cargo_id": "CC",
            "started_at": "2026-01-01T10:00:00+00:00",
            "temp_upper_limit": "5",
            "initial_holder": "A",
        },
    )

    def submit(i):
        # 所有线程都提交“同一时刻、A->Pi”的交接：货物行锁使其串行执行，
        # 只有第一笔成功；其余要么交出方失配（保管方已变更），
        # 要么触发同一时刻交接唯一约束
        return client.post(
            "/events",
            json={
                "event_id": f"hc{i}",
                "cargo_id": "CC",
                "occurred_at": "2026-01-01T11:00:00+00:00",
                "giver": "A",
                "receiver": f"P{i}",
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(submit, range(8)))

    assert codes.count(201) == 1
    assert all(c in (409, 422) for c in codes if c != 201)
    report = client.get("/cargo/CC/report").json()
    # 保管链只前进一次，当前保管方是唯一成功那笔的接收方
    assert report["current_holder"].startswith("P")


def test_concurrent_stance_submits_serialize_with_snapshot(client):
    """表态并发提交：同责任方相同内容只有第一笔 201，其余按幂等返回 200。"""
    client.post(
        "/cargo",
        json={
            "cargo_id": "CS",
            "started_at": "2026-01-01T10:00:00+00:00",
            "temp_upper_limit": "5",
            "initial_holder": "A",
        },
    )
    client.post("/events", json={
        "event_id": "m1", "cargo_id": "CS",
        "occurred_at": "2026-01-01T10:10:00+00:00", "temperature": "8"})
    client.post("/events", json={
        "event_id": "m2", "cargo_id": "CS",
        "occurred_at": "2026-01-01T10:20:00+00:00", "temperature": "4"})
    snap = client.get("/cargo/CS/responsibilities").json()["intervals"][0]["snapshot_id"]

    def submit(i):
        return client.post(
            "/cargo/CS/responsibilities/m1/stances",
            json={
                "snapshot_id": snap,
                "responsible_party": "A",
                "stance": "confirm",
                # 所有线程同一编号同一内容：行锁串行后表现为 1 次写入 + 其余幂等
                "stance_id": "same-stance",
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(submit, range(8)))

    assert codes.count(201) == 1
    assert codes.count(200) == 7
    hist = client.get("/cargo/CS/responsibilities/m1/stances").json()
    assert len(hist["history"]) == 1


def _mk(client, cargo, holder="A", limit="5"):
    client.post(
        "/cargo",
        json={
            "cargo_id": cargo,
            "started_at": "2026-01-01T10:00:00+00:00",
            "temp_upper_limit": limit,
            "initial_holder": holder,
        },
    )


def test_concurrent_manifest_registrations_no_overlap_persisted(client):
    """并发登记同货物装卸时段：行锁串行后只允许互不相交的时段落库。

    所有线程登记同一窗口：仅第一笔成功，其余按幂等（200）返回；
    登记与之相交窗口的线程全部被拒（422/409）。
    """
    _mk(client, "CM")
    client.post("/trips", json={"trip_id": "T1"})
    client.post("/trips", json={"trip_id": "T2"})

    barrier = __import__("threading").Barrier(8)

    def register(i):
        barrier.wait()
        trip = "T1" if i % 2 == 0 else "T2"
        return client.post(
            f"/trips/{trip}/manifest",
            json={
                "items": [
                    {
                        "cargo_id": "CM",
                        "loaded_at": "2026-01-01T11:00:00+00:00",
                        "unloaded_at": "2026-01-01T12:00:00+00:00",
                    }
                ]
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(register, range(8)))

    # 第一笔落库的窗口归属其中一个车次；另一车次因时段重叠全部被拒
    assert codes.count(201) == 1
    assert all(c in (200, 409, 422) for c in codes if c != 201)
    t1 = client.get("/trips/T1/manifest").json()
    t2 = client.get("/trips/T2/manifest").json()
    assert len(t1) + len(t2) == 1
    assert (t1 or t2)[0]["cargo_id"] == "CM"


def test_concurrent_reading_and_stance_serialize(client):
    """车载读数写入与责任表态共用货物行锁：并发下结果一致、无死锁。"""
    _mk(client, "CR")
    client.post("/trips", json={"trip_id": "T1"})
    client.post(
        "/trips/T1/manifest",
        json={
            "items": [
                {
                    "cargo_id": "CR",
                    "loaded_at": "2026-01-01T10:00:00+00:00",
                    "unloaded_at": "2026-01-01T13:00:00+00:00",
                }
            ]
        },
    )
    client.post("/trips/T1/readings", json={
        "reading_id": "r1",
        "read_at": "2026-01-01T10:10:00+00:00",
        "temperature": "8"})
    snap = client.get("/cargo/CR/responsibilities").json()
    # 开放区间尚无快照；先补闭合读数再并发表态 / 读数
    client.post("/trips/T1/readings", json={
        "reading_id": "r2",
        "read_at": "2026-01-01T10:20:00+00:00",
        "temperature": "4"})
    snap = client.get("/cargo/CR/responsibilities").json()["intervals"][0]["snapshot_id"]

    def stance_ok(i):
        return client.post(
            "/cargo/CR/responsibilities/trip:T1:reading:r1/stances",
            json={
                "snapshot_id": snap,
                "responsible_party": "A",
                "stance": "confirm",
                "stance_id": f"s{i}",
            },
        ).status_code

    def reading_ok(i):
        # 区间外时刻的读数，不改变区间快照；与表态并发抢同一把货物行锁
        return client.post(
            "/trips/T1/readings",
            json={
                "reading_id": f"rl{i}",
                "read_at": f"2026-01-01T10:2{i}:00+00:00",
                "temperature": "4",
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        f1 = [pool.submit(stance_ok, i) for i in range(4)]
        f2 = [pool.submit(reading_ok, i + 3) for i in range(4)]
        codes = [f.result() for f in f1 + f2]

    # 不同 stance_id 的表态全部成功；读数全部成功（无死锁/无 5xx）
    assert all(c in (200, 201) for c in codes), codes
    iv = client.get("/cargo/CR/responsibilities").json()["intervals"][0]
    assert iv["summary"] == "all_confirmed"
    assert len(iv["latest_stances"]) == 1
