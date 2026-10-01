"""SQLAlchemy ORM 模型：货物、冷链事件（测温 / 交接）、车次与车载读数。"""
import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EventType(str, enum.Enum):
    """事件类型。成员名与 PG enum 字面量保持一致。"""

    measurement = "measurement"  # 测温
    handover = "handover"        # 交接


class StanceKind(str, enum.Enum):
    """责任方表态类型。"""

    confirm = "confirm"  # 确认担责
    dispute = "dispute"  # 异议（须附理由）


class Cargo(Base):
    __tablename__ = "cargo"

    cargo_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    temp_upper_limit: Mapped[Decimal] = mapped_column(
        Numeric(10, 3), nullable=False
    )
    initial_holder: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class Event(Base):
    """一条货物**自有**测温或交接事件。

    测温：temperature 有值，giver/receiver 为空。
    交接：giver/receiver 有值，temperature 为空。

    车载读数不写入本表（见 ``TripReading``），因此货物事件列表只含自有事件。
    """

    __tablename__ = "event"
    __table_args__ = (
        CheckConstraint(
            "(event_type = 'measurement' AND temperature IS NOT NULL "
            "AND giver IS NULL AND receiver IS NULL) "
            "OR (event_type = 'handover' AND temperature IS NULL "
            "AND giver IS NOT NULL AND receiver IS NOT NULL AND giver <> receiver)",
            name="chk_event_shape",
        ),
        # 同一货物同一时刻只能有一笔测温（跨类型不互斥：测温与交接可同刻发生）
        Index(
            "ux_event_cargo_time_measurement",
            "cargo_id",
            "occurred_at",
            unique=True,
            postgresql_where=text("event_type = 'measurement'::event_type"),
            sqlite_where=text("event_type = 'measurement'"),
        ),
        # 同一货物同一时刻只能有一笔交接
        Index(
            "ux_event_cargo_time_handover",
            "cargo_id",
            "occurred_at",
            unique=True,
            postgresql_where=text("event_type = 'handover'::event_type"),
            sqlite_where=text("event_type = 'handover'"),
        ),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cargo_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cargo.cargo_id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_type: Mapped[EventType] = mapped_column(
        Enum(EventType, name="event_type", create_constraint=True),
        nullable=False,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    temperature: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 3), nullable=True
    )
    giver: Mapped[str | None] = mapped_column(String(128), nullable=True)
    receiver: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class Trip(Base):
    """冷链车次。同一车次承载多票货物，途中上报车载测温读数。"""

    __tablename__ = "trip"

    trip_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    vehicle_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class TripManifest(Base):
    """某票货物在某车次上的装卸时段：左闭右开 ``[loaded_at, unloaded_at)``。

    车载读数仅在 ``loaded_at <= read_at < unloaded_at`` 时作用于该票货物。
    同一货物在任意车次上的装卸时段互不相交（首尾相接允许），由写入事务
    在货物行锁下应用层校验（见 app/trip_crud.py）。
    """

    __tablename__ = "trip_manifest"
    __table_args__ = (
        CheckConstraint(
            "loaded_at < unloaded_at", name="chk_manifest_window"
        ),
        UniqueConstraint(
            "trip_id", "cargo_id", "loaded_at", name="ux_manifest_trip_cargo_loaded"
        ),
        # 跨车次兜底：同一货物在所有车次上的任一装车时刻只能登记一次，
        # 防止无行级锁的引擎并发写入“同时属于两个车次”的同起点窗口
        Index("ux_manifest_cargo_loaded", "cargo_id", "loaded_at", unique=True),
        Index("ix_manifest_cargo", "cargo_id", "loaded_at", "unloaded_at"),
    )

    trip_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("trip.trip_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    cargo_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cargo.cargo_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    loaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    unloaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class TripReading(Base):
    """车载测温读数（原始记录）。

    读数允许晚到（乱序写入）；按装卸时段作用于读数时刻仍在车上的各票货物，
    各货物按**各自温度上限**判断越温。同刻若货物有自有测温则自有值优先，
    本原始读数仍保留可查。读数不改变任何货物的保管方。
    """

    __tablename__ = "trip_reading"
    __table_args__ = (
        # 同一车载设备同一时刻至多一笔原始读数（同时充当查询索引）
        Index(
            "ux_reading_trip_time",
            "trip_id",
            "read_at",
            unique=True,
        ),
    )

    reading_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trip_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("trip.trip_id", ondelete="RESTRICT"),
        nullable=False,
    )
    read_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    temperature: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class Stance(Base):
    """责任方对一个已闭合越温区间的一次表态（确认 / 异议）。

    表态是只追加（append-only）的：改口产生新编号的新记录，历次表态永久保留；
    晚到事件 / 车载读数重建后若区间边界或责任片段变化，旧行不删除、不修改，
    其是否仍“有效”由查询时与当前快照比对得出（见 app/responsibility.py）。

    区间标识 ``interval_id`` 稳定可重建：
    * 自有测温开启：开启测温事件ID；
    * 车载读数开启：``trip:<车次ID>:reading:<读数ID>``。
    因此不再对外键做硬约束（标识可能指向车载读数而非自有事件）。
    """

    __tablename__ = "responsibility_stance"
    __table_args__ = (
        CheckConstraint(
            "(kind = 'confirm' AND reason IS NULL) "
            "OR (kind = 'dispute' AND reason IS NOT NULL AND char_length(reason) > 0)",
            name="chk_stance_shape",
        ),
        Index("ix_stance_cargo_interval", "cargo_id", "interval_id"),
    )

    stance_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cargo_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cargo.cargo_id", ondelete="RESTRICT"),
        nullable=False,
    )
    # 区间稳定标识：自有测温事件ID，或 trip:<trip_id>:reading:<reading_id>
    interval_id: Mapped[str] = mapped_column(String(160), nullable=False)
    responsible_party: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[StanceKind] = mapped_column(
        Enum(StanceKind, name="stance_kind", create_constraint=True),
        nullable=False,
    )
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 提交时校验通过的快照标识；区间/片段变化后该标识与当前快照不再一致
    snapshot_id: Mapped[str] = mapped_column(String(71), nullable=False)
    # 表态时该责任方承担的片段坐标（规范化 JSON：[[start,end,party],...]）。
    # 晚到记录重建后据此逐片段判定表态是否仍有效（未变化片段继续有效）。
    attested_segments: Mapped[str] = mapped_column(String(2000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
