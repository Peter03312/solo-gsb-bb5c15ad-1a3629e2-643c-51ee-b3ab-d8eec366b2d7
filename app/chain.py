"""保管链重建与越温责任拆分（纯函数，便于单测）。

读数来源
--------
合并后的时间线包含两类“测温点”：

* **货物自有测温**（``Event(measurement)``）；
* **车载读数**（``ReadingPoint``）：仅当其读数时刻货物仍在该车次上
  （``loaded_at <= read_at < unloaded_at``，左闭右开）。

同一时刻若存在货物自有测温，则该时刻的车载读数被**遮蔽**（自有值优先采用，
原始车载读数仍可在车次接口查询）。交接永远只由货物自有 handover 事件给出，
车载读数不改变保管方。

规则
----
* 时间线按时刻排序；同一时刻测温点先于交接处理
  （“交接时刻起归接收方”——交接在该时刻已生效）。
* 高于上限的测温开启越温区间；越温期间首次不超限（<= 上限）的测温关闭区间。
* 区间跨交接时按交接时刻拆分：[区间起, 交接时刻) 归交出方，
  [交接时刻, ...) 归接收方；交接与区间起点同刻时直接由接收方担责。
* 区间内重复超限测温不影响区间；区间外不超限测温无影响。
* 不产生零长度片段。
"""
from dataclasses import dataclass
from decimal import Decimal

from app.models import Event, EventType

# 车载读数开启的区间稳定标识前缀
ONBOARD_INTERVAL_PREFIX = "trip:"


def onboard_interval_id(trip_id: str, reading_id: str) -> str:
    """车载读数开启的越温区间稳定标识。"""
    return f"{ONBOARD_INTERVAL_PREFIX}{trip_id}:reading:{reading_id}"


@dataclass(frozen=True)
class ReadingPoint:
    """合并时间线中的一个测温点（自有测温或车载读数）。"""

    at: object                 # datetime
    temperature: Decimal
    source: str                # "own" | "onboard"
    event_id: str | None = None       # source == "own" 时的自有事件ID
    trip_id: str | None = None        # source == "onboard" 时的车次ID
    reading_id: str | None = None     # source == "onboard" 时的读数ID

    @property
    def interval_id(self) -> str | None:
        """该测温点开启越温区间时对应的稳定区间标识。"""
        if self.source == "own":
            return self.event_id
        return onboard_interval_id(self.trip_id, self.reading_id)


@dataclass(frozen=True)
class Segment:
    start: object          # datetime，含
    end: object | None     # datetime，不含；None 表示未闭合
    party: str


@dataclass(frozen=True)
class ExcursionResult:
    start: object
    end: object | None
    segments: tuple[Segment, ...]
    start_event_id: str | None = None   # 开启区间的自有测温事件ID（车载开启为 None）
    end_event_id: str | None = None     # 闭合区间的自有测温事件ID（车载闭合/开放为 None）
    start_point: ReadingPoint | None = None
    end_point: ReadingPoint | None = None

    @property
    def is_open(self) -> bool:
        return self.end is None

    @property
    def interval_id(self) -> str | None:
        """稳定区间标识：自有测温事件ID，或 trip:<车次>:reading:<读数>。"""
        if self.start_point is not None:
            return self.start_point.interval_id
        return self.start_event_id  # 兼容旧式（无 point 的）构造


@dataclass(frozen=True)
class ChainResult:
    current_holder: str
    excursions: tuple[ExcursionResult, ...]


# 同刻排序：自有测温(0) < 车载读数(1) < 交接(2)——交接在测温“之后”生效；
# 自有测温同时遮蔽（优先采用）同刻车载读数。
_TYPE_ORDER = {EventType.measurement: 0, EventType.handover: 2}


def _item_key(item):
    if isinstance(item, ReadingPoint):
        return (item.at, 0 if item.source == "own" else 1, item.interval_id or "")
    return (item.occurred_at, _TYPE_ORDER[item.event_type], item.event_id)


def _sorted_items(items) -> list:
    return sorted(items, key=_item_key)


def _sorted_events(events: list[Event]) -> list[Event]:
    # 保留给写入校验 / holder_at 使用：同一时刻 measurement 在 handover 之前
    return sorted(
        events,
        key=lambda e: (e.occurred_at, _TYPE_ORDER[e.event_type], e.event_id),
    )


