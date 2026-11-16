"""电表分摊结账的业务口径测试。

运行：在 backend/ 下 `DATABASE_URL=sqlite:////tmp/acc-test.db python -m pytest tests -q`
（或直接 `python tests/test_accounting.py`，无 pytest 时使用内置断言运行器）。
"""
import os
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
if 'DATABASE_URL' not in os.environ:
    os.environ['DATABASE_URL'] = 'sqlite:///' + tempfile.mkdtemp() + '/acc-test.db'
os.environ.setdefault('JWT_SECRET', 'test-secret')

from fastapi import HTTPException
from src.config.database import Base, Session, engine
from src import models
from src.models import (CorrectionRow, KilnFiring, Meter, MeterReading, Posting,
                        Settlement, SettlementRow, User)
from src.schemas.accounting import (CorrectionInput, FiringInput, LinkInput,
                                    ReadingInput, SettlementInput)
from src.services import accounting as acc
from src.utils.security import hash_password

Base.metadata.create_all(engine)
H = 3600.0
T0 = 1_800_000_000.0


def h(n):
    return T0 + n * H


def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = Session()
    admin = User(email='a@test', name='管理员', role='admin',
                 password=hash_password('pw'))
    db.add(admin)
    db.commit()
    return db, admin


def setup_group(db, group='G1', interval_hours=8, readings=None):
    meter = Meter(code='M1', name='窑线总表', group_code=group,
                  reading_interval_seconds=interval_hours * H, created_at=time.time())
    db.add(meter)
    db.flush()
    for hrs, value in readings or [(0, 1000), (8, 1200), (16, 1400), (24, 1600)]:
        db.add(MeterReading(meter_id=meter.id, reading_at=h(hrs),
                            reading_value_milli=int(value * 1000), revision=1,
                            source='test', received_at=time.time()))
    for batch, start, end, loaded, passed in [
            ('F1', 2, 10, 100, 90), ('F2', 6, 14, 300, 270)]:
        db.add(KilnFiring(batch=batch, group_code=group, kiln_code='东窑',
                          start_at=h(start), end_at=h(end), loaded=loaded,
                          passed=passed, revision=1, source='test',
                          created_at=time.time(), updated_at=time.time()))
    db.commit()
    return meter


def make_settlement(db, admin, group='G1', start=0, end=24, ref='S1'):
    settlement = acc.create_settlement(db, SettlementInput(
        reference=ref, name='测试结账', group_code=group,
        start_at=h(start), end_at=h(end)), admin)
    db.commit()
    return settlement


# --------------------------------------------------------------------------- #

def test_shared_meter_allocation_by_loaded_weight():
    db, admin = fresh_db()
    setup_group(db)
    s = make_settlement(db, admin)
    assert s.status == 'draft' and s.pending_count == 0
    rows = {r.batch: r for r in s.rows}
    # 每 8 小时 200 kWh。0-2h 闲置 50；2-6h F1 独享 100；
    # 6-10h F1:F2=1:3 共 100（F1 25 / F2 75）；10-14h F2 独享 100；
    # 14-24h 闲置 250。合计 600 kWh。
    assert s.delta_total_milli == 600_000
    assert rows['F1'].allocated_milli == 125_000
    assert rows['F2'].allocated_milli == 175_000
    assert s.idle_total_milli == 300_000
    assert s.allocated_total_milli + s.idle_total_milli == s.delta_total_milli
    db.close()


def test_idle_is_listed_separately_and_reduces_idle_only():
    db, admin = fresh_db()
    setup_group(db)
    s = make_settlement(db, admin)
    kinds = [x.kind for x in s.slices]
    assert 'idle' in kinds and 'allocated' in kinds
    idle = sum(x.delta_milli for x in s.slices if x.kind == 'idle')
    assert idle == s.idle_total_milli
    db.close()


def test_reading_revision_overrides_same_timestamp():
    db, admin = fresh_db()
    meter = setup_group(db)
    acc.add_reading(db, meter, ReadingInput(reading_at=h(8), value_kwh=1300, revision=2), admin)
    db.commit()
    s = make_settlement(db, admin)
    assert s.delta_total_milli == 600_000  # 1000→1600，修订号 2 覆盖 8 点旧值
    db.close()


def test_missing_reading_marks_stale_and_blocks_close():
    db, admin = fresh_db()
    setup_group(db)
    s = make_settlement(db, admin, end=40)  # 24h 后无结束读数
    assert s.status == 'stale' and s.pending_count >= 1
    assert any(i.code == 'missing_end_reading' for i in s.issues)
    try:
        acc.close(db, s, admin, request_id='c1')
        raise AssertionError('缺读数时不应允许结账')
    except HTTPException as exc:
        assert exc.status_code == 409
    db.close()


