"""Pydantic 请求 / 响应模型。"""
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models import EventType, StanceKind


class CargoCreate(BaseModel):
    cargo_id: str = Field(..., min_length=1, max_length=64, description="货物编号")
    started_at: datetime = Field(..., description="起始时间（ISO 8601，建议带时区）")
    temp_upper_limit: Decimal = Field(..., description="温度上限（摄氏度）")
    initial_holder: str = Field(
        ..., min_length=1, max_length=128, description="起始保管方"
    )


class EventCreate(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=64, description="事件ID")
    cargo_id: str = Field(..., min_length=1, max_length=64, description="货物编号")
    occurred_at: datetime = Field(..., description="发生时间（ISO 8601，建议带时区）")

    # 测温
    temperature: Decimal | None = Field(
        default=None, description="测温温度；测温事件必填且事件只能含此字段"
    )
    # 交接
    giver: str | None = Field(default=None, max_length=128, description="交出方")
    receiver: str | None = Field(default=None, max_length=128, description="接收方")

    def event_type(self) -> EventType:
        is_measurement = self.temperature is not None
        is_handover = self.giver is not None or self.receiver is not None
        if is_measurement and is_handover:
            raise ValueError("事件不能同时包含温度与交接双方")
        if not is_measurement and not is_handover:
            raise ValueError("事件必须包含 temperature，或同时包含 giver 与 receiver")
        if is_handover and not (self.giver and self.receiver):
            raise ValueError("交接事件必须同时提供交出方与接收方")
        if is_handover and self.giver == self.receiver:
            raise ValueError("交出方与接收方不能相同")
        return EventType.measurement if is_measurement else EventType.handover


class CargoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    cargo_id: str
    started_at: datetime
    temp_upper_limit: Decimal
    initial_holder: str
    created_at: datetime


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: str
    cargo_id: str
    event_type: EventType
    occurred_at: datetime
    temperature: Decimal | None
    giver: str | None
    receiver: str | None


class ResponsibilitySegment(BaseModel):
    """越温区间内按交接时刻切分出的责任片段。"""

    start: datetime = Field(..., description="片段起点（含）")
    end: datetime | None = Field(
        ..., description="片段终点（不含）；null 表示区间未闭合（未结束）"
    )
    responsible_party: str = Field(..., description="该片段责任方（保管方）")


class Excursion(BaseModel):
    """一次越温区间及其责任拆分。"""

    start: datetime = Field(..., description="越温开始（首次超限测温时刻，含）")
    end: datetime | None = Field(
        ..., description="越温结束（首次不超限测温时刻，不含）；null=未闭合"
    )
    is_open: bool = Field(..., description="是否尚未闭合（未结束）")
    segments: list[ResponsibilitySegment] = Field(..., description="责任方片段")
    interval_id: str = Field(
        ..., description="区间稳定标识：自有测温事件ID 或 trip:<车次>:reading:<读数>"
    )
    start_source: "ReadingSourceRef" = Field(
        ..., description="开启区间的读数来源（货物自有 / 车载）"
    )
    end_source: "ReadingSourceRef | None" = Field(
        None, description="闭合区间的读数来源；开放区间为 null"
    )
    start_event_id: str | None = Field(
        None, description="开启区间的货物自有测温事件ID；车载读数开启时为 null"
    )
    end_event_id: str | None = Field(
        None, description="闭合区间的货物自有测温事件ID；车载闭合或开放时为 null"
    )


# ----------------------------------------------------------- 车次 / 车载读数


class TripCreate(BaseModel):
    trip_id: str = Field(..., min_length=1, max_length=64, description="车次编号")
    vehicle_no: str | None = Field(
        default=None, max_length=64, description="车牌号等车辆标识（可选）"
    )


class TripOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    trip_id: str
    vehicle_no: str | None
    created_at: datetime


class ManifestItem(BaseModel):
    """一票货物在车次上的装卸时段（左闭右开 [loaded_at, unloaded_at)）。"""

    cargo_id: str = Field(..., min_length=1, max_length=64, description="货物编号")
    loaded_at: datetime = Field(..., description="装车时刻（含）")
    unloaded_at: datetime = Field(..., description="卸车时刻（不含），必须晚于装车时刻")


