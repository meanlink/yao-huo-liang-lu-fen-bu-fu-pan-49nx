"""累计电表读数 → 窑次分摊 → 结账 / 更正 的核心算法与事务编排。

口径
----
* 电量以毫千瓦时（mWh）整数记账，1 kWh = 1000 mWh，避免浮点尾差。
* 表差 = 同一计量组内、同一抄表链上相邻两次有效累计读数之差。
  同一抄表时刻取最高修订号；累计值回退时必须由“换表/清零衔接”承接。
* 结账窗口按窑次起止切分：有窑在烧的时段按各在烧窑次的装窑量分摊，
  无窑运行的时段单列为闲置；待核对时段单列，不参与结账。
* 结账要求待核对时段为 0，因此 分摊 + 闲置 = 表差 在结账时严格成立。
* 迟到读数 / 上游更新使未结账分摊失效并按受影响时段重算；已结账只登记更正差额。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from src.models import (AccountingState, Correction, CorrectionRow, KilnFiring,
                        Meter, MeterLink, MeterReading, Posting, Settlement,
                        SettlementIssue, SettlementRow, SettlementSlice)
from src.services.audit import record
from src.utils.serialization import checksum
from src.utils.units import fmt_time, kwh

ALGORITHM_VERSION = 2
MICRO = 1_000_000
GAP_FACTOR = 1.5
OPEN_STATUSES = ('draft', 'stale')


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def parse_time(value, field='时间'):
    """接受 epoch 秒或 ISO-8601 字符串，统一返回 epoch 秒。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return float(text)
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            pass
    raise HTTPException(422, f'{field}格式无效，请使用 ISO-8601 或 epoch 秒')


