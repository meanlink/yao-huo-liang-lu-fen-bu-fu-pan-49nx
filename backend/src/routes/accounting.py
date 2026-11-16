from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from src.config.database import get_db
from src.middlewares.auth import admin_only, can_write, current_user
from src.models import Correction, CorrectionRow, KilnFiring, Meter, MeterLink, Settlement
from src.schemas.accounting import (CloseInput, CorrectionInput, FiringInput,
                                    LinkInput, MeterInput, ReadingInput,
                                    SettlementInput)
from src.services import accounting
from src.utils.serialization import (correction_dict, firing_dict, link_dict,
                                     meter_dict, reading_dict,
                                     settlement_detail, settlement_summary)

router = APIRouter(prefix='/accounting')


# ---------------------------------- 基础数据 --------------------------------- #

@router.get('/meters')
def meters(group_code: str | None = None, db=Depends(get_db), u=Depends(current_user)):
    query = select(Meter).order_by(Meter.group_code, Meter.code)
    if group_code:
        query = query.where(Meter.group_code == group_code)
    return [meter_dict(m, with_readings=True, db=db) for m in db.scalars(query)]


@router.post('/meters', status_code=201)
def create_meter(data: MeterInput, db=Depends(get_db), u=Depends(can_write)):
    meter = accounting.create_meter(db, data, u)
    db.commit()
    return meter_dict(meter)


@router.post('/meters/{meter_id}/readings', status_code=201)
def add_reading(meter_id: int, data: ReadingInput, db=Depends(get_db), u=Depends(can_write)):
    meter = db.get(Meter, meter_id)
    if not meter:
        raise HTTPException(404, '电表不存在')
    row, affected, closed_impacted = accounting.add_reading(db, meter, data, u)
    db.commit()
    return {'reading': reading_dict(row),
            'data_revision': accounting.get_state(db).data_revision,
            'recalculated': [settlement_summary(s) for s in affected],
            'closed_impacted': [{'id': s.id, 'reference': s.reference} for s in closed_impacted]}


@router.get('/meters/links')
def links(db=Depends(get_db), u=Depends(current_user)):
    meters_by_id = {m.id: m for m in db.scalars(select(Meter))}
    return [link_dict(l, meters_by_id.get(l.old_meter_id), meters_by_id.get(l.new_meter_id))
            for l in db.scalars(select(MeterLink).order_by(MeterLink.at.desc()))]


@router.post('/meters/links', status_code=201)
def create_link(data: LinkInput, db=Depends(get_db), u=Depends(can_write)):
    link = accounting.create_link(db, data, u)
    db.commit()
    old, new = db.get(Meter, link.old_meter_id), db.get(Meter, link.new_meter_id)
    return link_dict(link, old, new)


@router.get('/firings')
def firings(group_code: str | None = None, db=Depends(get_db), u=Depends(current_user)):
    query = select(KilnFiring).order_by(KilnFiring.start_at.desc())
    if group_code:
        query = query.where(KilnFiring.group_code == group_code)
    return [firing_dict(f) for f in db.scalars(query)]


@router.post('/firings', status_code=201)
def upsert_firing(data: FiringInput, db=Depends(get_db), u=Depends(can_write)):
    firing = accounting.upsert_firing(db, data, u)
    db.commit()
    return firing_dict(firing)


# ---------------------------------- 结账单 ---------------------------------- #

@router.get('/settlements')
def settlements(db=Depends(get_db), u=Depends(current_user)):
    return [settlement_summary(s) for s in db.scalars(
        select(Settlement).order_by(Settlement.id.desc()).limit(200))]


@router.post('/settlements', status_code=201)
def create_settlement(data: SettlementInput, db=Depends(get_db), u=Depends(can_write)):
    settlement = accounting.create_settlement(db, data, u)
    db.commit()
    return settlement_detail(settlement, db)


@router.get('/settlements/{id}')
def settlement(id: int, db=Depends(get_db), u=Depends(current_user)):
    settlement = db.get(Settlement, id)
    if not settlement:
        raise HTTPException(404, '结账单不存在')
    return settlement_detail(settlement, db)


@router.post('/settlements/{id}/recompute')
def recompute(id: int, db=Depends(get_db), u=Depends(can_write)):
    settlement = db.get(Settlement, id)
    if not settlement:
        raise HTTPException(404, '结账单不存在')
    accounting.recompute(db, settlement, u, reason='用户手动重算')
    db.commit()
    return settlement_detail(settlement, db)


@router.post('/settlements/{id}/close')
def close(id: int, data: CloseInput | None = None, db=Depends(get_db), u=Depends(admin_only)):
    settlement = db.get(Settlement, id)
    if not settlement:
        raise HTTPException(404, '结账单不存在')
    payload = data or CloseInput()
    settlement = accounting.close(
        db, settlement, u, request_id=payload.request_id,
        expected_revision=payload.expected_revision,
        expected_fingerprint=payload.expected_fingerprint)
    db.refresh(settlement)
    return settlement_detail(settlement, db)


@router.post('/settlements/{id}/resume')
def resume(id: int, db=Depends(get_db), u=Depends(admin_only)):
    settlement = db.get(Settlement, id)
    if not settlement:
        raise HTTPException(404, '结账单不存在')
    settlement = accounting.resume(db, settlement, u)
    db.refresh(settlement)
    return settlement_detail(settlement, db)


@router.post('/settlements/{id}/corrections', status_code=201)
def correction(id: int, data: CorrectionInput, db=Depends(get_db), u=Depends(admin_only)):
    settlement = db.get(Settlement, id)
    if not settlement:
        raise HTTPException(404, '结账单不存在')
    correction = accounting.record_correction(db, settlement, data, u)
    db.commit()
    rows = db.scalars(select(CorrectionRow).where(
        CorrectionRow.correction_id == correction.id).order_by(CorrectionRow.id)).all()
    return correction_dict(correction, rows)


@router.get('/corrections')
def corrections(db=Depends(get_db), u=Depends(current_user)):
    data = []
    for correction in db.scalars(select(Correction).order_by(Correction.id.desc()).limit(200)):
        rows = db.scalars(select(CorrectionRow).where(
            CorrectionRow.correction_id == correction.id).order_by(CorrectionRow.id)).all()
        data.append(correction_dict(correction, rows))
    return data
