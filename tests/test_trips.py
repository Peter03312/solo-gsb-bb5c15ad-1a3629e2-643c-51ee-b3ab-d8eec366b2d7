"""车次 / 装卸时段 / 车载读数的 HTTP API 集成测试。

覆盖本轮规则：
* 车次登记、装卸时段左闭右开且起点严格早于终点；
* 同货物不得同时属于两个车次（时段重叠拒绝，首尾相接允许）；
* 非法装载时段 / 货物或车次不存在 -> 整批拒绝不留部分记录；
* 车载读数仅作用于读数时刻仍在车上的货物，各货物按各自上限判断越温；
* 同刻货物自有测温优先采用，车载原始读数仍可查；
* 交接仍只由货物自有事件改变保管方（车载读数不改变保管方）；
* 车载读数允许晚到；编号同内容重传幂等、异内容冲突；同刻重复读数冲突；
* 追责 / 责任确认按合并读数时间线重建；车载开启区间有稳定标识
  trip:<车次>:reading:<读数>，表态、快照、改口、失效、历史规则沿用。
"""
# --------------------------------------------------------------- 辅助


def mk_cargo(client, cargo="C1", limit="5", holder="A"):
    r = client.post(
        "/cargo",
        json={
            "cargo_id": cargo,
            "started_at": "2026-01-01T10:00:00+00:00",
            "temp_upper_limit": limit,
            "initial_holder": holder,
        },
    )
    assert r.status_code == 201, r.text


def mk_trip(client, trip="T1", vehicle=None):
    r = client.post("/trips", json={"trip_id": trip, "vehicle_no": vehicle})
    assert r.status_code == 201, r.text
    return r.json()


def manifest(client, trip, items):
    return client.post(f"/trips/{trip}/manifest", json={"items": items})


def W(cargo, lo, hi):
    return {
        "cargo_id": cargo,
        "loaded_at": f"2026-01-01T{lo}:00+00:00" if isinstance(lo, int) else lo,
        "unloaded_at": f"2026-01-01T{hi}:00+00:00" if isinstance(hi, int) else hi,
    }


def reading(client, trip, rid, at, temp):
    return client.post(
        f"/trips/{trip}/readings",
        json={
            "reading_id": rid,
            "read_at": at,
            "temperature": str(temp),
        },
    )


def meas(client, eid, at, temp, cargo="C1"):
    return client.post(
        "/events",
        json={
            "event_id": eid,
            "cargo_id": cargo,
            "occurred_at": at,
            "temperature": str(temp),
        },
    )


def handover(client, eid, at, giver, receiver, cargo="C1"):
    return client.post(
        "/events",
        json={
            "event_id": eid,
            "cargo_id": cargo,
            "occurred_at": at,
            "giver": giver,
            "receiver": receiver,
        },
    )


# --------------------------------------------------------------- 车次


def test_create_and_get_trip(client):
    mk_trip(client, "T1", "京A12345")
    r = client.get("/trips/T1")
    assert r.status_code == 200
    assert r.json()["trip_id"] == "T1"
    assert r.json()["vehicle_no"] == "京A12345"
    assert [t["trip_id"] for t in client.get("/trips").json()] == ["T1"]


