"""车次、装卸时段登记与车载读数写入。

串行化（与货物事件写入、责任表态共用同一套货物行锁）
----------------------------------------------------
* 装卸时段登记：锁车次行 + 涉及货物行（按 cargo_id 升序），与同货物的
  事件写入 / 表态 / 其他时段登记互斥，重叠判定与写入原子完成；
* 车载读数写入：车次行 + 读数时刻在车上的货物行（按 cargo_id 升序），
  与这些货物的事件写入、表态严格串行——读到的就是持锁期间最新的
  装卸时段 / 事件 / 表态状态；
* 所有多货物加锁均按 cargo_id 升序，避免死锁。

整体拒绝：装载时段无效（起点不早于终点）、货物或车次不存在、装卸时段
重叠、读数编号冲突等任一校验失败即回滚，不留部分记录。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from app import crud
from app.crud import ApiError, _conflict, _unprocessable
from app.models import Cargo, Trip, TripManifest, TripReading
from app.schemas import ManifestRegister, ReadingCreate, TripCreate
from app.timeutil import ensure_utc


# --------------------------------------------------------------- 车次


def create_trip(db, payload: TripCreate) -> Trip:
    trip = Trip(trip_id=payload.trip_id, vehicle_no=payload.vehicle_no)
    db.add(trip)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise _conflict(f"车次编号 {payload.trip_id} 已存在", code="trip_exists")
    return trip


def _get_trip(db, trip_id: str) -> Trip | None:
    return db.get(Trip, trip_id)


def _load_trip_locked(db, trip_id: str) -> Trip:
    stmt = select(Trip).where(Trip.trip_id == trip_id).with_for_update()
    trip = db.scalars(stmt).first()
    if trip is None:
        raise ApiError(404, "trip_not_found", f"车次 {trip_id} 不存在")
    return trip


def _lock_cargos(db, cargo_ids) -> dict[str, Cargo]:
    """按 cargo_id 升序锁货物行（统一加锁顺序避免死锁）。"""
    cargos: dict[str, Cargo] = {}
    for cid in sorted(set(cargo_ids)):
        stmt = select(Cargo).where(Cargo.cargo_id == cid).with_for_update()
        cargo = db.scalars(stmt).first()
        if cargo is None:
            raise ApiError(404, "cargo_not_found", f"货物 {cid} 不存在")
        cargo.started_at = ensure_utc(cargo.started_at)
        cargos[cid] = cargo
    return cargos


# ----------------------------------------------------------- 装卸时段


def _windows_overlap(a_start, a_end, b_start, b_end) -> bool:
    """左闭右开时段相交判定（首尾相接不算重叠）。"""
    return a_start < b_end and b_start < a_end


def register_manifest(db, trip_id: str, payload: ManifestRegister):
    """登记一批货物的装卸时段；全部成功或全部拒绝（无部分记录）。"""
    items = []
    for it in payload.items:
        loaded = ensure_utc(it.loaded_at)
        unloaded = ensure_utc(it.unloaded_at)
        if not loaded < unloaded:
            raise _unprocessable(
                f"货物 {it.cargo_id} 的装载时段非法：起点 {loaded.isoformat()} "
                f"必须严格早于终点 {unloaded.isoformat()}（左闭右开）",
                code="invalid_loading_window",
            )
        items.append((it.cargo_id, loaded, unloaded))

    try:
        # 先锁车次（车次不存在直接 404），再按 cargo_id 升序锁货物行
        _load_trip_locked(db, trip_id)
        _lock_cargos(db, [cid for cid, _, _ in items])

        # 批内自身去重 / 重叠检查：完全相同的条目按重传去重；
        # 同起点不同终点按冲突，时段相交按重叠拒绝
        batch_by_cargo: dict[str, list] = {}
        dedup_items: list = []
        for cid, loaded, unloaded in items:
            duplicate = False
            for ex_loaded, ex_unloaded in batch_by_cargo.setdefault(cid, []):
                if ex_loaded == loaded:
                    if ex_unloaded == unloaded:
                        duplicate = True  # 批内完全重复：去重，后续按幂等命中
                        break
                    raise _conflict(
                        f"货物 {cid} 在车次 {trip_id} 上起点 "
                        f"{ex_loaded.isoformat()} 对应两个不同终点",
                        code="manifest_id_conflict",
                    )
                if _windows_overlap(loaded, unloaded, ex_loaded, ex_unloaded):
                    raise _unprocessable(
                        f"货物 {cid} 在请求内的装卸时段重叠："
                        f"[{loaded.isoformat()},{unloaded.isoformat()}) 与 "
                        f"[{ex_loaded.isoformat()},{ex_unloaded.isoformat()})",
                        code="loading_overlap",
                    )
            if not duplicate:
                batch_by_cargo[cid].append((loaded, unloaded))
                dedup_items.append((cid, loaded, unloaded))
        items = dedup_items
        if not items:
            raise _unprocessable("登记条目不能为空", code="invalid_loading_window")

        # 与既有登记（任意车次）比对：同货物不得同时属于两个车次
        replayed_rows: list[TripManifest] = []
        new_rows: list[TripManifest] = []
        for cid, loaded, unloaded in items:
            existing = db.get(TripManifest, (trip_id, cid, loaded))
            if existing is not None:
                existing.unloaded_at = ensure_utc(existing.unloaded_at)
                if existing.unloaded_at != unloaded:
                    raise _conflict(
                        f"货物 {cid} 在车次 {trip_id} 上起点 {loaded.isoformat()} "
                        "的装卸时段已存在且终点不同，拒绝覆盖",
                        code="manifest_id_conflict",
                    )
                replayed_rows.append(existing)
                continue
            for m in crud._load_manifests(db, cid):
                if m.trip_id != trip_id and _windows_overlap(
                    loaded, unloaded, m.loaded_at, m.unloaded_at
                ):
                    raise _unprocessable(
                        f"货物 {cid} 已在时段 [{m.loaded_at.isoformat()},"
                        f"{m.unloaded_at.isoformat()}) 属于车次 {m.trip_id}，"
                        "同一时刻不得同时属于两个车次",
                        code="loading_overlap",
                    )
                if m.trip_id == trip_id and _windows_overlap(
                    loaded, unloaded, m.loaded_at, m.unloaded_at
                ):
                    raise _unprocessable(
                        f"货物 {cid} 在车次 {trip_id} 上的既有装卸时段 "
                        f"[{m.loaded_at.isoformat()},{m.unloaded_at.isoformat()}) "
                        "与本次登记重叠",
                        code="loading_overlap",
                    )
            row = TripManifest(
                trip_id=trip_id,
                cargo_id=cid,
                loaded_at=loaded,
                unloaded_at=unloaded,
            )
            db.add(row)
            new_rows.append(row)

        db.commit()
    except ApiError:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        # 无行级锁的引擎（SQLite）并发穿透时，唯一索引兜底：
        # 重新判一次重叠，给出与持锁路径一致的业务错误码
        for cid, loaded, unloaded in items:
            for m in crud._load_manifests(db, cid):
                if m.trip_id != trip_id and _windows_overlap(
                    loaded, unloaded, m.loaded_at, m.unloaded_at
                ):
                    raise _unprocessable(
                        f"货物 {cid} 已在时段 [{m.loaded_at.isoformat()},"
                        f"{m.unloaded_at.isoformat()}) 属于车次 {m.trip_id}，"
                        "同一时刻不得同时属于两个车次",
                        code="loading_overlap",
                    )
                if (
                    m.trip_id == trip_id
                    and m.loaded_at == loaded
                    and m.unloaded_at != unloaded
                ):
                    raise _conflict(
                        f"货物 {cid} 在车次 {trip_id} 上起点 {loaded.isoformat()} "
                        "的装卸时段已存在且终点不同，拒绝覆盖",
                        code="manifest_id_conflict",
                    )
        raise _conflict(
            "装卸时段登记与既有数据冲突（车次/货物不存在、重复登记或时段约束）",
            code="integrity_conflict",
        )

    return ManifestWriteResult(
        manifests=sorted(replayed_rows + new_rows, key=lambda m: (m.cargo_id, m.loaded_at)),
        created=len(new_rows),
        replayed=len(replayed_rows),
        all_replayed=bool(items) and not new_rows,
    )


@dataclass
class ManifestWriteResult:
    manifests: list[TripManifest]
    created: int
    replayed: int
    all_replayed: bool


# ------------------------------------------------------------- 车载读数


def _same_reading_content(existing: TripReading, candidate: TripReading) -> bool:
    return (
        existing.trip_id == candidate.trip_id
        and ensure_utc(existing.read_at) == candidate.read_at
        and existing.temperature == candidate.temperature
    )


def submit_reading(db, trip_id: str, payload: ReadingCreate):
    """写入一条车载读数（允许晚到）。同编号同内容幂等、异内容冲突。

    读数本身按车次存储；对货物的作用（装卸时段投影、越温判定）全部在
    查询 / 重建时计算，因此读数晚到无需回写，只需与受影响货物串行即可。
    """
    read_at = ensure_utc(payload.read_at)
    candidate = TripReading(
        reading_id=payload.reading_id,
        trip_id=trip_id,
        read_at=read_at,
        temperature=payload.temperature,
    )

    def _tx():
        _load_trip_locked(db, trip_id)

        existing = db.get(TripReading, payload.reading_id)
        if existing is not None:
            if _same_reading_content(existing, candidate):
                db.rollback()
                return ReadingWriteResult(reading=existing, replayed=True)
            raise _conflict(
                f"车载读数编号 {payload.reading_id} 已存在且内容不同，拒绝覆盖",
                code="reading_id_conflict",
            )

        # 同一车次同一时刻至多一笔原始读数
        clash = db.scalars(
            select(TripReading).where(
                TripReading.trip_id == trip_id,
                TripReading.read_at == read_at,
            )
        ).first()
        if clash is not None:
            raise _conflict(
                f"车次 {trip_id} 在 {read_at.isoformat()} 已存在车载读数 "
                f"{clash.reading_id}，同一时刻不允许重复上报",
                code="reading_same_timestamp_conflict",
            )

        # 确定读数时刻在车上的货物，并按 cargo_id 升序加锁——
        # 与这些货物的事件写入 / 表态串行一致
        manifests = list(
            db.scalars(
                select(TripManifest).where(
                    TripManifest.trip_id == trip_id,
                    TripManifest.loaded_at <= read_at,
                    TripManifest.unloaded_at > read_at,
                )
            ).all()
        )
        for m in manifests:
            m.loaded_at = ensure_utc(m.loaded_at)
            m.unloaded_at = ensure_utc(m.unloaded_at)
        _lock_cargos(db, [m.cargo_id for m in manifests])

        db.add(candidate)
        db.commit()
        return ReadingWriteResult(reading=candidate, replayed=False)

    try:
        return _tx()
    except ApiError:
        db.rollback()
        raise
    except OperationalError:
        # SQLite 文件库极端并发下 busy timeout 耗尽；整事务重来语义不变
        db.rollback()
        return _tx()
    except IntegrityError:
        db.rollback()
        raise _conflict(
            f"车载读数 {payload.reading_id} 与既有数据冲突"
            "（读数编号冲突 / 车次不存在 / 字段约束）",
            code="integrity_conflict",
        )


@dataclass
class ReadingWriteResult:
    reading: TripReading
    replayed: bool
