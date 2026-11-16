"""电表结账：带修订号的累计读数、换表衔接、窑次分摊与更正差额。

金额（电量）一律以“毫千瓦时”（mWh，1 kWh = 1000）整数记账，
保证“分摊 + 闲置 = 表差”在整数域内严格成立，不产生浮点尾差。
"""
from sqlalchemy import (BigInteger, Boolean, Double, ForeignKey, Index, JSON,
                        String, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from src.config.database import Base


class AccountingState(Base):
    """单行状态表：锁住结账与改数的先后顺序，并记录全局数据修订号。"""
    __tablename__ = 'accounting_state'
    id: Mapped[int] = mapped_column(primary_key=True)
    data_revision: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[float] = mapped_column(Double, default=0.0)


class Meter(Base):
    __tablename__ = 'meters'
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(160))
    group_code: Mapped[str] = mapped_column(String(80), index=True)
    unit: Mapped[str] = mapped_column(String(20), default='kWh')
    # 期望抄表间隔，用于识别“缺读数”造成的长空档。
    reading_interval_seconds: Mapped[int] = mapped_column(BigInteger, default=86400)
    created_at: Mapped[float] = mapped_column(Double)
    readings: Mapped[list['MeterReading']] = relationship(
        back_populates='meter', cascade='all, delete-orphan')


class MeterReading(Base):
    __tablename__ = 'meter_readings'
    __table_args__ = (
        UniqueConstraint('meter_id', 'reading_at', 'revision', name='uq_reading_revision'),
        Index('ix_reading_point', 'meter_id', 'reading_at', 'revision'),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey('meters.id'), index=True)
    reading_at: Mapped[float] = mapped_column(Double)
    reading_value_milli: Mapped[int] = mapped_column(BigInteger)
    revision: Mapped[int] = mapped_column(BigInteger, default=1)
    source: Mapped[str] = mapped_column(String(60), default='upstream')
    received_at: Mapped[float] = mapped_column(Double)
    created_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    meter: Mapped[Meter] = relationship(back_populates='readings')


class MeterLink(Base):
    """换表/表计清零的衔接登记：旧表止码、新表起码与发生时刻。"""
    __tablename__ = 'meter_links'
    __table_args__ = (
        UniqueConstraint('old_meter_id', 'new_meter_id', 'at', name='uq_meter_link'),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    group_code: Mapped[str] = mapped_column(String(80), index=True)
    old_meter_id: Mapped[int] = mapped_column(ForeignKey('meters.id'))
    new_meter_id: Mapped[int] = mapped_column(ForeignKey('meters.id'))
    at: Mapped[float] = mapped_column(Double)
    old_final_milli: Mapped[int] = mapped_column(BigInteger)
    new_initial_milli: Mapped[int] = mapped_column(BigInteger)
    note: Mapped[str] = mapped_column(String(300), default='')
    created_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    created_at: Mapped[float] = mapped_column(Double)


class KilnFiring(Base):
    """窑次：起止时间、装窑量与合格件数，可被上游修订。"""
    __tablename__ = 'kiln_firings'
    id: Mapped[int] = mapped_column(primary_key=True)
    batch: Mapped[str] = mapped_column(String(80), unique=True)
    group_code: Mapped[str] = mapped_column(String(80), index=True)
    kiln_code: Mapped[str] = mapped_column(String(60), index=True)
    start_at: Mapped[float] = mapped_column(Double, index=True)
    end_at: Mapped[float] = mapped_column(Double, index=True)
    loaded: Mapped[int] = mapped_column(BigInteger)
    passed: Mapped[int] = mapped_column(BigInteger)
    revision: Mapped[int] = mapped_column(BigInteger, default=1)
    source: Mapped[str] = mapped_column(String(60), default='upstream')
    created_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    created_at: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[float] = mapped_column(Double)


class Settlement(Base):
    """结账单。draft/stale = 未结账；closing = 记账中断可恢复；closed = 已结账。"""
    __tablename__ = 'settlements'
    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(160))
    group_code: Mapped[str] = mapped_column(String(80), index=True)
    start_at: Mapped[float] = mapped_column(Double)
    end_at: Mapped[float] = mapped_column(Double)
    status: Mapped[str] = mapped_column(String(20), default='draft', index=True)
    revision: Mapped[int] = mapped_column(BigInteger, default=1)  # 分摊计算版本
    input_revision: Mapped[int] = mapped_column(BigInteger, default=0)
    fingerprint: Mapped[str] = mapped_column(String(64), default='')
    algorithm_version: Mapped[int] = mapped_column(BigInteger, default=1)
    delta_total_milli: Mapped[int] = mapped_column(BigInteger, default=0)
    allocated_total_milli: Mapped[int] = mapped_column(BigInteger, default=0)
    idle_total_milli: Mapped[int] = mapped_column(BigInteger, default=0)
    unresolved_total_milli: Mapped[int] = mapped_column(BigInteger, default=0)
    incomplete_readings: Mapped[bool] = mapped_column(Boolean, default=False)
    pending_count: Mapped[int] = mapped_column(BigInteger, default=0)
    stale_reason: Mapped[str] = mapped_column(String(500), default='')
    stale_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    recalc_count: Mapped[int] = mapped_column(BigInteger, default=0)
    recomputed_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    last_recalc_start: Mapped[float | None] = mapped_column(Double, nullable=True)
    last_recalc_end: Mapped[float | None] = mapped_column(Double, nullable=True)
    last_recalc_reason: Mapped[str] = mapped_column(String(500), default='')
    posting_step: Mapped[str] = mapped_column(String(40), default='')
    posting_progress: Mapped[dict] = mapped_column(JSON, default=dict)
    posting_started_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    close_request_id: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    created_at: Mapped[float] = mapped_column(Double)
    closed_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    closed_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    postings: Mapped[list['Posting']] = relationship(
        back_populates='settlement', cascade='all, delete-orphan')
    rows: Mapped[list['SettlementRow']] = relationship(
        back_populates='settlement', cascade='all, delete-orphan')
    slices: Mapped[list['SettlementSlice']] = relationship(
        back_populates='settlement', cascade='all, delete-orphan')
    issues: Mapped[list['SettlementIssue']] = relationship(
        back_populates='settlement', cascade='all, delete-orphan')


