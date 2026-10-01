"""冷链货物追责服务 —— FastAPI 入口。

启动（开发）: uvicorn app.main:app --reload
一键启动    : docker compose up --build  （见 README）
"""
from contextlib import asynccontextmanager
from decimal import Decimal

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import chain, crud, responsibility, stance_crud, trip_crud
from app.database import Base, engine, get_db
from app.models import (
    Cargo,
    Event,
    Stance,
    StanceKind,
    Trip,
    TripManifest,
    TripReading,
)
from app.schemas import (
    CargoCreate,
    CargoOut,
    CargoReadingItem,
    CargoReadings,
    ChainReport,
    EventCreate,
    EventOut,
    Excursion,
    IdempotentReplay,
    LatestStance,
    ManifestOut,
    ManifestRegister,
    ManifestResult,
    OnboardCargo,
    ReadingCreate,
    ReadingDetail,
    ReadingOut,
    ReadingReplay,
    ReadingSourceRef,
    ResponsibilityInterval,
    ResponsibilityReport,
    ResponsibilitySegment,
    StanceCreate,
    StanceHistory,
    StanceHistoryItem,
    StanceOut,
    StanceReplay,
    StanceSegment,
    TripCreate,
    TripOut,
    TripReadings,
)
from app.timeutil import ensure_utc


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 生产中由 db/init.sql 建表；此处确保直接 uvicorn / sqlite 测试也可用
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="冷链货物追责服务",
    description=(
        "记录冷链货物的测温与交接事件、车次装卸时段与车载测温读数，"
        "按发生时间重建保管链（货物自有测温与车载读数合并，自有测温同刻优先），"
        "计算越温区间并拆分各保管方责任。"
    ),
    version="2.0.0",
    lifespan=lifespan,
)