def test_reading_gap_between_samples_needs_review():
    db, admin = fresh_db()
    setup_group(db, interval_hours=4)  # 期望 4 小时，但只每 8 小时一抄
    s = make_settlement(db, admin)
    assert s.status == 'stale'
    assert any(i.code == 'missing_reading' for i in s.issues)
    db.close()


def test_rollback_without_link_is_pending_with_link_bridges():
    db, admin = fresh_db()
    meter = setup_group(db)
    acc.add_reading(db, meter, ReadingInput(reading_at=h(32), value_kwh=100), admin)
    db.commit()
    s = make_settlement(db, admin, start=0, end=32, ref='R1')
    assert any(i.code == 'rollback_unlinked' for i in s.issues)
    try:
        acc.close(db, s, admin, request_id='c2')
        raise AssertionError('回退未登记衔接不应允许结账')
    except HTTPException:
        pass
    acc.create_link(db, LinkInput(old_meter_id=meter.id, new_meter_id=meter.id,
                                  at=h(24), old_final_kwh=1600, new_initial_kwh=100),
                    admin)
    db.commit()
    s = db.get(Settlement, s.id)
    assert s.status == 'draft' and s.pending_count == 0
    # 1000→1600 旧表 + 清零后 100→100 新表起点 → 合计 600 kWh
    assert s.delta_total_milli == 600_000
    db.close()


def test_meter_switch_requires_link():
    db, admin = fresh_db()
    meter = setup_group(db)
    new = Meter(code='M2', name='新表', group_code='G1',
                reading_interval_seconds=8 * H, created_at=time.time())
    db.add(new)
    db.flush()
    db.add(MeterReading(meter_id=new.id, reading_at=h(32), reading_value_milli=100_000,
                        revision=1, source='test', received_at=time.time()))
    db.commit()
    s = make_settlement(db, admin, end=32, ref='SW')
    assert s.status == 'stale'
    acc.create_link(db, LinkInput(old_meter_id=meter.id, new_meter_id=new.id, at=h(24),
                                  old_final_kwh=1600, new_initial_kwh=0), admin)
    db.commit()
    s = db.get(Settlement, s.id)
    assert s.status == 'draft'
    assert s.delta_total_milli == 700_000
    db.close()


def test_late_reading_stales_and_recomputes_open_settlement():
    db, admin = fresh_db()
    meter = setup_group(db)
    s = make_settlement(db, admin)
    before = s.revision
    acc.add_reading(db, meter, ReadingInput(reading_at=h(4), value_kwh=1100, revision=2), admin)
    db.commit()
    s = db.get(Settlement, s.id)
    assert s.status == 'draft' and s.revision > before
    assert s.last_recalc_start is not None
    assert s.delta_total_milli == 600_000
    db.close()


def test_late_reading_on_closed_requires_correction_and_keeps_old_ledger():
    db, admin = fresh_db()
    meter = setup_group(db)
    s = make_settlement(db, admin)
    acc.close(db, s, admin, request_id='c3')
    db.commit()
    original_total = db.get(Settlement, s.id).delta_total_milli
    acc.add_reading(db, meter, ReadingInput(reading_at=h(16), value_kwh=1550, revision=2), admin)
    db.commit()
    assert db.get(Settlement, s.id).delta_total_milli == original_total
    correction = acc.record_correction(
        db, db.get(Settlement, s.id),
        CorrectionInput(reason='补录 16 点迟到读数', reference='C1'), admin)
    db.commit()
    # 16 点读数 1400→1550：总量不变，但 8–16h 与 16–24h 之间的电量重新切分
    assert correction.total_delta_milli == 0
    assert correction.allocated_delta_milli != 0
    assert correction.allocated_delta_milli + correction.idle_delta_milli == \
        correction.total_delta_milli
    assert db.get(Settlement, s.id).delta_total_milli == original_total
    db.close()


def test_close_is_idempotent_with_same_request_id():
    db, admin = fresh_db()
    setup_group(db)
    s = make_settlement(db, admin)
    acc.close(db, s, admin, request_id='same')
    db.commit()
    count = db.query(Posting).filter(Posting.settlement_id == s.id).count()
    acc.close(db, db.get(Settlement, s.id), admin, request_id='same')
    db.commit()
    assert db.query(Posting).filter(Posting.settlement_id == s.id).count() == count
    try:
        acc.close(db, db.get(Settlement, s.id), admin, request_id='other')
        raise AssertionError('已结账不应再次结账')
    except HTTPException as exc:
        assert exc.status_code == 409
    db.close()


