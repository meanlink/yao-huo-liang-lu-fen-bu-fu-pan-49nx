import hashlib, json
def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
def dataset_dict(d):
    return {'id':d.id, 'name':d.name, 'source':d.source, 'rows':len(d.records), 'checksum':d.checksum, 'created_at':d.created_at}
def job_dict(j):
    return {k:getattr(j,k) for k in ['id','name','dataset_id','params','status','result','error','approved','created_at','finished_at']}
def report_dict(r):
    return {k:getattr(r,k) for k in ['id','name','job_id','destination','interval_seconds','next_run','enabled']}
def delivery_dict(d):
    return {k:getattr(d,k) for k in ['id','report_id','created_at','checksum','filename','destination']}


# --------------------------------------------------------------------------- #
# 电表结账序列化
# --------------------------------------------------------------------------- #
from src.utils.units import kwh, fmt_time


def meter_dict(m, with_readings=False, db=None):
    data = {'id': m.id, 'code': m.code, 'name': m.name, 'group_code': m.group_code,
            'unit': m.unit, 'reading_interval_seconds': m.reading_interval_seconds,
            'created_at': m.created_at}
    if with_readings and db is not None:
        from src.services.accounting import effective_readings
        data['readings'] = [reading_dict(r) for r in effective_readings(db, m.id)]
    return data


def reading_dict(r):
    return {'id': r.id, 'meter_id': r.meter_id, 'reading_at': r.reading_at,
            'reading_at_text': fmt_time(r.reading_at), 'value_kwh': kwh(r.reading_value_milli),
            'revision': r.revision, 'source': r.source, 'received_at': r.received_at}


def link_dict(l, old=None, new=None):
    return {'id': l.id, 'group_code': l.group_code, 'old_meter_id': l.old_meter_id,
            'new_meter_id': l.new_meter_id, 'old_code': old.code if old else '',
            'new_code': new.code if new else '', 'at': l.at, 'at_text': fmt_time(l.at),
            'old_final_kwh': kwh(l.old_final_milli),
            'new_initial_kwh': kwh(l.new_initial_milli), 'note': l.note,
            'created_at': l.created_at}


def firing_dict(f):
    return {'id': f.id, 'batch': f.batch, 'group_code': f.group_code,
            'kiln_code': f.kiln_code, 'start_at': f.start_at, 'end_at': f.end_at,
            'start_text': fmt_time(f.start_at), 'end_text': fmt_time(f.end_at),
            'loaded': f.loaded, 'passed': f.passed,
            'yield_pct': round(f.passed / f.loaded * 100, 3) if f.loaded else 0,
            'kwh_per_passed': None, 'revision': f.revision, 'source': f.source,
            'updated_at': f.updated_at}


def settlement_summary(s):
    return {
        'id': s.id, 'reference': s.reference, 'name': s.name, 'group_code': s.group_code,
        'start_at': s.start_at, 'end_at': s.end_at,
        'start_text': fmt_time(s.start_at), 'end_text': fmt_time(s.end_at),
        'status': s.status, 'revision': s.revision, 'input_revision': s.input_revision,
        'fingerprint': s.fingerprint, 'algorithm_version': s.algorithm_version,
        'delta_kwh': kwh(s.delta_total_milli),
        'allocated_kwh': kwh(s.allocated_total_milli),
        'idle_kwh': kwh(s.idle_total_milli),
        'unresolved_kwh': kwh(s.unresolved_total_milli),
        'pending_count': s.pending_count, 'incomplete_readings': bool(s.incomplete_readings),
        'stale_reason': s.stale_reason, 'stale_at': s.stale_at,
        'recalc_count': s.recalc_count, 'recomputed_at': s.recomputed_at,
        'last_recalc_start': s.last_recalc_start, 'last_recalc_end': s.last_recalc_end,
        'last_recalc_reason': s.last_recalc_reason,
        'posting_step': s.posting_step,
        'posting_progress': s.posting_progress or {},
        'close_request_id': s.close_request_id,
        'created_at': s.created_at, 'closed_at': s.closed_at,
    }