@app.exception_handler(crud.ApiError)
async def api_error_handler(request: Request, exc: crud.ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


# ---------------------------------------------------------------- 货物


@app.post("/cargo", response_model=CargoOut, status_code=201, tags=["货物"])
def create_cargo(payload: CargoCreate, db: Session = Depends(get_db)) -> Cargo:
    payload.started_at = ensure_utc(payload.started_at)
    return crud.create_cargo(db, payload)


@app.get("/cargo", response_model=list[CargoOut], tags=["货物"])
def list_cargo(db: Session = Depends(get_db)) -> list[Cargo]:
    return list(db.scalars(select(Cargo).order_by(Cargo.cargo_id)).all())


@app.get("/cargo/{cargo_id}", response_model=CargoOut, tags=["货物"])
def get_cargo(cargo_id: str, db: Session = Depends(get_db)) -> Cargo:
    cargo = db.get(Cargo, cargo_id)
    if cargo is None:
        raise crud.ApiError(404, "cargo_not_found", f"货物 {cargo_id} 不存在")
    return cargo


# ---------------------------------------------------------------- 事件


@app.post(
    "/events",
    response_model=EventOut,
    status_code=201,
    tags=["事件"],
    responses={
        200: {
            "model": IdempotentReplay,
            "description": "同 ID 同内容重传：不重复写入",
        }
    },
)
def create_event(payload: EventCreate, db: Session = Depends(get_db)):
    payload.occurred_at = ensure_utc(payload.occurred_at)
    result = crud.create_event(db, payload)
    if result.replayed:
        return JSONResponse(
            status_code=200,
            content=IdempotentReplay(
                event_id=payload.event_id,
                message="相同内容的事件已存在，重传被忽略（幂等）",
            ).model_dump(),
        )
    return result.event


@app.get(
    "/cargo/{cargo_id}/events",
    response_model=list[EventOut],
    tags=["事件"],
)
def list_events(cargo_id: str, db: Session = Depends(get_db)) -> list[Event]:
    """货物事件列表：**只列货物自有事件**（测温 / 交接），车载读数不在此列。"""
    if db.get(Cargo, cargo_id) is None:
        raise crud.ApiError(404, "cargo_not_found", f"货物 {cargo_id} 不存在")
    stmt = (
        select(Event)
        .where(Event.cargo_id == cargo_id)
        .order_by(Event.occurred_at, Event.event_id)
    )
    events = list(db.scalars(stmt).all())
    for e in events:
        e.occurred_at = ensure_utc(e.occurred_at)
    return events


# ---------------------------------------------------------------- 车次


@app.post("/trips", response_model=TripOut, status_code=201, tags=["车次"])
def create_trip(payload: TripCreate, db: Session = Depends(get_db)) -> Trip:
    return trip_crud.create_trip(db, payload)


@app.get("/trips", response_model=list[TripOut], tags=["车次"])
def list_trips(db: Session = Depends(get_db)) -> list[Trip]:
    return list(db.scalars(select(Trip).order_by(Trip.trip_id)).all())


@app.get("/trips/{trip_id}", response_model=TripOut, tags=["车次"])
def get_trip(trip_id: str, db: Session = Depends(get_db)) -> Trip:
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise crud.ApiError(404, "trip_not_found", f"车次 {trip_id} 不存在")
    return trip


@app.post(
    "/trips/{trip_id}/manifest",
    response_model=ManifestResult,
    status_code=201,
    tags=["车次"],
    responses={
        200: {"model": ManifestResult, "description": "全部条目幂等重传"},
    },
)
def register_manifest(
    trip_id: str, payload: ManifestRegister, db: Session = Depends(get_db)
):
    """登记车次承载货物的装卸时段（左闭右开，起点严格早于终点）。

    同货物不得同时属于两个车次（时段重叠即拒绝）；任一条目不合法则整批拒绝、
    不留部分记录。同 (车次, 货物, 装车时刻) 且终点一致为重传幂等。
    """
    result = trip_crud.register_manifest(db, trip_id, payload)
    body = ManifestResult(
        trip_id=trip_id,
        manifests=[
            ManifestOut(
                trip_id=m.trip_id,
                cargo_id=m.cargo_id,
                loaded_at=ensure_utc(m.loaded_at),
                unloaded_at=ensure_utc(m.unloaded_at),
            )
            for m in result.manifests
        ],
        created=result.created,
        replayed=result.replayed,
        all_replayed=result.all_replayed,
    )
    if result.all_replayed:
        return JSONResponse(status_code=200, content=body.model_dump(mode="json"))
    return body


@app.get(
    "/trips/{trip_id}/manifest",
    response_model=list[ManifestOut],
    tags=["车次"],
)
def list_manifest(trip_id: str, db: Session = Depends(get_db)):
    if db.get(Trip, trip_id) is None:
        raise crud.ApiError(404, "trip_not_found", f"车次 {trip_id} 不存在")
    rows = list(
        db.scalars(
            select(TripManifest)
            .where(TripManifest.trip_id == trip_id)
            .order_by(TripManifest.loaded_at, TripManifest.cargo_id)
        ).all()
    )
    return [
        ManifestOut(
            trip_id=m.trip_id,
            cargo_id=m.cargo_id,
            loaded_at=ensure_utc(m.loaded_at),
            unloaded_at=ensure_utc(m.unloaded_at),
        )
        for m in rows
    ]


# ------------------------------------------------------------- 车载读数


def _reading_detail(db: Session, r: TripReading) -> ReadingDetail:
    """装配一条车载读数的受影响货物视图（读数时刻在车上 + 各自上限 + 采用情况）。"""
    at = ensure_utc(r.read_at)
    manifests = list(
        db.scalars(
            select(TripManifest).where(
                TripManifest.trip_id == r.trip_id,
                TripManifest.loaded_at <= at,
                TripManifest.unloaded_at > at,
            )
        ).all()
    )
    cargos = []
    adopted_ids = []
    own_event_times = {
        ensure_utc(e.occurred_at)
        for e in db.scalars(
            select(Event).where(
                Event.cargo_id.in_([m.cargo_id for m in manifests] or [""])
            )
        ).all()
    }
    for m in sorted(manifests, key=lambda x: x.cargo_id):
        cargo = db.get(Cargo, m.cargo_id)
        limit = Decimal(cargo.temp_upper_limit)
        adopted = at not in own_event_times
        over = Decimal(r.temperature) > limit
        cargos.append(
            OnboardCargo(
                cargo_id=m.cargo_id,
                temp_upper_limit=limit,
                over_limit=over,
                adopted=adopted,
            )
        )
        # “受影响”= 读数实际作用（在车上、被采用）且按该货物上限构成越温
        if adopted and over:
            adopted_ids.append(m.cargo_id)
    return ReadingDetail(
        reading_id=r.reading_id,
        trip_id=r.trip_id,
        read_at=at,
        temperature=Decimal(r.temperature),
        created_at=ensure_utc(r.created_at),
        onboard_cargos=cargos,
        affected_cargo_ids=adopted_ids,
    )


@app.post(
    "/trips/{trip_id}/readings",
    response_model=ReadingOut,
    status_code=201,
    tags=["车载读数"],
    responses={200: {"model": ReadingReplay, "description": "同编号同内容重传"}},
)
def submit_reading(
    trip_id: str, payload: ReadingCreate, db: Session = Depends(get_db)
):
    """上报一条车载测温读数（允许晚到）。

    读数仅作用于读数时刻仍在车上的货物，各货物按自身温度上限判断越温；
    同刻货物有自有测温时自有值优先（车载原始读数仍可在车次读数接口查询）。
    同编号同内容重传幂等，异内容 409。
    """
    payload.read_at = ensure_utc(payload.read_at)
    result = trip_crud.submit_reading(db, trip_id, payload)
    r = result.reading
    body = ReadingOut(
        reading_id=r.reading_id,
        trip_id=r.trip_id,
        read_at=ensure_utc(r.read_at),
        temperature=Decimal(r.temperature),
        created_at=ensure_utc(r.created_at),
    )
    if result.replayed:
        return JSONResponse(
            status_code=200,
            content=ReadingReplay(
                replayed=True,
                reading=body,
                message="相同内容的车载读数已存在，重传被忽略（幂等）",
            ).model_dump(mode="json"),
        )
    return body


@app.get(
    "/trips/{trip_id}/readings",
    response_model=TripReadings,
    tags=["车载读数"],
)
def list_trip_readings(trip_id: str, db: Session = Depends(get_db)) -> TripReadings:
    """车次读数：原始读数 + 每条读数时刻在车上的货物及其是否被采用。"""
    if db.get(Trip, trip_id) is None:
        raise crud.ApiError(404, "trip_not_found", f"车次 {trip_id} 不存在")
    rows = list(
        db.scalars(
            select(TripReading)
            .where(TripReading.trip_id == trip_id)
            .order_by(TripReading.read_at, TripReading.reading_id)
        ).all()
    )
    return TripReadings(
        trip_id=trip_id,
        readings=[_reading_detail(db, r) for r in rows],
    )


@app.get(
    "/cargo/{cargo_id}/readings",
    response_model=CargoReadings,
    tags=["车载读数"],
)
def cargo_readings(cargo_id: str, db: Session = Depends(get_db)) -> CargoReadings:
    """货物的合并测温时间线：自有测温 + 各车次车载原始读数（标注是否被采用）。

    同刻存在货物自有测温时，车载读数 ``adopted=false``（被遮蔽但仍可查）；
    越温判定一律按该货物自身的温度上限。
    """
    cargo = db.get(Cargo, cargo_id)
    if cargo is None:
        raise crud.ApiError(404, "cargo_not_found", f"货物 {cargo_id} 不存在")
    cargo.started_at = ensure_utc(cargo.started_at)
    limit = Decimal(cargo.temp_upper_limit)

    events, points = crud.merged_timeline(db, cargo)
    own_times = {
        ensure_utc(e.occurred_at)
        for e in events
        if e.event_type.name == "measurement"
    }
    # 未过滤前的全部候选车载点（含被同刻自有测温遮蔽的）
    manifests = crud._load_manifests(db, cargo_id)
    items: list[tuple] = []
    for e in events:
        if e.event_type.name == "measurement":
            at = ensure_utc(e.occurred_at)
            items.append(
                (
                    at,
                    0,
                    CargoReadingItem(
                        at=at,
                        temperature=Decimal(e.temperature),
                        adopted=True,
                        source="own",
                        event_id=e.event_id,
                        over_limit=Decimal(e.temperature) > limit,
                    ),
                )
            )
    if manifests:
        trip_ids = {m.trip_id for m in manifests}
        readings = list(
            db.scalars(
                select(TripReading).where(TripReading.trip_id.in_(trip_ids))
            ).all()
        )
        for p in crud.onboard_points_for_cargo(
            manifests, readings, started_at=cargo.started_at
        ):
            items.append(
                (
                    p.at,
                    1,
                    CargoReadingItem(
                        at=p.at,
                        temperature=Decimal(p.temperature),
                        adopted=p.at not in own_times,
                        source="onboard",
                        trip_id=p.trip_id,
                        reading_id=p.reading_id,
                        over_limit=Decimal(p.temperature) > limit,
                    ),
                )
            )
    items.sort(key=lambda x: (x[0], x[1]))
    return CargoReadings(
        cargo_id=cargo_id,
        temp_upper_limit=limit,
        readings=[it[2] for it in items],
    )


# ---------------------------------------------------------------- 追责查询


def _source_ref(point) -> ReadingSourceRef:
    return ReadingSourceRef(
        source=point.source,
        event_id=point.event_id,
        trip_id=point.trip_id,
        reading_id=point.reading_id,
    )


@app.get(
    "/cargo/{cargo_id}/report",
    response_model=ChainReport,
    tags=["追责查询"],
)
def cargo_report(cargo_id: str, db: Session = Depends(get_db)) -> ChainReport:
    """按合并时间线（自有测温 + 车载读数）返回越温区间与各责任方片段。"""
    cargo = db.get(Cargo, cargo_id)
    if cargo is None:
        raise crud.ApiError(404, "cargo_not_found", f"货物 {cargo_id} 不存在")
    cargo.started_at = ensure_utc(cargo.started_at)

    events, points = crud.merged_timeline(db, cargo)
    result = chain.rebuild_chain(
        cargo.initial_holder,
        Decimal(cargo.temp_upper_limit),
        events,
        onboard_points=points,
    )

    excursions = [
        Excursion(
            start=ex.start,
            end=ex.end,
            is_open=ex.is_open,
            interval_id=ex.interval_id,
            start_source=_source_ref(ex.start_point),
            end_source=_source_ref(ex.end_point) if ex.end_point is not None else None,
            start_event_id=ex.start_event_id,
            end_event_id=ex.end_event_id,
            segments=[
                ResponsibilitySegment(
                    start=seg.start, end=seg.end, responsible_party=seg.party
                )
                for seg in ex.segments
            ],
        )
        for ex in result.excursions
    ]
    return ChainReport(
        cargo_id=cargo.cargo_id,
        temp_upper_limit=Decimal(cargo.temp_upper_limit),
        current_holder=result.current_holder,
        excursions=excursions,
    )


# ---------------------------------------------------------------- 责任确认


def _load_cargo_for_resp(db, cargo_id: str) -> Cargo:
    cargo = db.get(Cargo, cargo_id)
    if cargo is None:
        raise crud.ApiError(404, "cargo_not_found", f"货物 {cargo_id} 不存在")
    cargo.started_at = ensure_utc(cargo.started_at)
    return cargo


def _interval_schema(view) -> ResponsibilityInterval:
    """责任确认视图 -> 响应模型。"""
    return ResponsibilityInterval(
        cargo_id=view.cargo_id,
        interval_id=view.interval_id,
        start_event_id=view.start_event_id,
        end_event_id=view.end_event_id,
        start_source=ReadingSourceRef(
            source=view.start_source.source,
            event_id=view.start_source.event_id,
            trip_id=view.start_source.trip_id,
            reading_id=view.start_source.reading_id,
        ),
        end_source=ReadingSourceRef(
            source=view.end_source.source,
            event_id=view.end_source.event_id,
            trip_id=view.end_source.trip_id,
            reading_id=view.end_source.reading_id,
        ),
        start=view.start,
        end=view.end,
        snapshot_id=view.snapshot_id,
        segments=[
            StanceSegment(
                start=seg.start, end=seg.end, responsible_party=seg.responsible_party
            )
            for seg in view.segments
        ],
        latest_stances=[
            LatestStance(
                stance_id=sv.stance_id,
                responsible_party=sv.responsible_party,
                stance=sv.kind,
                reason=sv.reason,
                created_at=sv.created_at,
                segments=[
                    StanceSegment(
                        start=s.start,
                        end=s.end,
                        responsible_party=s.responsible_party,
                    )
                    for s in sv.segments
                ],
            )
            for sv in view.stances
        ],
        summary=view.summary,
    )


def _closed_interval_views(db, cargo: Cargo):
    """重建合并链并为每个已闭合区间装配责任确认视图（按开始时间升序）。"""
    result, closed = responsibility.rebuild_cargo(db, cargo)
    views = []
    for ex in sorted(closed.values(), key=lambda e: e.start):
        rows = list(
            db.scalars(
                select(Stance).where(
                    Stance.cargo_id == cargo.cargo_id,
                    Stance.interval_id == ex.interval_id,
                )
            ).all()
        )
        views.append(
            responsibility.build_interval_view(cargo.cargo_id, ex, rows)
        )
    return result, views


@app.get(
    "/cargo/{cargo_id}/responsibilities",
    response_model=ResponsibilityReport,
    tags=["责任确认"],
)
def list_responsibilities(cargo_id: str, db: Session = Depends(get_db)) -> ResponsibilityReport:
    """列出货物下所有**已闭合**越温区间的责任确认状态（开放区间不参与）。"""
    cargo = _load_cargo_for_resp(db, cargo_id)
    _, views = _closed_interval_views(db, cargo)
    return ResponsibilityReport(
        cargo_id=cargo_id, intervals=[_interval_schema(v) for v in views]
    )


@app.get(
    "/cargo/{cargo_id}/responsibilities/{interval_id}",
    response_model=ResponsibilityInterval,
    tags=["责任确认"],
    responses={404: {"description": "货物 / 区间不存在"}, 422: {"description": "区间开放"}},
)
def get_responsibility(
    interval_id: str, cargo_id: str, db: Session = Depends(get_db)
) -> ResponsibilityInterval:
    """按稳定区间标识查询单个已闭合区间的责任确认视图。

    区间标识：自有测温开启时为测温事件ID；车载读数开启时为
    ``trip:<车次ID>:reading:<读数ID>``。
    """
    cargo = _load_cargo_for_resp(db, cargo_id)

    _, ex = stance_crud._resolve_interval(db, cargo, interval_id)
    rows = list(
        db.scalars(
            select(Stance).where(
                Stance.cargo_id == cargo_id,
                Stance.interval_id == interval_id,
            )
        ).all()
    )
    return _interval_schema(
        responsibility.build_interval_view(cargo_id, ex, rows)
    )


@app.post(
    "/cargo/{cargo_id}/responsibilities/{interval_id}/stances",
    response_model=StanceOut,
    status_code=201,
    tags=["责任确认"],
    responses={
        200: {"model": StanceReplay, "description": "同编号同内容重传：返回原记录"},
    },
)
def submit_stance(
    interval_id: str,
    payload: StanceCreate,
    cargo_id: str,
    db: Session = Depends(get_db),
):
    """提交责任方表态（确认 / 异议）。

    与同货物的事件写入、车载读数写入、装卸时段登记共用货物行锁串行化；
    以合并时间线重建出的当前快照校验，过期快照、区间不存在或开放区间一律
    拒绝且不落库。
    """
    result = stance_crud.submit_stance(db, cargo_id, interval_id, payload)
    stance = result.stance
    body = StanceOut(
        stance_id=stance.stance_id,
        cargo_id=stance.cargo_id,
        interval_id=stance.interval_id,
        responsible_party=stance.responsible_party,
        stance=StanceKind(stance.kind),
        reason=stance.reason,
        snapshot_id=stance.snapshot_id,
        created_at=ensure_utc(stance.created_at),
    )
    if result.replayed:
        return JSONResponse(
            status_code=200,
            content=StanceReplay(
                replayed=True,
                stance=body,
                message="相同内容的表态已存在，重传被忽略（幂等；即使该表态快照已失效也返回原记录）",
            ).model_dump(mode="json"),
        )
    return body


@app.get(
    "/cargo/{cargo_id}/responsibilities/{interval_id}/stances",
    response_model=StanceHistory,
    tags=["责任确认"],
)
def list_stance_history(
    interval_id: str, cargo_id: str, db: Session = Depends(get_db)
) -> StanceHistory:
    """区间表态历史：历次表态全部保留（含已改口、已失效），按提交时间升序。

    区间当前不存在 / 仍开放时，历史仍可查，头部字段给出当前状态。
    追责与责任确认一律按**合并后的读数时间线**（自有测温 + 车载读数）重建。
    """
    cargo = _load_cargo_for_resp(db, cargo_id)
    rows = list(
        db.scalars(
            select(Stance)
            .where(
                Stance.cargo_id == cargo_id,
                Stance.interval_id == interval_id,
            )
            .order_by(Stance.created_at, Stance.stance_id)
        ).all()
    )

    # 当前区间状态（不因此报 404/422：历史要始终可查）
    result, closed = responsibility.rebuild_cargo(db, cargo)
    ex = closed.get(interval_id)
    open_ex = next(
        (e for e in result.excursions if e.interval_id == interval_id), None
    )
    interval_exists = ex is not None or open_ex is not None
    is_open = ex is None and open_ex is not None
    current_snapshot = responsibility.snapshot_id(ex) if ex is not None else None

    # 各责任方“最新有效表态”（行已按 (created_at, stance_id) 升序），
    # 用于把更早的有效表态标记为已改口
    latest_current: dict[str, Stance] = {}
    if ex is not None:
        for s in rows:
            if responsibility.is_stance_current(s, ex):
                latest_current[s.responsible_party] = s

    items = [
        StanceHistoryItem(
            stance_id=s.stance_id,
            cargo_id=s.cargo_id,
            interval_id=s.interval_id,
            responsible_party=s.responsible_party,
            stance=StanceKind(s.kind),
            reason=s.reason,
            snapshot_id=s.snapshot_id,
            created_at=ensure_utc(s.created_at),
            is_current=(ex is not None and responsibility.is_stance_current(s, ex)),
            # 改口只在“当前仍有效”的行之间比较：已因快照变化失效的行不算被改口
            superseded=(
                ex is not None
                and responsibility.is_stance_current(s, ex)
                and (
                    (latest := latest_current.get(s.responsible_party)) is not None
                    and latest.stance_id != s.stance_id
                )
            ),
        )
        for s in rows
    ]
    return StanceHistory(
        cargo_id=cargo_id,
        interval_id=interval_id,
        interval_exists=interval_exists,
        is_open=is_open,
        current_snapshot_id=current_snapshot,
        history=items,
    )


@app.get("/health", tags=["运维"])
def health() -> dict:
    return {"status": "ok"}