class ManifestRegister(BaseModel):
    items: list[ManifestItem] = Field(
        ..., min_length=1, description="本批登记的装卸时段；整体成功或整体拒绝"
    )


class ManifestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    trip_id: str
    cargo_id: str
    loaded_at: datetime
    unloaded_at: datetime


class ManifestResult(BaseModel):
    trip_id: str
    manifests: list[ManifestOut]
    created: int = Field(..., description="本次新登记条数")
    replayed: int = Field(..., description="同车次/货物/起点已存在且终点一致的条数")
    all_replayed: bool = Field(
        ..., description="全部条目均为幂等重传（HTTP 200）时为 true"
    )


class ReadingCreate(BaseModel):
    reading_id: str = Field(..., min_length=1, max_length=64, description="车载读数编号")
    read_at: datetime = Field(..., description="读数时刻（ISO 8601，建议带时区；允许晚到）")
    temperature: Decimal = Field(..., description="车载测温温度（摄氏度）")


class ReadingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    reading_id: str
    trip_id: str
    read_at: datetime
    temperature: Decimal
    created_at: datetime


class ReadingReplay(BaseModel):
    replayed: bool = True
    reading: ReadingOut
    message: str


class OnboardCargo(BaseModel):
    """读数时刻仍在车上的一票货物及其采用情况。"""

    cargo_id: str
    temp_upper_limit: Decimal
    over_limit: bool = Field(..., description="按该货物自身温度上限是否越温")
    adopted: bool = Field(
        ...,
        description="是否被该货物的合并时间线采用；同刻存在货物自有测温时为 false",
    )


class ReadingDetail(ReadingOut):
    onboard_cargos: list[OnboardCargo] = Field(
        ..., description="读数时刻在车上的货物（按货物编号升序）"
    )
    affected_cargo_ids: list[str] = Field(
        ...,
        description="读数对其实际生效（在车上且被采用）并按自身上限判为越温的货物编号",
    )


class ReadingSourceRef(BaseModel):
    """越温区间开启 / 闭合读数的来源描述。"""

    source: str = Field(..., description="own=货物自有测温；onboard=车载读数")
    event_id: str | None = Field(None, description="自有测温事件ID")
    trip_id: str | None = Field(None, description="车载读数所属车次")
    reading_id: str | None = Field(None, description="车载读数编号")


class CargoReadingItem(BaseModel):
    """货物合并测温时间线上的一个读数。"""

    at: datetime = Field(..., description="读数时刻")
    temperature: Decimal
    adopted: bool = Field(
        ...,
        description="是否为追责所采用的读数：同刻自有测温优先，被遮蔽的车载读数为 false",
    )
    source: str = Field(..., description="own=货物自有测温；onboard=车载读数")
    event_id: str | None = Field(None, description="自有测温事件ID")
    trip_id: str | None = Field(None, description="车载读数所属车次")
    reading_id: str | None = Field(None, description="车载读数编号")
    over_limit: bool = Field(..., description="按该货物自身温度上限是否越温")


class CargoReadings(BaseModel):
    cargo_id: str
    temp_upper_limit: Decimal
    readings: list[CargoReadingItem] = Field(
        ..., description="合并后的测温时间线（含被遮蔽的车载原始读数），按时刻升序"
    )


class TripReadings(BaseModel):
    trip_id: str
    readings: list[ReadingDetail] = Field(..., description="车载读数（按读数时刻升序）")


class ChainReport(BaseModel):
    cargo_id: str
    temp_upper_limit: Decimal
    current_holder: str = Field(..., description="按事件重建出的当前保管方")
    excursions: list[Excursion]


class IdempotentReplay(BaseModel):
    """同 ID 同内容重传时的响应：不重复写入。"""

    replayed: bool = True
    event_id: str
    message: str


# ----------------------------------------------------- 责任确认