def split_int(total: int, weights) -> list[int]:
    """最大余数法：把整数 total 按权重精确分配，返回和恰好等于 total。"""
    n = len(weights)
    if n == 0:
        return []
    total = int(total)
    sign = -1 if total < 0 else 1
    total = abs(total)
    weights = [max(0.0, float(w)) for w in weights]
    weight_sum = sum(weights)
    if weight_sum <= 0:
        weights = [1.0] * n
        weight_sum = float(n)
    base = [int(total * w // weight_sum) for w in weights]
    remainder = total - sum(base)
    order = sorted(range(n), key=lambda i: (-((total * weights[i]) % weight_sum), i))
    for i in order[:remainder]:
        base[i] += 1
    return [sign * v for v in base]


def _micro(duration):
    return int(round(max(0.0, duration) * MICRO))


def get_state(db) -> AccountingState:
    state = db.get(AccountingState, 1)
    if state is None:
        state = AccountingState(id=1, data_revision=0, updated_at=time.time())
        db.add(state)
        db.flush()
    return state


def lock_state(db) -> AccountingState:
    """串行化“改数”与“结账”；不支持行锁的数据库退化为普通读取。"""
    get_state(db)
    try:
        return db.execute(
            select(AccountingState).where(AccountingState.id == 1).with_for_update()
        ).scalar_one()
    except Exception:  # pragma: no cover - SQLite 等不支持 FOR UPDATE
        return db.get(AccountingState, 1)


def bump_revision(db) -> int:
    state = lock_state(db)
    state.data_revision = int(state.data_revision or 0) + 1
    state.updated_at = time.time()
    db.flush()
    return state.data_revision


# --------------------------------------------------------------------------- #
# 抄表链
# --------------------------------------------------------------------------- #

def effective_readings(db, meter_id: int) -> list[MeterReading]:
    """每个抄表时刻只保留最高修订号的读数。"""
    rows = db.scalars(
        select(MeterReading).where(MeterReading.meter_id == meter_id)
        .order_by(MeterReading.reading_at, MeterReading.revision)
    ).all()
    best: dict[float, MeterReading] = {}
    for row in rows:
        current = best.get(row.reading_at)
        if current is None or row.revision > current.revision:
            best[row.reading_at] = row
    return [best[t] for t in sorted(best)]


def _links_for_group(db, group_code):
    return db.scalars(
        select(MeterLink).where(MeterLink.group_code == group_code).order_by(MeterLink.at)
    ).all()


def build_chains(db, group_code, meters=None):
    """按换表衔接把物理电表连成逻辑抄表链，返回 [[Meter, ...], ...]。"""
    meters = meters or db.scalars(select(Meter).where(Meter.group_code == group_code)
                                  .order_by(Meter.code)).all()
    by_id = {m.id: m for m in meters}
    links = [l for l in _links_for_group(db, group_code)
             if l.old_meter_id in by_id and l.new_meter_id in by_id]
    successor = {}
    has_predecessor = set()
    for link in links:
        successor.setdefault(link.old_meter_id, link.new_meter_id)
        has_predecessor.add(link.new_meter_id)
    chains, seen = [], set()
    for meter in meters:
        if meter.id in has_predecessor or meter.id in seen:
            continue
        chain, current = [], meter.id
        while current is not None and current in by_id and current not in seen:
            seen.add(current)
            chain.append(by_id[current])
            current = successor.get(current)
        chains.append(chain)
    for meter in meters:  # 环或父子关系异常时仍保证覆盖
        if meter.id not in seen:
            chains.append([meter])
            seen.add(meter.id)
    return chains


def reading_stream(db, chain):
    """把一条链上的有效读数按时间合并，返回 [(reading, meter), ...]。"""
    stream = []
    for meter in chain:
        for reading in effective_readings(db, meter.id):
            stream.append(reading)
    return sorted(stream, key=lambda r: (r.reading_at, r.meter_id))


def _link_between(db, group_code, a: MeterReading, b: MeterReading):
    for link in _links_for_group(db, group_code):
        if link.old_meter_id == a.meter_id and link.new_meter_id == b.meter_id \
                and a.reading_at <= link.at <= b.reading_at:
            return link
    return None


def pair_delta(db, group_code, a: MeterReading, b: MeterReading):
    """返回 (消耗 mWh 或 None, 问题码, 衔接记录)。"""
    if a.meter_id == b.meter_id:
        if b.reading_value_milli >= a.reading_value_milli:
            return b.reading_value_milli - a.reading_value_milli, None, None
        # 同表清零同样需要衔接登记
        for link in _links_for_group(db, group_code):
            if link.old_meter_id == a.meter_id and link.new_meter_id == b.meter_id \
                    and a.reading_at <= link.at <= b.reading_at:
                if link.old_final_milli >= a.reading_value_milli \
                        and link.new_initial_milli <= b.reading_value_milli:
                    return (link.old_final_milli - a.reading_value_milli) + \
                           (b.reading_value_milli - link.new_initial_milli), None, link
                return None, 'link_unclear', link
        return None, 'rollback_unlinked', None

    link = _link_between(db, group_code, a, b)
    if link is None:
        return None, 'meter_switch_unlinked', None
    if link.old_final_milli < a.reading_value_milli \
            or link.new_initial_milli > b.reading_value_milli:
        return None, 'link_unclear', link
    consumed = (link.old_final_milli - a.reading_value_milli) + \
               (b.reading_value_milli - link.new_initial_milli)
    return consumed, None, link


def affected_span(db, group_code, meter, at):
    """新增/更正读数会影响的时间区间：链上前后相邻读数之间。

    没有相邻读数时以期望抄表间隔向两侧扩展，避免漏掉邻近的未结账分摊。
    """
    for chain in build_chains(db, group_code):
        if meter.id not in [m.id for m in chain]:
            continue
        stream = reading_stream(db, chain)
        previous = [r for r in stream if r.reading_at < at]
        following = [r for r in stream if r.reading_at > at]
        interval = meter.reading_interval_seconds or 86400
        start = previous[-1].reading_at if previous else at - interval
        end = following[0].reading_at if following else at + interval
        return start, max(start, end)
    return at - 86400, at + 86400


# --------------------------------------------------------------------------- #
# 分摊计算
# --------------------------------------------------------------------------- #

def _issue(issues, code, message, start=None, end=None, detail=None):
    issues.append((code, message, start, end, detail or {}))


def build_plan(db, group_code, start_at, end_at):
    """计算但不落库：slices / rows / issues / totals / fingerprint。"""
    if end_at <= start_at:
        raise HTTPException(422, '结账结束时间必须晚于开始时间')
    state = get_state(db)
    meters = db.scalars(select(Meter).where(Meter.group_code == group_code)
                        .order_by(Meter.code)).all()
    if not meters:
        raise HTTPException(404, '该计量组下没有电表')

    firings = db.scalars(
        select(KilnFiring).where(
            KilnFiring.group_code == group_code,
            KilnFiring.start_at < end_at,
            KilnFiring.end_at > start_at,
        ).order_by(KilnFiring.start_at, KilnFiring.batch)
    ).all()

    issues = []
    resolved_segments = []   # (start, end, delta)
    pending_segments = []    # (start, end, known_delta_or_None, code)
    unknown_energy = False

    chains = build_chains(db, group_code, meters)
    for chain in chains:
        stream = reading_stream(db, chain)
        if not stream:
            codes = '/'.join(m.code for m in chain)
            _issue(issues, 'missing_reading', f'抄表链 {codes} 在结账窗口内没有任何读数',
                   start_at, end_at, {'meters': codes})
            pending_segments.append((start_at, end_at, None, 'missing_reading'))
            unknown_energy = True
            continue
        if stream[0].reading_at > start_at:
            lo = start_at
            hi = min(end_at, stream[0].reading_at)
            _issue(issues, 'missing_start_reading',
                   f'抄表链 {stream[0].meter.code} 缺少 {fmt_time(start_at)} 之前的起始读数',
                   lo, hi, {'meter': stream[0].meter.code})
            pending_segments.append((lo, hi, None, 'missing_start_reading'))
            unknown_energy = True
        if stream[-1].reading_at < end_at:
            lo = max(start_at, stream[-1].reading_at)
            _issue(issues, 'missing_end_reading',
                   f'抄表链 {stream[-1].meter.code} 缺少 {fmt_time(end_at)} 之后的结束读数',
                   lo, end_at, {'meter': stream[-1].meter.code})
            pending_segments.append((lo, end_at, None, 'missing_end_reading'))
            unknown_energy = True

        for a, b in zip(stream, stream[1:]):
            if b.reading_at <= start_at or a.reading_at >= end_at:
                continue
            lo, hi = max(a.reading_at, start_at), min(b.reading_at, end_at)
            if hi <= lo:
                continue
            duration = b.reading_at - a.reading_at
            delta, problem, link = pair_delta(db, group_code, a, b)
            interval = a.meter.reading_interval_seconds
            gap = duration > interval * GAP_FACTOR
            if delta is None:
                labels = {
                    'rollback_unlinked': f'电表 {a.meter.code} 读数回退，但未登记换表衔接',
                    'link_unclear': '换表衔接登记的止码/起码与原读数不符，无法确认承接量',
                    'meter_switch_unlinked': f'电表 {a.meter.code}→{b.meter.code} 跨表读数缺少衔接登记',
                }
                _issue(issues, problem, f'{labels.get(problem, "读数异常")}，时段待核对',
                       lo, hi, {'from_meter': a.meter.code, 'to_meter': b.meter.code,
                                'from': a.reading_value_milli / 1000,
                                'to': b.reading_value_milli / 1000})
                pending_segments.append((lo, hi, None, problem))
                unknown_energy = True
                continue
            outside = duration - (hi - lo)
            share = split_int(delta, [_micro(hi - lo), _micro(outside)])[0]
            if gap:
                _issue(issues, 'missing_reading',
                       f'电表 {a.meter.code} 在 {fmt_time(a.reading_at)}–{fmt_time(b.reading_at)} '
                       f'缺少中间读数（间隔超过期望 {interval} 秒），时段待核对',
                       lo, hi, {'meter': a.meter.code, 'delta_kwh': kwh(share)})
                pending_segments.append((lo, hi, share, 'missing_reading'))
            else:
                resolved_segments.append((lo, hi, share))

    clipped = []
    for firing in firings:
        cs, ce = max(firing.start_at, start_at), min(firing.end_at, end_at)
        if ce > cs:
            clipped.append((firing, cs, ce))
    boundaries = sorted({start_at, end_at, *[c[1] for c in clipped], *[c[2] for c in clipped]})
    intervals = [(boundaries[i], boundaries[i + 1]) for i in range(len(boundaries) - 1)]

    def distribute(segments):
        buckets = [0] * len(intervals)
        for lo, hi, delta in segments:
            if delta is None:
                continue
            indexes, weights = [], []
            for index, (i0, i1) in enumerate(intervals):
                if i1 <= lo or i0 >= hi:
                    continue
                indexes.append(index)
                weights.append(_micro(min(hi, i1) - max(lo, i0)))
            for index, part in zip(indexes, split_int(delta, weights)):
                buckets[index] += part
        return buckets

    resolved_energy = distribute(resolved_segments)
    pending_energy = distribute([(s, e, d) for s, e, d, _ in pending_segments])

    slices = []
    for index, (i0, i1) in enumerate(intervals):
        if i1 <= i0:
            continue
        active = [(f, cs, ce) for f, cs, ce in clipped if cs <= i0 and ce >= i1]
        if resolved_energy[index] > 0:
            if active:
                shares = split_int(resolved_energy[index], [f.loaded for f, _, _ in active])
                slices.append({'start': i0, 'end': i1, 'delta': resolved_energy[index],
                               'kind': 'allocated', 'firing_count': len(active),
                               'shares': [(f, s) for (f, _, _), s in zip(active, shares)]})
            else:
                slices.append({'start': i0, 'end': i1, 'delta': resolved_energy[index],
                               'kind': 'idle', 'firing_count': 0, 'shares': []})
        pending_here = pending_energy[index] > 0 or any(
            s < i1 and e > i0 for s, e, _, _ in pending_segments)
        if pending_here:
            slices.append({'start': i0, 'end': i1, 'delta': pending_energy[index],
                           'kind': 'unresolved', 'firing_count': len(active), 'shares': []})

    rows = {firing.id: {'firing': firing, 'allocated': 0, 'overlap': 0.0}
            for firing, _, _ in clipped}
    for item in slices:
        if item['kind'] == 'allocated':
            for firing, share in item['shares']:
                rows[firing.id]['allocated'] += share
    for firing, cs, ce in clipped:
        rows[firing.id]['overlap'] = ce - cs

    allocated_total = sum(s['delta'] for s in slices if s['kind'] == 'allocated')
    idle_total = sum(s['delta'] for s in slices if s['kind'] == 'idle')
    pending_total = sum(s['delta'] for s in slices if s['kind'] == 'unresolved')
    assert allocated_total + idle_total + pending_total >= 0

    return {
        'slices': slices,
        'rows': rows,
        'issues': issues,
        'allocated_total': allocated_total,
        'idle_total': idle_total,
        'unresolved_total': pending_total,
        'delta_total': allocated_total + idle_total + pending_total,
        'unknown_energy': unknown_energy,
        'pending_count': len(issues),
        'fingerprint': input_fingerprint(db, group_code, start_at, end_at, meters, firings),
        'input_revision': int(state.data_revision or 0),
    }


def _window_readings(chain_stream, start_at, end_at):
    """只保留真正影响该窗口的读数：窗口前最后一条、窗口内、窗口后第一条。"""
    before = [r for r in chain_stream if r.reading_at <= start_at]
    after = [r for r in chain_stream if r.reading_at >= end_at]
    inside = [r for r in chain_stream if start_at < r.reading_at < end_at]
    picked = ([before[-1]] if before else []) + inside + ([after[0]] if after else [])
    return picked


def input_fingerprint(db, group_code, start_at, end_at, meters=None, firings=None):
    """窗口内影响分摊的输入指纹，用于识别并发改数；窗口外变化不影响本单。"""
    meters = meters if meters is not None else db.scalars(
        select(Meter).where(Meter.group_code == group_code).order_by(Meter.code)).all()
    firings = firings if firings is not None else db.scalars(
        select(KilnFiring).where(
            KilnFiring.group_code == group_code,
            KilnFiring.start_at < end_at, KilnFiring.end_at > start_at,
        ).order_by(KilnFiring.batch)).all()
    readings, links = [], []
    span_start, span_end = start_at, end_at
    for chain in build_chains(db, group_code, meters):
        stream = reading_stream(db, chain)
        for reading in _window_readings(stream, start_at, end_at):
            readings.append([reading.meter.code, round(reading.reading_at, 6),
                             reading.reading_value_milli, reading.revision])
            span_start = min(span_start, reading.reading_at)
            span_end = max(span_end, reading.reading_at)
    for link in _links_for_group(db, group_code):
        if span_start <= link.at <= span_end:
            links.append([link.old_meter_id, link.new_meter_id, round(link.at, 6),
                          link.old_final_milli, link.new_initial_milli])
    payload = {
        'algorithm': ALGORITHM_VERSION, 'group': group_code,
        'start': round(start_at, 6), 'end': round(end_at, 6),
        'readings': sorted(readings),
        'links': sorted(links),
        'firings': sorted([[f.batch, f.kiln_code, round(f.start_at, 6), round(f.end_at, 6),
                            f.loaded, f.passed, f.revision] for f in firings]),
    }
    return checksum(payload)


def _write_plan(db, settlement, plan):
    db.execute(delete(SettlementSlice).where(SettlementSlice.settlement_id == settlement.id))
    db.execute(delete(SettlementRow).where(SettlementRow.settlement_id == settlement.id))
    db.execute(delete(SettlementIssue).where(SettlementIssue.settlement_id == settlement.id))
    for seq, item in enumerate(plan['slices'], start=1):
        db.add(SettlementSlice(
            settlement_id=settlement.id, seq=seq, start_at=item['start'], end_at=item['end'],
            delta_milli=item['delta'], kind=item['kind'], firing_count=item['firing_count'],
            breakdown=[[f.batch, s] for f, s in item['shares']]))
    for entry in plan['rows'].values():
        firing = entry['firing']
        db.add(SettlementRow(
            settlement_id=settlement.id, firing_id=firing.id, batch=firing.batch,
            kiln_code=firing.kiln_code, start_at=firing.start_at, end_at=firing.end_at,
            loaded=firing.loaded, passed=firing.passed,
            overlap_seconds=entry['overlap'], allocated_milli=entry['allocated'], posted=False))
    for code, message, start, end, detail in plan['issues']:
        db.add(SettlementIssue(settlement_id=settlement.id, code=code, message=message,
                               start_at=start, end_at=end, detail=detail))
    settlement.delta_total_milli = plan['delta_total']
    settlement.allocated_total_milli = plan['allocated_total']
    settlement.idle_total_milli = plan['idle_total']
    settlement.unresolved_total_milli = plan['unresolved_total']
    settlement.incomplete_readings = plan['unknown_energy']
    settlement.pending_count = plan['pending_count']
    settlement.fingerprint = plan['fingerprint']
    settlement.input_revision = plan['input_revision']
    settlement.algorithm_version = ALGORITHM_VERSION
    settlement.revision = int(settlement.revision or 0) + 1
    settlement.recomputed_at = time.time()
    db.flush()


# --------------------------------------------------------------------------- #
# 结账单生命周期
# --------------------------------------------------------------------------- #

def create_settlement(db, data, user):
    group_code = data.group_code.strip()
    start_at = parse_time(data.start_at, '开始时间')
    end_at = parse_time(data.end_at, '结束时间')
    if end_at <= start_at:
        raise HTTPException(422, '结账结束时间必须晚于开始时间')
    if db.scalar(select(Settlement).where(Settlement.reference == data.reference.strip())):
        raise HTTPException(409, '结账单号已存在')
    settlement = Settlement(
        reference=data.reference.strip(), name=data.name.strip(), group_code=group_code,
        start_at=start_at, end_at=end_at, status='draft', revision=0,
        input_revision=int(get_state(db).data_revision or 0),
        created_by=user.id, created_at=time.time())
    db.add(settlement)
    db.flush()
    plan = build_plan(db, group_code, start_at, end_at)
    _write_plan(db, settlement, plan)
    if plan['pending_count']:
        settlement.status = 'stale'
        settlement.stale_reason = f'{plan["pending_count"]} 个时段待核对，补齐读数或换表衔接后重算'
        settlement.stale_at = time.time()
    record(db, user.email, 'settlement.create', {
        'id': settlement.id, 'reference': settlement.reference, 'group': group_code,
        'revision': settlement.revision, 'pending': plan['pending_count'],
        'delta_kwh': kwh(plan['delta_total'])})
    return settlement


def recompute(db, settlement, user=None, reason='手动重算',
              affected_start=None, affected_end=None):
    if settlement.status not in OPEN_STATUSES:
        raise HTTPException(409, '仅未结账的分摊可以重算')
    plan = build_plan(db, settlement.group_code, settlement.start_at, settlement.end_at)
    _write_plan(db, settlement, plan)
    settlement.status = 'stale' if plan['pending_count'] else 'draft'
    settlement.stale_reason = (
        f'{plan["pending_count"]} 个时段待核对，补齐读数或换表衔接后重算'
        if plan['pending_count'] else '')
    settlement.stale_at = time.time() if plan['pending_count'] else None
    settlement.recalc_count = int(settlement.recalc_count or 0) + 1
    settlement.last_recalc_start = affected_start
    settlement.last_recalc_end = affected_end
    settlement.last_recalc_reason = reason
    record(db, user.email if user else 'system', 'settlement.recompute', {
        'id': settlement.id, 'reason': reason, 'revision': settlement.revision,
        'pending': plan['pending_count'],
        'affected_start': affected_start, 'affected_end': affected_end})
    return settlement


def _reset_posting(db, settlement):
    """撤销未完成的入账台账；这些行从未构成已结账，旧的半成品分摊被整体拒绝。"""
    partial = db.scalars(select(Posting).where(
        Posting.settlement_id == settlement.id)).all()
    if partial:
        record(db, 'system', 'settlement.postings_reversed', {
            'id': settlement.id, 'keys': [p.posting_key for p in partial],
            'amount_kwh': [kwh(p.amount_milli) for p in partial]})
    settlement.posting_step = ''
    settlement.posting_progress = {}
    settlement.posting_started_at = None
    settlement.close_request_id = None
    db.execute(delete(Posting).where(Posting.settlement_id == settlement.id))
    for row in db.scalars(select(SettlementRow).where(
            SettlementRow.settlement_id == settlement.id)).all():
        row.posted = False


def mark_affected(db, group_code, affected_start, affected_end, reason):
    """迟到读数 / 上游更新：未结账的受影响分摊失效并按受影响时段重算。"""
    affected, closed_impacted = [], []
    candidates = db.scalars(select(Settlement).where(
        Settlement.group_code == group_code).with_for_update()).all()
    for settlement in candidates:
        if not (settlement.start_at < affected_end and settlement.end_at > affected_start):
            continue
        if settlement.status in OPEN_STATUSES:
            settlement.status = 'stale'
            settlement.stale_reason = reason
            settlement.stale_at = time.time()
            _reset_posting(db, settlement)
            db.flush()
            affected.append(settlement)
        elif settlement.status == 'closing':
            settlement.status = 'stale'
            settlement.stale_reason = '结账入账期间收到改数，旧分摊被拒绝'
            settlement.stale_at = time.time()
            _reset_posting(db, settlement)
            db.flush()
            affected.append(settlement)
        elif settlement.status == 'closed':
            closed_impacted.append(settlement)
    db.flush()
    for settlement in affected:
        recompute(db, settlement, None,
                  reason=f'受影响时段 {fmt_time(affected_start)}–{fmt_time(affected_end)}：{reason}',
                  affected_start=affected_start, affected_end=affected_end)
        record(db, 'system', 'settlement.recalculated', {
            'id': settlement.id, 'revision': settlement.revision,
            'affected_start': affected_start, 'affected_end': affected_end,
            'reason': reason})
    for settlement in closed_impacted:
        record(db, 'system', 'settlement.correction_required', {
            'id': settlement.id, 'affected_start': affected_start,
            'affected_end': affected_end, 'reason': reason})
    return affected, closed_impacted


# --------------------------------------------------------------------------- #
# 结账：分步入账 + 中断恢复 + 幂等
# --------------------------------------------------------------------------- #

def _claim_close(db, settlement, user, request_id):
    settlement.status = 'closing'
    settlement.posting_step = 'prepare'
    settlement.posting_progress = {'posted_rows': [], 'data_revision': _revision(db)}
    settlement.posting_started_at = time.time()
    settlement.close_request_id = request_id or f'close-{settlement.id}-{int(time.time() * 1000)}'
    record(db, user.email if user else 'system', 'settlement.close_start', {
        'id': settlement.id, 'revision': settlement.revision,
        'request_id': settlement.close_request_id})
    db.commit()
    db.refresh(settlement)


def _revision(db):
    state = db.get(AccountingState, 1)
    return int(state.data_revision or 0) if state else 0


def _abort_if_changed(db, settlement, claim_revision):
    """每次入账前复核；并发改数时拒绝旧分摊并清除未完成台账。"""
    try:
        db.execute(select(Settlement.id).where(
            Settlement.id == settlement.id).with_for_update())
        db.refresh(settlement)
    except Exception:  # pragma: no cover - 不支持行锁的数据库
        pass
    if settlement.status != 'closing':
        db.rollback()
        raise HTTPException(409, '该结账单已不在入账状态，旧分摊被拒绝')
    if _revision(db) != claim_revision or settlement.fingerprint != _current_fp(db, settlement):
        settlement.status = 'stale'
        settlement.stale_reason = '结账入账期间数据发生变化，旧分摊被拒绝'
        settlement.stale_at = time.time()
        _reset_posting(db, settlement)
        db.commit()
        raise HTTPException(409, '结账期间数据已变化，旧分摊被拒绝；请重算后重新结账')


def _current_fp(db, settlement):
    return input_fingerprint(db, settlement.group_code, settlement.start_at, settlement.end_at)


def post_rows(db, settlement, user, claim_revision):
    """逐行入账：台账唯一键保证中断重试不重复入账。"""
    rows = db.scalars(select(SettlementRow).where(
        SettlementRow.settlement_id == settlement.id).order_by(SettlementRow.id)).all()
    posted_by_key = {p.posting_key for p in db.scalars(
        select(Posting).where(Posting.settlement_id == settlement.id)).all()}
    for row in rows:
        key = f'{settlement.id}:firing:{row.firing_id}'
        if key in posted_by_key:
            if not row.posted:
                row.posted = True
            continue
        _abort_if_changed(db, settlement, claim_revision)
        try:
            db.add(Posting(settlement_id=settlement.id, posting_key=key, kind='firing',
                           firing_id=row.firing_id, amount_milli=row.allocated_milli,
                           created_at=time.time()))
            row.posted = True
            progress = dict(settlement.posting_progress or {})
            progress['posted_rows'] = list(progress.get('posted_rows', [])) + [row.id]
            settlement.posting_progress = progress
            settlement.posting_step = 'posting_rows'
            db.commit()
        except IntegrityError:
            db.rollback()
            settlement = db.get(Settlement, settlement.id)
            row = db.get(SettlementRow, row.id)
            if row and not row.posted:
                row.posted = True
                db.commit()
        posted_by_key.add(key)
    idle_key = f'{settlement.id}:idle'
    if idle_key not in posted_by_key:
        _abort_if_changed(db, settlement, claim_revision)
        db.add(Posting(settlement_id=settlement.id, posting_key=idle_key, kind='idle',
                       firing_id=None, amount_milli=settlement.idle_total_milli,
                       created_at=time.time()))
        db.commit()
    return settlement


def _finalize(db, settlement, user, claim_revision):
    _abort_if_changed(db, settlement, claim_revision)
    settlement.posting_step = 'finalize'
    settlement.status = 'closed'
    settlement.closed_by = user.id if user else None
    settlement.closed_at = time.time()
    record(db, user.email if user else 'system', 'settlement.closed', {
        'id': settlement.id, 'revision': settlement.revision,
        'delta_kwh': kwh(settlement.delta_total_milli),
        'allocated_kwh': kwh(settlement.allocated_total_milli),
        'idle_kwh': kwh(settlement.idle_total_milli),
        'request_id': settlement.close_request_id})
    db.commit()
    return settlement


def close(db, settlement, user, request_id=None, expected_revision=None,
          expected_fingerprint=None):
    """结账。请求号与台账唯一键共同保证原单重试不重复入账。"""
    if request_id and settlement.close_request_id == request_id:
        if settlement.status == 'closed':
            return settlement
        if settlement.status == 'closing':
            return resume(db, settlement, user)
    if settlement.status == 'closed':
        raise HTTPException(409, '该结账单已结账；如需调整请登记更正差额')
    if settlement.status == 'closing':
        raise HTTPException(409, '该结账单正在入账，请使用原结账请求号重试以恢复进度')
    if settlement.status not in OPEN_STATUSES:
        raise HTTPException(409, '当前状态不允许结账')
    if request_id:
        other = db.scalar(select(Settlement).where(
            Settlement.close_request_id == request_id,
            Settlement.id != settlement.id))
        if other:
            raise HTTPException(409, '该结账请求号已用于其他结账单')

    state = lock_state(db)  # 与改数串行
    claim_revision = int(state.data_revision or 0)
    # 以最新状态复核
    settlement = db.scalar(select(Settlement).where(Settlement.id == settlement.id)
                           .with_for_update().execution_options(populate_existing=True))
    if settlement.status == 'stale':
        raise HTTPException(
            409, f'数据已更新，旧分摊已失效：{settlement.stale_reason or "请重算后再结账"}')
    if settlement.status == 'closed':
        raise HTTPException(409, '该结账单已结账；如需调整请登记更正差额')
    if settlement.status not in OPEN_STATUSES:
        raise HTTPException(409, '当前状态不允许结账')
    if expected_revision is not None and int(expected_revision) != int(settlement.revision):
        raise HTTPException(409, '分摊版本已变化，请刷新后核对再结账')
    if expected_fingerprint and expected_fingerprint != settlement.fingerprint:
        raise HTTPException(409, '分摊版本已变化，请刷新后核对再结账')
    if settlement.pending_count:
        raise HTTPException(409, f'仍有 {settlement.pending_count} 个待核对时段，'
                                 f'补齐读数或换表衔接后方可结账')
    current = build_plan(db, settlement.group_code, settlement.start_at, settlement.end_at)
    if current['pending_count']:
        settlement.status = 'stale'
        settlement.stale_reason = '结账前复核发现待核对时段'
        settlement.stale_at = time.time()
        db.commit()
        raise HTTPException(409, '结账前复核发现待核对时段，旧分摊被拒绝')
    if current['fingerprint'] != settlement.fingerprint or \
            current['delta_total'] != settlement.delta_total_milli:
        settlement.status = 'stale'
        settlement.stale_reason = '结账期间数据发生变化，旧分摊失效'
        settlement.stale_at = time.time()
        db.commit()
        raise HTTPException(409, '结账期间数据已变化，旧分摊被拒绝；请重算后重新结账')
    if (current['allocated_total'] != settlement.allocated_total_milli
            or current['idle_total'] != settlement.idle_total_milli):
        settlement.status = 'stale'
        settlement.stale_reason = '结账期间分摊结果发生变化，旧分摊失效'
        settlement.stale_at = time.time()
        db.commit()
        raise HTTPException(409, '结账期间分摊结果已变化，旧分摊被拒绝；请重算后重新结账')

    _claim_close(db, settlement, user, request_id)
    claim_revision = int((settlement.posting_progress or {}).get('data_revision',
                                                                 claim_revision))
    post_rows(db, settlement, user, claim_revision)
    return _finalize(db, settlement, user, claim_revision)


def resume(db, settlement, user):
    """从持久化进度继续入账；已入账行按台账唯一键跳过。"""
    if settlement.status == 'closed':
        return settlement
    if settlement.status != 'closing':
        raise HTTPException(409, '该结账单没有可恢复的入账进度')
    claim_revision = int((settlement.posting_progress or {}).get(
        'data_revision', _revision(db)))
    record(db, user.email if user else 'system', 'settlement.resume', {
        'id': settlement.id, 'step': settlement.posting_step,
        'posted': len((settlement.posting_progress or {}).get('posted_rows', []))})
    db.commit()
    post_rows(db, settlement, user, claim_revision)
    return _finalize(db, settlement, user, claim_revision)


def recover_interrupted(db):
    """启动时恢复中断的结账，保留并续用已入账进度。"""
    recovered = []
    for settlement in db.scalars(select(Settlement).where(
            Settlement.status == 'closing')).all():
        try:
            resume(db, settlement, None)
            record(db, 'system', 'settlement.recovered', {'id': settlement.id})
            recovered.append(settlement.id)
        except HTTPException as exc:
            record(db, 'system', 'settlement.recovery_blocked',
                   {'id': settlement.id, 'detail': str(exc.detail)})
            db.commit()
    return recovered


# --------------------------------------------------------------------------- #
# 上游数据接入
# --------------------------------------------------------------------------- #

def create_meter(db, data, user):
    if db.scalar(select(Meter).where(Meter.code == data.code.strip())):
        raise HTTPException(409, '电表编号已存在')
    meter = Meter(code=data.code.strip(), name=data.name.strip(),
                  group_code=data.group_code.strip(), unit='kWh',
                  reading_interval_seconds=data.reading_interval_seconds,
                  created_at=time.time())
    db.add(meter)
    db.flush()
    record(db, user.email, 'meter.create',
           {'id': meter.id, 'code': meter.code, 'group': meter.group_code})
    return meter


def add_reading(db, meter, data, user):
    reading_at = parse_time(data.reading_at, '抄表时间')
    value_milli = int(round(float(data.value_kwh) * 1000))
    existing = db.scalars(select(MeterReading).where(
        MeterReading.meter_id == meter.id,
        MeterReading.reading_at == reading_at)).all()
    revision = data.revision
    if revision is None:
        revision = max([r.revision for r in existing], default=0) + 1
    elif any(r.revision == revision for r in existing):
        raise HTTPException(409, '该抄表时刻已存在相同修订号，请使用更高修订号更正')
    start, end = affected_span(db, meter.group_code, meter, reading_at)
    lock_state(db)
    row = MeterReading(meter_id=meter.id, reading_at=reading_at,
                       reading_value_milli=value_milli, revision=revision,
                       source=data.source, received_at=time.time(), created_by=user.id)
    db.add(row)
    db.flush()
    data_revision = bump_revision(db)
    affected, closed_impacted = mark_affected(
        db, meter.group_code, start, end,
        f'电表 {meter.code} 在 {fmt_time(reading_at)} 收到读数（修订号 {revision}）')
    record(db, user.email, 'meter.reading', {
        'meter': meter.code, 'at': reading_at, 'value_kwh': value_milli / 1000,
        'revision': revision, 'data_revision': data_revision,
        'affected_start': start, 'affected_end': end,
        'recomputed': [s.id for s in affected],
        'closed_impacted': [s.id for s in closed_impacted]})
    return row, affected, closed_impacted


def create_link(db, data, user):
    old = db.get(Meter, data.old_meter_id)
    new = db.get(Meter, data.new_meter_id)
    if not old or not new:
        raise HTTPException(404, '换表衔接涉及的电表不存在')
    if old.group_code != new.group_code:
        raise HTTPException(422, '换表衔接必须在同一计量组内')
    at = parse_time(data.at, '换表时间')
    if db.scalar(select(MeterLink).where(
            MeterLink.old_meter_id == old.id, MeterLink.new_meter_id == new.id,
            MeterLink.at == at)):
        raise HTTPException(409, '该换表衔接已登记')
    old_final = int(round(float(data.old_final_kwh) * 1000))
    new_initial = int(round(float(data.new_initial_kwh) * 1000))
    if old_final < 0 or new_initial < 0:
        raise HTTPException(422, '衔接读数值不能为负数')
    start, end = affected_span(db, old.group_code, old, at)
    lock_state(db)
    link = MeterLink(group_code=old.group_code, old_meter_id=old.id, new_meter_id=new.id,
                     at=at, old_final_milli=old_final, new_initial_milli=new_initial,
                     note=data.note, created_by=user.id, created_at=time.time())
    db.add(link)
    db.flush()
    bump_revision(db)
    affected, closed_impacted = mark_affected(
        db, old.group_code, min(start, at), max(end, at),
        f'登记 {old.code}→{new.code} 换表衔接，重新核对回退时段')
    record(db, user.email, 'meter.link', {
        'old': old.code, 'new': new.code, 'at': at,
        'recomputed': [s.id for s in affected],
        'closed_impacted': [s.id for s in closed_impacted]})
    return link


def upsert_firing(db, data, user):
    firing = db.scalar(select(KilnFiring).where(KilnFiring.batch == data.batch.strip()))
    start_at = parse_time(data.start_at, '开窑时间')
    end_at = parse_time(data.end_at, '停窑时间')
    if end_at <= start_at:
        raise HTTPException(422, '窑次停窑时间必须晚于开窑时间')
    if data.loaded <= 0 or data.passed < 0 or data.passed > data.loaded:
        raise HTTPException(422, '装窑量必须大于 0，且 0 ≤ 合格件数 ≤ 装窑量')
    group_code = data.group_code.strip()
    lock_state(db)
    if firing is None:
        firing = KilnFiring(batch=data.batch.strip(), group_code=group_code,
                            kiln_code=data.kiln_code.strip(), start_at=start_at,
                            end_at=end_at, loaded=data.loaded, passed=data.passed,
                            revision=1, source=data.source, created_by=user.id,
                            created_at=time.time(), updated_at=time.time())
        db.add(firing)
        start, end = start_at, end_at
        action = 'firing.create'
    else:
        start = min(firing.start_at, start_at)
        end = max(firing.end_at, end_at)
        group_code = firing.group_code
        firing.kiln_code = data.kiln_code.strip()
        firing.start_at, firing.end_at = start_at, end_at
        firing.loaded, firing.passed = data.loaded, data.passed
        firing.revision = int(firing.revision or 1) + 1
        firing.updated_at = time.time()
        action = 'firing.update'
    db.flush()
    bump_revision(db)
    affected, closed_impacted = mark_affected(
        db, group_code, start, end,
        f'上游{"更新" if action.endswith("update") else "新增"}窑次 {firing.batch}'
        f'（修订号 {firing.revision}）')
    record(db, user.email, action, {
        'id': firing.id, 'batch': firing.batch, 'revision': firing.revision,
        'loaded': firing.loaded, 'passed': firing.passed,
        'recomputed': [s.id for s in affected],
        'closed_impacted': [s.id for s in closed_impacted]})
    return firing


# --------------------------------------------------------------------------- #
# 已结账更正：只记差额，不改旧账
# --------------------------------------------------------------------------- #

def record_correction(db, settlement, data, user):
    try:
        db.execute(select(Settlement.id).where(
            Settlement.id == settlement.id).with_for_update())
        db.refresh(settlement)
    except Exception:  # pragma: no cover
        pass
    if settlement.status != 'closed':
        raise HTTPException(409, '仅已结账的分摊需要登记更正差额')
    if data.request_id:
        existing = db.scalar(select(Correction).where(
            Correction.request_id == data.request_id))
        if existing:
            return existing
    reason = (data.reason or '').strip()
    if len(reason) < 4:
        raise HTTPException(422, '更正来由至少需要 4 个字符，便于复核')
    plan = build_plan(db, settlement.group_code, settlement.start_at, settlement.end_at)
    if plan['pending_count']:
        raise HTTPException(409, '当前数据仍有待核对时段，补齐读数与换表衔接后才能登记更正')
    latest = db.scalars(select(Correction).where(
        Correction.settlement_id == settlement.id)
        .order_by(Correction.id.desc())).first()
    if latest and latest.fingerprint == plan['fingerprint']:
        raise HTTPException(409, '当前数据已登记过更正，无需重复记账')

    previous_rows = {}
    if latest:
        for row in db.scalars(select(CorrectionRow).where(
                CorrectionRow.correction_id == latest.id)).all():
            if row.kind == 'firing':
                previous_rows[row.firing_id] = row.current_milli
    if latest:
        base_total = latest.base_total_milli + latest.total_delta_milli
        base_allocated = latest.base_allocated_milli + latest.allocated_delta_milli
        base_idle = latest.base_idle_milli + latest.idle_delta_milli
    else:
        base_total = settlement.delta_total_milli
        base_allocated = settlement.allocated_total_milli
        base_idle = settlement.idle_total_milli

    correction = Correction(
        settlement_id=settlement.id,
        reference=(data.reference.strip() if data.reference else
                   f'{settlement.reference}-C{int(time.time() * 1000)}'),
        reason=reason, request_id=data.request_id, status='posted',
        total_delta_milli=plan['delta_total'] - base_total,
        allocated_delta_milli=plan['allocated_total'] - base_allocated,
        idle_delta_milli=plan['idle_total'] - base_idle,
        base_total_milli=base_total,
        base_allocated_milli=base_allocated,
        base_idle_milli=base_idle,
        input_revision=plan['input_revision'], fingerprint=plan['fingerprint'],
        created_by=user.id, created_at=time.time())
    if db.scalar(select(Correction).where(Correction.reference == correction.reference)):
        raise HTTPException(409, '更正单号已存在')
    db.add(correction)
    db.flush()

    by_firing = {entry['firing'].id: entry['firing'] for entry in plan['rows'].values()}
    all_ids = sorted(set(by_firing) | set(previous_rows), key=lambda fid: (
        by_firing[fid].batch if fid in by_firing else str(fid)))
    for firing_id in all_ids:
        firing = by_firing.get(firing_id)
        original = previous_rows.get(firing_id)
        if original is None:
            stored = db.scalar(select(SettlementRow).where(
                SettlementRow.settlement_id == settlement.id,
                SettlementRow.firing_id == firing_id))
            original = stored.allocated_milli if stored else 0
        current = plan['rows'].get(firing_id, {}).get('allocated', 0)
        db.add(CorrectionRow(
            correction_id=correction.id, kind='firing', firing_id=firing_id,
            batch=firing.batch if firing else '', kiln_code=firing.kiln_code if firing else '',
            original_milli=original, current_milli=current, delta_milli=current - original))
    db.add(CorrectionRow(correction_id=correction.id, kind='idle', firing_id=None,
                         batch='闲置电量', kiln_code='', original_milli=base_idle,
                         current_milli=plan['idle_total'],
                         delta_milli=plan['idle_total'] - base_idle))
    record(db, user.email, 'settlement.correction', {
        'id': settlement.id, 'correction': correction.id, 'reason': reason,
        'delta_kwh': kwh(correction.total_delta_milli)})
    return correction