class SettlementSlice(Base):
    """按窑次起止切开的时段。kind=idle 即无窑运行、单列的闲置电量。"""
    __tablename__ = 'settlement_slices'
    id: Mapped[int] = mapped_column(primary_key=True)
    settlement_id: Mapped[int] = mapped_column(ForeignKey('settlements.id'), index=True)
    seq: Mapped[int] = mapped_column(BigInteger)
    start_at: Mapped[float] = mapped_column(Double)
    end_at: Mapped[float] = mapped_column(Double)
    delta_milli: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(20))  # allocated / idle / unresolved
    firing_count: Mapped[int] = mapped_column(BigInteger, default=0)
    breakdown: Mapped[list] = mapped_column(JSON, default=list)  # [[窑次, mWh], ...]
    settlement: Mapped[Settlement] = relationship(back_populates='slices')


class SettlementRow(Base):
    """逐窑分摊快照。posted 标志用于中断恢复与防重复入账。"""
    __tablename__ = 'settlement_rows'
    __table_args__ = (
        UniqueConstraint('settlement_id', 'firing_id', name='uq_settlement_firing'),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    settlement_id: Mapped[int] = mapped_column(ForeignKey('settlements.id'), index=True)
    firing_id: Mapped[int] = mapped_column(ForeignKey('kiln_firings.id'))
    batch: Mapped[str] = mapped_column(String(80))
    kiln_code: Mapped[str] = mapped_column(String(60))
    start_at: Mapped[float] = mapped_column(Double)
    end_at: Mapped[float] = mapped_column(Double)
    loaded: Mapped[int] = mapped_column(BigInteger)
    passed: Mapped[int] = mapped_column(BigInteger)
    overlap_seconds: Mapped[float] = mapped_column(Double)
    allocated_milli: Mapped[int] = mapped_column(BigInteger)
    posted: Mapped[bool] = mapped_column(Boolean, default=False)
    settlement: Mapped[Settlement] = relationship(back_populates='rows')


class SettlementIssue(Base):
    __tablename__ = 'settlement_issues'
    id: Mapped[int] = mapped_column(primary_key=True)
    settlement_id: Mapped[int] = mapped_column(ForeignKey('settlements.id'), index=True)
    code: Mapped[str] = mapped_column(String(50))
    message: Mapped[str] = mapped_column(String(500))
    start_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    end_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    settlement: Mapped[Settlement] = relationship(back_populates='issues')


class Correction(Base):
    """已结账后的更正：只记差额，不改原结账单。"""
    __tablename__ = 'corrections'
    id: Mapped[int] = mapped_column(primary_key=True)
    settlement_id: Mapped[int] = mapped_column(ForeignKey('settlements.id'), index=True)
    reference: Mapped[str] = mapped_column(String(60), unique=True)
    reason: Mapped[str] = mapped_column(String(500))
    request_id: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default='posted')
    total_delta_milli: Mapped[int] = mapped_column(BigInteger, default=0)
    allocated_delta_milli: Mapped[int] = mapped_column(BigInteger, default=0)
    idle_delta_milli: Mapped[int] = mapped_column(BigInteger, default=0)
    base_total_milli: Mapped[int] = mapped_column(BigInteger, default=0)
    base_allocated_milli: Mapped[int] = mapped_column(BigInteger, default=0)
    base_idle_milli: Mapped[int] = mapped_column(BigInteger, default=0)
    input_revision: Mapped[int] = mapped_column(BigInteger, default=0)
    fingerprint: Mapped[str] = mapped_column(String(64), default='')
    created_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    created_at: Mapped[float] = mapped_column(Double)


class Posting(Base):
    """入账台账。posting_key 唯一，保证中断重试不重复入账。"""
    __tablename__ = 'postings'
    __table_args__ = (UniqueConstraint('posting_key', name='uq_posting_key'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    settlement_id: Mapped[int] = mapped_column(ForeignKey('settlements.id'), index=True)
    posting_key: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20), default='firing')  # firing / idle
    firing_id: Mapped[int | None] = mapped_column(ForeignKey('kiln_firings.id'), nullable=True)
    amount_milli: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[float] = mapped_column(Double)
    settlement: Mapped[Settlement] = relationship(back_populates='postings')


class CorrectionRow(Base):
    __tablename__ = 'correction_rows'
    id: Mapped[int] = mapped_column(primary_key=True)
    correction_id: Mapped[int] = mapped_column(ForeignKey('corrections.id'), index=True)
    kind: Mapped[str] = mapped_column(String(20), default='firing')  # firing / idle
    firing_id: Mapped[int | None] = mapped_column(ForeignKey('kiln_firings.id'), nullable=True)
    batch: Mapped[str] = mapped_column(String(80), default='')
    kiln_code: Mapped[str] = mapped_column(String(60), default='')
    original_milli: Mapped[int] = mapped_column(BigInteger, default=0)
    current_milli: Mapped[int] = mapped_column(BigInteger, default=0)
    delta_milli: Mapped[int] = mapped_column(BigInteger, default=0)