def settlement_detail(s, db):
    from src.models import Correction, CorrectionRow, Posting
    from sqlalchemy import select
    data = settlement_summary(s)
    slices = sorted(s.slices, key=lambda x: x.seq)
    rows = sorted(s.rows, key=lambda x: (x.start_at, x.batch))
    data['slices'] = [{
        'seq': x.seq, 'start_at': x.start_at, 'end_at': x.end_at,
        'start_text': fmt_time(x.start_at), 'end_text': fmt_time(x.end_at),
        'kind': x.kind, 'delta_kwh': kwh(x.delta_milli),
        'firing_count': x.firing_count, 'breakdown': x.breakdown or []}
        for x in slices]
    data['allocated_total_check'] = kwh(sum(x.delta_milli for x in slices
                                            if x.kind == 'allocated'))
    data['idle_total_check'] = kwh(sum(x.delta_milli for x in slices if x.kind == 'idle'))
    data['rows'] = [{
        'firing_id': x.firing_id, 'batch': x.batch, 'kiln_code': x.kiln_code,
        'start_text': fmt_time(x.start_at), 'end_text': fmt_time(x.end_at),
        'loaded': x.loaded, 'passed': x.passed,
        'yield_pct': round(x.passed / x.loaded * 100, 3) if x.loaded else 0,
        'overlap_hours': round(x.overlap_seconds / 3600, 3),
        'allocated_kwh': kwh(x.allocated_milli),
        'kwh_per_passed': round(kwh(x.allocated_milli) / x.passed, 4) if x.passed else None,
        'posted': x.posted} for x in rows]
    data['issues'] = [{
        'id': x.id, 'code': x.code, 'message': x.message,
        'start_at': x.start_at, 'end_at': x.end_at,
        'start_text': fmt_time(x.start_at), 'end_text': fmt_time(x.end_at),
        'detail': x.detail or {}} for x in sorted(s.issues, key=lambda x: (x.start_at or 0, x.id))]
    postings = db.scalars(select(Posting).where(Posting.settlement_id == s.id)
                          .order_by(Posting.id)).all()
    data['postings'] = [{
        'id': p.id, 'key': p.posting_key, 'kind': p.kind, 'firing_id': p.firing_id,
        'amount_kwh': kwh(p.amount_milli), 'created_at': p.created_at} for p in postings]
    corrections = db.scalars(select(Correction).where(Correction.settlement_id == s.id)
                             .order_by(Correction.id)).all()
    data['corrections'] = []
    for c in corrections:
        rows_c = db.scalars(select(CorrectionRow).where(
            CorrectionRow.correction_id == c.id).order_by(CorrectionRow.id)).all()
        data['corrections'].append({
            'id': c.id, 'reference': c.reference, 'reason': c.reason,
            'status': c.status, 'created_at': c.created_at,
            'total_delta_kwh': kwh(c.total_delta_milli),
            'allocated_delta_kwh': kwh(c.allocated_delta_milli),
            'idle_delta_kwh': kwh(c.idle_delta_milli),
            'input_revision': c.input_revision,
            'rows': [{
                'kind': r.kind, 'batch': r.batch, 'kiln_code': r.kiln_code,
                'original_kwh': kwh(r.original_milli), 'current_kwh': kwh(r.current_milli),
                'delta_kwh': kwh(r.delta_milli)} for r in rows_c]})
    return data


def correction_dict(c, rows):
    return {'id': c.id, 'settlement_id': c.settlement_id, 'reference': c.reference,
            'reason': c.reason, 'status': c.status,
            'total_delta_kwh': kwh(c.total_delta_milli),
            'allocated_delta_kwh': kwh(c.allocated_delta_milli),
            'idle_delta_kwh': kwh(c.idle_delta_milli),
            'created_at': c.created_at,
            'rows': [{'kind': r.kind, 'batch': r.batch, 'kiln_code': r.kiln_code,
                      'original_kwh': kwh(r.original_milli),
                      'current_kwh': kwh(r.current_milli),
                      'delta_kwh': kwh(r.delta_milli)} for r in rows]}
