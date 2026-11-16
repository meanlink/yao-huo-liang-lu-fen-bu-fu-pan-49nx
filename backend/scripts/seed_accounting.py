"""电表结账演示数据：三口窑、一块共用电表、一次换表衔接与一段缺读数。

时间基准固定为 2026-11-10 00:00 UTC，便于复核与重放。
"""
import time

from sqlalchemy import select
from src.config.database import Session
from src.models import KilnFiring, Meter, MeterLink, MeterReading, Settlement
from src.services.accounting import create_settlement, get_state, kwh, parse_time
from src.services.audit import record
from src.schemas.accounting import SettlementInput

BASE_ISO = '2026-11-10T00:00:00+00:00'
GROUP = '一号窑线 · 共用电表'


def base_ts():
    return parse_time(BASE_ISO)


def hours(n):
    return base_ts() + n * 3600


def demo_readings():
    """共用电表 M-主表 每 4 小时一抄；第 9 天换表到 M-新表。"""
    rows = []
    for i in range(0, 73):  # 0h .. 288h
        t = hours(i * 4)
        rows.append(('M-主表', t, round(120000 + i * 46.5, 3)))
    for i in range(0, 25):
        t = hours(288 + i * 4)
        if t == hours(288):
            continue
        rows.append(('M-新表', t, round(i * 44.25, 3)))
    return rows


def demo_firings():
    plan = [
        ('QH-201', '东窑', 4, 34, 240, 226),
        ('QH-202', '中窑', 6, 40, 260, 241),
        ('QH-203', '西窑', 30, 62, 220, 214),
        ('QH-204', '东窑', 38, 70, 250, 236),
        ('QH-205', '中窑', 66, 100, 270, 249),
        ('QH-206', '西窑', 104, 138, 230, 221),
        ('QH-207', '东窑', 142, 176, 245, 231),
        ('QH-208', '中窑', 180, 214, 265, 244),
        ('QH-209', '西窑', 218, 252, 235, 227),
        ('QH-210', '东窑', 256, 286, 255, 240),
    ]
    return [(batch, kiln, hours(a), hours(b), loaded, passed)
            for batch, kiln, a, b, loaded, passed in plan]


def seed_accounting():
    with Session() as db:
        get_state(db)
        group = GROUP
        existing = db.scalar(select(Meter).where(Meter.code == 'M-主表'))
        if not existing:
            old = Meter(code='M-主表', name='窑线总表（旧）', group_code=group,
                        unit='kWh', reading_interval_seconds=4 * 3600,
                        created_at=time.time())
            new = Meter(code='M-新表', name='窑线总表（新）', group_code=group,
                        unit='kWh', reading_interval_seconds=4 * 3600,
                        created_at=time.time())
            db.add_all([old, new])
            db.flush()
            for code, at, value in demo_readings():
                meter = old if code == 'M-主表' else new
                db.add(MeterReading(meter_id=meter.id, reading_at=at,
                                    reading_value_milli=int(round(value * 1000)),
                                    revision=1, source='seed', received_at=time.time()))
            last_old_value = 120000 + 72 * 46.5
            db.add(MeterLink(group_code=group, old_meter_id=old.id, new_meter_id=new.id,
                             at=hours(288.5),
                             old_final_milli=int(round(last_old_value * 1000)),
                             new_initial_milli=0,
                             note='第 9 天计划换表：旧表止码 123,348 kWh，新表从 0 起',
                             created_at=time.time()))
            db.flush()
        for batch, kiln, start, end, loaded, passed in demo_firings():
            if not db.scalar(select(KilnFiring).where(KilnFiring.batch == batch)):
                db.add(KilnFiring(batch=batch, group_code=group, kiln_code=kiln,
                                  start_at=start, end_at=end, loaded=loaded,
                                  passed=passed, revision=1, source='seed',
                                  created_at=time.time(), updated_at=time.time()))
        db.commit()

        if not db.scalar(select(Settlement).where(Settlement.reference == 'JD-202611-01')):
            ref = SettlementInput(reference='JD-202611-01', name='11 月第二周 · 一号窑线分摊',
                                  group_code=group, start_at=base_ts(),
                                  end_at=hours(240))
            settlement = create_settlement(db, ref, _demo_user(db))
            record(db, 'seed', 'settlement.demo', {
                'id': settlement.id, 'status': settlement.status,
                'delta_kwh': kwh(settlement.delta_total_milli)})
            db.commit()

        if not db.scalar(select(Settlement).where(Settlement.reference == 'JD-202611-02')):
            ref = SettlementInput(reference='JD-202611-02', name='11 月第二周 · 含换表日分摊',
                                  group_code=group, start_at=hours(240),
                                  end_at=hours(300))
            create_settlement(db, ref, _demo_user(db))
            db.commit()


def _demo_user(db):
    from src.models import User
    return db.scalar(select(User).where(User.role == 'admin'))


if __name__ == '__main__':
    seed_accounting()