def test_interrupted_posting_resumes_without_duplicates():
    db, admin = fresh_db()
    setup_group(db)
    s = make_settlement(db, admin)
    acc._claim_close(db, s, admin, 'resume-1')
    claim = int(s.posting_progress['data_revision'])
    rows = db.query(SettlementRow).filter(
        SettlementRow.settlement_id == s.id).order_by(SettlementRow.id).all()
    db.add(Posting(settlement_id=s.id, posting_key=f'{s.id}:firing:{rows[0].firing_id}',
                   kind='firing', firing_id=rows[0].firing_id,
                   amount_milli=rows[0].allocated_milli, created_at=time.time()))
    rows[0].posted = True
    s.posting_progress = {'posted_rows': [rows[0].id], 'data_revision': claim}
    s.posting_step = 'posting_rows'
    db.commit()
    resumed = acc.close(db, db.get(Settlement, s.id), admin, request_id='resume-1')
    db.commit()
    assert resumed.status == 'closed'
    keys = [p.posting_key for p in db.query(Posting).filter(Posting.settlement_id == s.id)]
    assert len(keys) == len(set(keys)) == len(rows) + 1
    db.close()


def test_recovery_on_startup_finishes_interrupted_close():
    db, admin = fresh_db()
    setup_group(db)
    s = make_settlement(db, admin)
    acc._claim_close(db, s, admin, 'boot-1')
    recovered = acc.recover_interrupted(db)
    assert s.id in recovered
    assert db.get(Settlement, s.id).status == 'closed'
    db.close()


def test_concurrent_change_rejects_old_allocation():
    db, admin = fresh_db()
    meter = setup_group(db)
    s = make_settlement(db, admin)
    acc._claim_close(db, s, admin, 'race')
    claim = int(s.posting_progress['data_revision'])
    db.add(MeterReading(meter_id=meter.id, reading_at=h(20), reading_value_milli=1500_000,
                        revision=1, source='late', received_at=time.time()))
    db.flush()
    acc.bump_revision(db)
    db.commit()
    try:
        acc.post_rows(db, s, admin, claim)
        raise AssertionError('并发改数后不应继续旧分摊')
    except HTTPException as exc:
        assert exc.status_code == 409
    s = db.get(Settlement, s.id)
    assert s.status == 'stale'
    assert db.query(Posting).filter(Posting.settlement_id == s.id).count() == 0
    db.close()


def test_firing_update_recomputes_open_and_flags_closed():
    db, admin = fresh_db()
    setup_group(db)
    open_s = make_settlement(db, admin, ref='OPEN')
    closed_s = make_settlement(db, admin, ref='CLOSED')
    acc.close(db, closed_s, admin, request_id='c4')
    db.commit()
    acc.upsert_firing(db, FiringInput(batch='F2', group_code='G1', kiln_code='东窑',
                                      start_at=h(6), end_at=h(14), loaded=900,
                                      passed=810), admin)
    db.commit()
    open_s = db.get(Settlement, open_s.id)
    closed_s = db.get(Settlement, closed_s.id)
    assert open_s.status == 'draft' and open_s.recalc_count >= 1
    assert closed_s.status == 'closed'
    actions = [a.action for a in db.query(models.Audit)]
    assert 'settlement.correction_required' in actions
    correction = acc.record_correction(
        db, closed_s, CorrectionInput(reason='上游修订装窑量', reference='C5'), admin)
    db.commit()
    # 总量不变，但同一时段的电量在 F1/F2 之间按新装窑量重分：差额落在逐窑行
    row_deltas = {r.batch: r.delta_milli for r in db.query(CorrectionRow).filter(
        CorrectionRow.correction_id == correction.id, CorrectionRow.kind == 'firing')}
    assert row_deltas['F1'] != 0 and row_deltas['F2'] == -row_deltas['F1']
    assert correction.allocated_delta_milli + correction.idle_delta_milli == \
        correction.total_delta_milli
    db.close()


def test_planted_sums_balance_exactly_with_units():
    """整数 mWh 记账：任意权重下 分摊 + 闲置 = 表差，无尾差。"""
    db, admin = fresh_db()
    setup_group(db, readings=[(0, 100), (8, 101), (16, 103), (24, 107)])  # 非整除电量
    s = make_settlement(db, admin)
    assert s.allocated_total_milli + s.idle_total_milli == s.delta_total_milli == 7_000
    assert sum(r.allocated_milli for r in s.rows) == s.allocated_total_milli
    db.close()


def test_split_int_is_exact():
    for total, weights in [(7, [1, 1, 1]), (1000, [1, 2, 3]), (5, [0, 0, 0]),
                           (-7, [1, 2]), (123456, [17, 3, 29, 11])]:
        parts = acc.split_int(total, weights)
        assert sum(parts) == total, (total, weights, parts)
    assert sorted(acc.split_int(7, [1, 1, 1])) == [2, 2, 3]


if __name__ == '__main__':
    failures = 0
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith('test_') and callable(fn)]
    for name, fn in tests:
        try:
            fn()
            print('PASS', name)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print('FAIL', name, '->', type(exc).__name__, exc)
            import traceback
            traceback.print_exc()
    print(f'{len(tests) - failures}/{len(tests)} passed')
    sys.exit(1 if failures else 0)