def test_duplicate_trip_rejected(client):
    mk_trip(client, "T1")
    r = client.post("/trips", json={"trip_id": "T1"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "trip_exists"


def test_trip_not_found_404(client):
    assert client.get("/trips/NOPE").status_code == 404
    r = reading(client, "NOPE", "r1", "2026-01-01T11:00:00+00:00", 5)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "trip_not_found"


# ------------------------------------------------------------- 装卸时段


def test_manifest_register_and_list(client):
    mk_cargo(client)
    mk_trip(client)
    r = manifest(client, "T1", [W("C1", 11, 12)])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] == 1 and body["replayed"] == 0
    rows = client.get("/trips/T1/manifest").json()
    assert len(rows) == 1
    assert rows[0]["loaded_at"] == "2026-01-01T11:00:00Z"
    assert rows[0]["unloaded_at"] == "2026-01-01T12:00:00Z"


def test_manifest_invalid_window_rejected(client):
    mk_cargo(client)
    mk_trip(client)
    # 起点 == 终点
    r = manifest(client, "T1", [W("C1", 11, 11)])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_loading_window"
    # 起点晚于终点
    r2 = manifest(
        client,
        "T1",
        [{
            "cargo_id": "C1",
            "loaded_at": "2026-01-01T12:00:00+00:00",
            "unloaded_at": "2026-01-01T11:00:00+00:00",
        }],
    )
    assert r2.status_code == 422
    assert client.get("/trips/T1/manifest").json() == []


def test_manifest_unknown_cargo_or_trip_rejected(client):
    mk_trip(client, "T1")
    r = manifest(client, "T1", [W("NOPE", 11, 12)])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "cargo_not_found"
    mk_cargo(client)
    r2 = manifest(client, "NOPE", [W("C1", 11, 12)])
    assert r2.status_code == 404
    assert r2.json()["error"]["code"] == "trip_not_found"


def test_manifest_batch_partial_invalid_leaves_nothing(client):
    mk_cargo(client, "C1")
    mk_cargo(client, "C2", holder="B")
    mk_trip(client)
    r = manifest(client, "T1", [
        W("C2", 15, 16),          # 合法
        W("C1", 12, 11),          # 非法窗口
    ])
    assert r.status_code == 422
    rows = client.get("/trips/T1/manifest").json()
    assert rows == []             # 整体拒绝，不留部分记录


def test_manifest_same_cargo_cannot_be_on_two_trips_overlapping(client):
    mk_cargo(client)
    mk_trip(client, "T1")
    mk_trip(client, "T2")
    assert manifest(client, "T1", [W("C1", 11, 13)]).status_code == 201
    r = manifest(client, "T2", [W("C1", 12, 14)])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "loading_overlap"
    assert [m["trip_id"] for m in client.get("/trips/T2/manifest").json()] == []


def test_manifest_touching_windows_allowed_same_and_cross_trip(client):
    mk_cargo(client)
    mk_trip(client, "T1")
    mk_trip(client, "T2")
    assert manifest(client, "T1", [W("C1", 11, 12)]).status_code == 201
    # 首尾相接（左闭右开，12:00 不在第一段）：跨车次允许
    assert manifest(client, "T2", [W("C1", 12, 13)]).status_code == 201
    # 同车次同货物登记多段不重叠窗口也允许
    assert manifest(client, "T1", [W("C1", 14, 15)]).status_code == 201


def test_manifest_within_batch_overlap_rejected(client):
    mk_cargo(client)
    mk_trip(client)
    r = manifest(client, "T1", [W("C1", 11, 13), W("C1", 12, 14)])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "loading_overlap"
    assert client.get("/trips/T1/manifest").json() == []


def test_manifest_within_batch_same_start_different_end_conflict(client):
    mk_cargo(client)
    mk_trip(client)
    r = manifest(client, "T1", [W("C1", 11, 13), W("C1", 11, 14)])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "manifest_id_conflict"
    assert client.get("/trips/T1/manifest").json() == []


def test_manifest_within_batch_exact_duplicate_deduped(client):
    mk_cargo(client)
    mk_trip(client)
    r = manifest(client, "T1", [W("C1", 11, 13), W("C1", 11, 13)])
    assert r.status_code == 201
    assert len(client.get("/trips/T1/manifest").json()) == 1


def test_manifest_idempotent_replay_and_content_conflict(client):
    mk_cargo(client)
    mk_trip(client)
    body = [W("C1", 11, 13)]
    first = manifest(client, "T1", body)
    assert first.status_code == 201
    second = manifest(client, "T1", body)
    assert second.status_code == 200
    assert second.json()["all_replayed"] is True
    assert second.json()["replayed"] == 1
    # 同 (车次,货物,装车时刻) 不同终点 -> 冲突
    r = manifest(client, "T1", [W("C1", 11, 12)])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "manifest_id_conflict"


# ------------------------------------------------------------- 车载读数


def test_reading_idempotent_conflict_and_unknown_trip(client):
    mk_cargo(client)
    mk_trip(client)
    manifest(client, "T1", [W("C1", 11, 13)])
    body = {"reading_id": "r1", "read_at": "2026-01-01T11:30:00+00:00", "temperature": "8"}
    assert client.post("/trips/T1/readings", json=body).status_code == 201
    again = client.post("/trips/T1/readings", json=body)
    assert again.status_code == 200
    assert again.json()["replayed"] is True
    # 异内容
    body["temperature"] = "9"
    r = client.post("/trips/T1/readings", json=body)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "reading_id_conflict"


def test_two_readings_same_instant_same_trip_conflict(client):
    mk_cargo(client)
    mk_trip(client)
    manifest(client, "T1", [W("C1", 11, 13)])
    reading(client, "T1", "r1", "2026-01-01T11:30:00+00:00", 8)
    r = reading(client, "T1", "r2", "2026-01-01T11:30:00+00:00", 7)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "reading_same_timestamp_conflict"


def test_reading_applies_only_while_on_vehicle_half_open(client):
    mk_cargo(client, limit="5")
    mk_trip(client)
    manifest(client, "T1", [W("C1", 11, 12)])
    # 装车前 10:30、卸车时刻 12:00（右开）均不作用；车上 11:30 作用
    reading(client, "T1", "before", "2026-01-01T10:30:00+00:00", 9)
    reading(client, "T1", "on", "2026-01-01T11:30:00+00:00", 9)
    reading(client, "T1", "edge", "2026-01-01T12:00:00+00:00", 9)
    rpt = client.get("/cargo/C1/report").json()
    assert len(rpt["excursions"]) == 1
    ex = rpt["excursions"][0]
    assert ex["start"] == "2026-01-01T11:30:00Z"
    assert ex["end"] is None
    assert ex["interval_id"] == "trip:T1:reading:on"
    assert ex["start_source"] == {
        "source": "onboard", "event_id": None, "trip_id": "T1", "reading_id": "on"
    }
    # 车次读数：edge 时刻车上无货物
    details = {d["reading_id"]: d for d in client.get("/trips/T1/readings").json()["readings"]}
    assert details["before"]["onboard_cargos"] == []
    assert details["edge"]["onboard_cargos"] == []
    assert [x["cargo_id"] for x in details["on"]["onboard_cargos"]] == ["C1"]


def test_per_cargo_limits_and_multi_cargo_trip(client):
    mk_cargo(client, "C1", limit="5", holder="A")
    mk_cargo(client, "C2", limit="2", holder="X")
    mk_trip(client)
    manifest(client, "T1", [W("C1", 11, 13), W("C2", 11, 12)])
    # 11:30 温度 4：C1（限 5）不越温；C2（限 2）越温
    reading(client, "T1", "r1", "2026-01-01T11:30:00+00:00", 4)
    c1 = client.get("/cargo/C1/report").json()["excursions"]
    # 11:45 温度 1：C2 闭合（C1 全程不越温）
    reading(client, "T1", "r2", "2026-01-01T11:45:00+00:00", 1)
    assert c1 == []
    c2 = client.get("/cargo/C2/report").json()["excursions"]
    assert len(c2) == 1 and c2[0]["interval_id"] == "trip:T1:reading:r1"

    detail = next(
        d for d in client.get("/trips/T1/readings").json()["readings"]
        if d["reading_id"] == "r1"
    )
    by = {x["cargo_id"]: x for x in detail["onboard_cargos"]}
    assert by["C1"]["over_limit"] is False
    assert by["C2"]["over_limit"] is True
    assert set(detail["affected_cargo_ids"]) == {"C2"}


def test_onboard_reading_does_not_change_holder(client):
    mk_cargo(client, holder="A")
    mk_trip(client)
    manifest(client, "T1", [W("C1", 11, 13)])
    reading(client, "T1", "r1", "2026-01-01T11:30:00+00:00", 9)
    assert client.get("/cargo/C1/report").json()["current_holder"] == "A"


def test_onboard_excursion_split_by_handover(client):
    mk_cargo(client, limit="5", holder="A")
    mk_trip(client)
    manifest(client, "T1", [W("C1", 11, 13)])
    reading(client, "T1", "r1", "2026-01-01T11:15:00+00:00", 8)
    assert handover(client, "h1", "2026-01-01T11:30:00+00:00", "A", "B").status_code == 201
    reading(client, "T1", "r2", "2026-01-01T11:45:00+00:00", 3)
    ex = client.get("/cargo/C1/report").json()["excursions"][0]
    assert [(s["responsible_party"]) for s in ex["segments"]] == ["A", "B"]
    assert ex["start_event_id"] is None and ex["end_event_id"] is None
    assert ex["start_source"]["source"] == "onboard"
    assert ex["end_source"]["source"] == "onboard"


def test_own_measurement_same_instant_takes_precedence(client):
    mk_cargo(client, limit="5", holder="A")
    mk_trip(client)
    manifest(client, "T1", [W("C1", 11, 13)])
    reading(client, "T1", "rover", "2026-01-01T11:30:00+00:00", 8)
    # 同刻货物自有测温 2（不越温）：车载读数被遮蔽，区间不开
    meas(client, "m-own", "2026-01-01T11:30:00+00:00", 2)
    assert client.get("/cargo/C1/report").json()["excursions"] == []
    tl = client.get("/cargo/C1/readings").json()["readings"]
    at = [x for x in tl if x["at"] == "2026-01-01T11:30:00Z"]
    assert {x["source"]: x["adopted"] for x in at} == {"onboard": False, "own": True}
    # 车载原始读数仍可在车次接口查到，且标注未被采用
    d = client.get("/trips/T1/readings").json()["readings"][0]
    assert d["reading_id"] == "rover"
    assert d["onboard_cargos"][0]["adopted"] is False
    # 货物事件列表仍只有自有事件
    assert [e["event_id"] for e in client.get("/cargo/C1/events").json()] == ["m-own"]


def test_readings_allowed_late_and_rebuild(client):
    mk_cargo(client, limit="5", holder="A")
    mk_trip(client)
    manifest(client, "T1", [W("C1", 11, 13)])
    # 先闭合读数，后补更早的开启读数（乱序晚到）
    reading(client, "T1", "r-late-close", "2026-01-01T11:50:00+00:00", 3)
    assert client.get("/cargo/C1/report").json()["excursions"] == []
    reading(client, "T1", "r-late-open", "2026-01-01T11:10:00+00:00", 9)
    ex = client.get("/cargo/C1/report").json()["excursions"][0]
    assert ex["interval_id"] == "trip:T1:reading:r-late-open"
    assert (ex["start"], ex["end"]) == (
        "2026-01-01T11:10:00Z",
        "2026-01-01T11:50:00Z",
    )
    assert ex["segments"][0]["responsible_party"] == "A"


def test_manifest_registered_late_makes_past_reading_applicable(client):
    """登记晚于读数上报：重建后历史读数按装卸时段补作用。"""
    mk_cargo(client, limit="5")
    mk_trip(client)
    reading(client, "T1", "r1", "2026-01-01T11:30:00+00:00", 9)
    assert client.get("/cargo/C1/report").json()["excursions"] == []
    manifest(client, "T1", [W("C1", 11, 13)])
    ex = client.get("/cargo/C1/report").json()["excursions"][0]
    assert ex["interval_id"] == "trip:T1:reading:r1"


# ------------------------------------------------------- 车载区间责任确认


def _closed_onboard_interval(client, cargo="C1"):
    ivs = client.get(f"/cargo/{cargo}/responsibilities").json()["intervals"]
    assert ivs, "应有已闭合区间"
    return ivs[0]


def test_onboard_interval_responsibility_stances(client):
    mk_cargo(client, limit="5", holder="A")
    mk_trip(client)
    manifest(client, "T1", [W("C1", 11, 13)])
    reading(client, "T1", "r1", "2026-01-01T11:15:00+00:00", 8)
    handover(client, "h1", "2026-01-01T11:30:00+00:00", "A", "B")
    reading(client, "T1", "r2", "2026-01-01T11:45:00+00:00", 3)

    iv = _closed_onboard_interval(client)
    iid = "trip:T1:reading:r1"
    assert iv["interval_id"] == iid
    assert iv["start_event_id"] is None
    assert iv["start_source"]["reading_id"] == "r1"
    assert iv["end_source"]["reading_id"] == "r2"

    # 直查
    assert client.get(f"/cargo/C1/responsibilities/{iid}").status_code == 200
    # A/B 分别表态 -> all_confirmed
    for party, sid in [("A", "sa"), ("B", "sb")]:
        r = client.post(
            f"/cargo/C1/responsibilities/{iid}/stances",
            json={
                "snapshot_id": iv["snapshot_id"],
                "responsible_party": party,
                "stance": "confirm",
                "stance_id": sid,
            },
        )
        assert r.status_code == 201, r.text
    iv2 = _closed_onboard_interval(client)
    assert iv2["summary"] == "all_confirmed"


def test_open_onboard_interval_not_listed_and_stance_rejected(client):
    mk_cargo(client)
    mk_trip(client)
    manifest(client, "T1", [W("C1", 11, 13)])
    reading(client, "T1", "r1", "2026-01-01T11:30:00+00:00", 9)
    assert client.get("/cargo/C1/responsibilities").json()["intervals"] == []
    iid = "trip:T1:reading:r1"
    r = client.get(f"/cargo/C1/responsibilities/{iid}")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "interval_open"
    r2 = client.post(
        f"/cargo/C1/responsibilities/{iid}/stances",
        json={
            "snapshot_id": "sha256:" + "0" * 64,
            "responsible_party": "A",
            "stance": "confirm",
            "stance_id": "s1",
        },
    )
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "interval_open"


def test_late_reading_invalidates_old_stance_and_history(client):
    mk_cargo(client, limit="5", holder="A")
    mk_trip(client)
    manifest(client, "T1", [W("C1", 11, 13)])
    reading(client, "T1", "r1", "2026-01-01T11:15:00+00:00", 8)
    reading(client, "T1", "r2", "2026-01-01T11:45:00+00:00", 3)
    iv = _closed_onboard_interval(client)
    old_iid = iv["interval_id"]
    client.post(
        f"/cargo/C1/responsibilities/{old_iid}/stances",
        json={
            "snapshot_id": iv["snapshot_id"],
            "responsible_party": "A",
            "stance": "confirm",
            "stance_id": "sa",
        },
    )
    # 晚到更早的超限读数 r0 -> 开启点前移，区间标识改变，旧区间消失
    reading(client, "T1", "r0", "2026-01-01T11:05:00+00:00", 9)
    new_iv = _closed_onboard_interval(client)
    assert new_iv["interval_id"] == "trip:T1:reading:r0"

    hist = client.get(
        f"/cargo/C1/responsibilities/{old_iid}/stances"
    ).json()
    assert hist["interval_exists"] is False
    assert hist["current_snapshot_id"] is None
    assert hist["history"][0]["is_current"] is False

    # 旧表态同编号同内容重传：区间已消失仍 200 返回原记录（幂等优先）
    replay = client.post(
        f"/cargo/C1/responsibilities/{old_iid}/stances",
        json={
            "snapshot_id": iv["snapshot_id"],
            "responsible_party": "A",
            "stance": "confirm",
            "stance_id": "sa",
        },
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["stance"]["stance_id"] == "sa"
    # 历史未新增
    assert len(hist["history"]) == 1


def test_mixed_own_and_onboard_interval_id_switch(client):
    """车载读数先开启；晚到更早的自有测温开启 -> 区间标识切到事件ID。"""
    mk_cargo(client, limit="5", holder="A")
    mk_trip(client)
    manifest(client, "T1", [W("C1", 11, 13)])
    reading(client, "T1", "r1", "2026-01-01T11:20:00+00:00", 8)
    meas(client, "m-close", "2026-01-01T11:40:00+00:00", 3)
    iv = _closed_onboard_interval(client)
    assert iv["interval_id"] == "trip:T1:reading:r1"
    assert iv["start_source"]["source"] == "onboard"
    assert iv["end_source"]["source"] == "own"
    assert iv["end_event_id"] == "m-close"

    # 晚到 11:10 自有超限测温 -> 新开启点为自有事件
    meas(client, "m-open", "2026-01-01T11:10:00+00:00", 9)
    iv2 = _closed_onboard_interval(client)
    assert iv2["interval_id"] == "m-open"
    assert iv2["start_event_id"] == "m-open"
    assert iv2["start_source"]["source"] == "own"
    assert iv2["snapshot_id"] != iv["snapshot_id"]


def test_cargo_readings_timeline_includes_raw_and_adoption(client):
    mk_cargo(client, limit="5")
    mk_trip(client, "T1")
    mk_trip(client, "T2")
    manifest(client, "T1", [W("C1", 11, 12)])
    manifest(client, "T2", [W("C1", 12, 13)])
    reading(client, "T1", "r1", "2026-01-01T11:30:00+00:00", 8)
    reading(client, "T2", "r2", "2026-01-01T12:30:00+00:00", 2)
    meas(client, "m1", "2026-01-01T11:45:00+00:00", 3)
    tl = client.get("/cargo/C1/readings").json()
    assert [(x["reading_id"] or x["event_id"], x["source"], x["adopted"])
            for x in tl["readings"]] == [
        ("r1", "onboard", True),
        ("m1", "own", True),
        ("r2", "onboard", True),
    ]
    assert [x["over_limit"] for x in tl["readings"]] == [True, False, False]


def test_trip_reading_detail_shows_unloaded_cargo_excluded(client):
    mk_cargo(client, "C1", limit="5")
    mk_cargo(client, "C2", limit="9", holder="B")
    mk_trip(client)
    manifest(client, "T1", [W("C1", 11, 13), W("C2", 11, 12)])
    reading(client, "T1", "r1", "2026-01-01T12:30:00+00:00", 6)
    d = client.get("/trips/T1/readings").json()["readings"][0]
    assert [x["cargo_id"] for x in d["onboard_cargos"]] == ["C1"]
    # 6 对 C1（限5）越温，对已下车的 C2 不判断
    assert d["onboard_cargos"][0]["over_limit"] is True
    assert d["affected_cargo_ids"] == ["C1"]


def test_stale_snapshot_after_late_reading_rejected(client):
    mk_cargo(client, limit="5", holder="A")
    mk_trip(client)
    manifest(client, "T1", [W("C1", 11, 13)])
    reading(client, "T1", "r1", "2026-01-01T11:15:00+00:00", 8)
    handover(client, "h1", "2026-01-01T11:30:00+00:00", "A", "B")
    reading(client, "T1", "r2", "2026-01-01T11:45:00+00:00", 3)
    stale = _closed_onboard_interval(client)["snapshot_id"]
    # 晚到 11:40 合法交接 B->C（不与既有交接矛盾）：B 的片段缩短，快照变化
    assert handover(client, "h2", "2026-01-01T11:40:00+00:00", "B", "C").status_code == 201
    r = client.post(
        "/cargo/C1/responsibilities/trip:T1:reading:r1/stances",
        json={
            "snapshot_id": stale,
            "responsible_party": "A",
            "stance": "confirm",
            "stance_id": "s1",
        },
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_snapshot"