def _reading_points(events: list[Event]) -> list[ReadingPoint]:
    return [
        ReadingPoint(
            at=e.occurred_at,
            temperature=Decimal(e.temperature),
            source="own",
            event_id=e.event_id,
        )
        for e in events
        if e.event_type is EventType.measurement
    ]


def _dedup_points(points: list[ReadingPoint]) -> list[ReadingPoint]:
    """同刻去重：存在自有测温则遮蔽全部同刻车载读数；否则车载读数同刻取首个。"""
    by_at: dict = {}
    own_at: set = set()
    for p in sorted(points, key=lambda p: (p.at, 0 if p.source == "own" else 1)):
        if p.source == "own":
            own_at.add(p.at)
            by_at[p.at] = p  # 自有测温同刻唯一（DB 部分唯一索引兜底）
        elif p.at not in own_at and p.at not in by_at:
            by_at[p.at] = p
    return list(by_at.values())


def rebuild_chain(
    initial_holder: str,
    temp_upper_limit: Decimal,
    events: list[Event],
    onboard_points: list[ReadingPoint] | None = None,
) -> ChainResult:
    """按合并时间线重建当前保管方与全部越温区间（含责任片段）。

    ``onboard_points`` 为已按装卸时段过滤后的车载测温点；为 None 时退化为
    仅依据货物自有事件重建（旧行为）。
    """
    points = _reading_points(events)
    if onboard_points:
        points.extend(onboard_points)
    points = _dedup_points(points)

    limit = Decimal(temp_upper_limit)
    holder = initial_holder

    open_start = None                  # 当前越温区间起点
    opener: ReadingPoint | None = None
    segments: list[Segment] = []       # 当前区间已切分出的片段（最后一段开放）
    excursions: list[ExcursionResult] = []

    items = _sorted_items(points + [e for e in events if e.event_type is EventType.handover])
    for item in items:
        if isinstance(item, ReadingPoint):
            over = Decimal(item.temperature) > limit
            if open_start is None and over:
                # 开启越温：当前保管方自测温时刻起担责
                open_start = item.at
                opener = item
                segments = [Segment(item.at, None, holder)]
            elif open_start is not None and not over:
                # 首次不超限测温 -> 在该时刻闭合
                last = segments[-1]
                if last.start < item.at:
                    segments[-1] = Segment(last.start, item.at, last.party)
                else:
                    segments.pop()  # 理论不会发生（同刻测温已去重）
                excursions.append(
                    ExcursionResult(
                        open_start,
                        item.at,
                        tuple(segments),
                        start_event_id=(
                            opener.event_id if opener.source == "own" else None
                        ),
                        end_event_id=(
                            item.event_id if item.source == "own" else None
                        ),
                        start_point=opener,
                        end_point=item,
                    )
                )
                open_start = None
                opener = None
                segments = []
        else:  # handover（永远是货物自有事件）
            if open_start is not None:
                # 跨交接拆分责任片段
                prev = segments[-1]
                if item.occurred_at > prev.start:
                    # 交出方承担 [prev.start, 交接时刻)，接收方自交接时刻起
                    segments[-1] = Segment(prev.start, item.occurred_at, prev.party)
                    segments.append(Segment(item.occurred_at, None, item.receiver))
                else:
                    # 交接与区间起点（同刻的开启测温）同一时刻：
                    # 交出方片段为零长，直接由接收方自该时刻起担责
                    segments[-1] = Segment(item.occurred_at, None, item.receiver)
            holder = item.receiver

    if open_start is not None:
        # 未闭合区间：最后片段保持开放终点，标记“未结束”
        excursions.append(
            ExcursionResult(
                open_start,
                None,
                tuple(segments),
                start_event_id=(
                    opener.event_id if opener.source == "own" else None
                ),
                start_point=opener,
            )
        )

    return ChainResult(current_holder=holder, excursions=tuple(excursions))


def holder_at(initial_holder: str, events: list[Event], at) -> str:
    """返回 at 时刻（该时刻的交接已生效后）的保管方。

    用于写入校验：交接的交出方必须等于“交接时刻的保管方”。
    仅由货物自有交接事件决定，车载读数不参与。
    """
    holder = initial_holder
    for e in _sorted_events(events):
        if e.occurred_at <= at and e.event_type is EventType.handover:
            holder = e.receiver
    return holder