class StanceCreate(BaseModel):
    """责任方表态提交。

    区间由路径上的区间标识（开启测温事件ID）定位；
    货物编号同样在路径上，请求体不再重复。
    """

    snapshot_id: str = Field(
        ...,
        min_length=8,
        max_length=71,
        description="提交前查询到的当前快照标识；与服务端当前快照不一致则拒绝（过期）",
    )
    responsible_party: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="表态责任方，必须出现在当前区间责任片段中",
    )
    stance: str = Field(
        ...,
        description="表态：confirm=确认担责；dispute=异议（必须同时提供 reason）",
    )
    reason: str | None = Field(
        default=None,
        max_length=512,
        description="异议理由；stance=dispute 时必填且非空，confirm 时必须为空",
    )
    stance_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="表态编号，客户端指定；同编号同内容重传返回原记录",
    )


class StanceSegment(BaseModel):
    """责任方在区间内承担的片段（一个责任方可能跨交接承担多段）。"""

    start: datetime
    end: datetime
    responsible_party: str


class LatestStance(BaseModel):
    """某责任方的最新一条有效表态。"""

    stance_id: str
    responsible_party: str
    stance: StanceKind
    reason: str | None
    created_at: datetime
    segments: list[StanceSegment] = Field(
        ..., description="该责任方在当前区间内承担的全部片段"
    )


class ResponsibilityInterval(BaseModel):
    """一个已闭合越温区间的责任确认视图。"""

    cargo_id: str
    interval_id: str = Field(
        ...,
        description="区间稳定标识：自有测温事件ID，或 trip:<车次>:reading:<读数>",
    )
    start_event_id: str | None = Field(
        None, description="开启区间的自有测温事件ID；车载读数开启时为 null"
    )
    end_event_id: str | None = Field(
        None, description="结束区间的自有测温事件ID；车载读数闭合时为 null"
    )
    start_source: ReadingSourceRef = Field(..., description="开启区间的读数来源")
    end_source: ReadingSourceRef = Field(..., description="闭合区间的读数来源")
    start: datetime
    end: datetime
    snapshot_id: str = Field(..., description="当前快照标识（区间起止 + 责任片段）")
    segments: list[StanceSegment] = Field(..., description="当前责任片段（按时间）")
    latest_stances: list[LatestStance] = Field(
        ..., description="各责任方最新一条有效表态（按片段顺序）"
    )
    summary: str = Field(
        ...,
        description="汇总状态：all_confirmed 全确认 / has_dispute 有异议 / pending 待确认",
    )


class ResponsibilityReport(BaseModel):
    """货物下全部已闭合越温区间的责任确认汇总（开放区间不参与确认）。"""

    cargo_id: str
    intervals: list[ResponsibilityInterval]


class StanceOut(BaseModel):
    """表态记录。"""

    model_config = ConfigDict(from_attributes=True)

    stance_id: str
    cargo_id: str
    interval_id: str = Field(
        ...,
        description="区间稳定标识：自有测温事件ID，或 trip:<车次>:reading:<读数>",
    )
    responsible_party: str
    stance: StanceKind
    reason: str | None
    snapshot_id: str
    created_at: datetime


class StanceReplay(BaseModel):
    """同编号同内容重传时的响应：返回原记录（即使其快照已失效）。"""

    replayed: bool = True
    stance: StanceOut
    message: str


class StanceHistoryItem(StanceOut):
    """表态历史中的一条：附加该表态对当前快照是否仍有效。"""

    is_current: bool = Field(
        ...,
        description="对当前快照是否仍有效：区间/片段变化后为 false，但历史仍保留可查",
    )
    superseded: bool = Field(
        ..., description="是否已被同责任方更新的表态改口（新编号）"
    )


class StanceHistory(BaseModel):
    cargo_id: str
    interval_id: str
    interval_exists: bool = Field(..., description="区间当前是否存在")
    is_open: bool = Field(..., description="区间当前是否仍开放")
    current_snapshot_id: str | None = Field(
        ..., description="当前快照标识；区间不存在/开放时为 null"
    )
    history: list[StanceHistoryItem] = Field(
        ..., description="历次表态（含已改口、已失效），按提交时间升序"
    )
