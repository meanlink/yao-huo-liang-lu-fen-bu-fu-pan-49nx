from pydantic import BaseModel, Field


class MeterInput(BaseModel):
    code: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=2, max_length=160)
    group_code: str = Field(min_length=1, max_length=80)
    reading_interval_seconds: int = Field(default=86400, ge=60, le=2592000)


class ReadingInput(BaseModel):
    reading_at: str | float
    value_kwh: float
    revision: int | None = Field(default=None, ge=1)
    source: str = Field(default='upstream', max_length=60)


class LinkInput(BaseModel):
    old_meter_id: int
    new_meter_id: int
    at: str | float
    old_final_kwh: float
    new_initial_kwh: float
    note: str = Field(default='', max_length=300)


class FiringInput(BaseModel):
    batch: str = Field(min_length=1, max_length=80)
    group_code: str = Field(min_length=1, max_length=80)
    kiln_code: str = Field(min_length=1, max_length=60)
    start_at: str | float
    end_at: str | float
    loaded: int = Field(gt=0)
    passed: int = Field(ge=0)
    source: str = Field(default='upstream', max_length=60)


class SettlementInput(BaseModel):
    reference: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=2, max_length=160)
    group_code: str = Field(min_length=1, max_length=80)
    start_at: str | float
    end_at: str | float


class CloseInput(BaseModel):
    expected_revision: int | None = None
    expected_fingerprint: str | None = Field(default=None, max_length=64)
    request_id: str | None = Field(default=None, max_length=100)


class CorrectionInput(BaseModel):
    reason: str = Field(min_length=4, max_length=500)
    reference: str | None = Field(default=None, max_length=60)
    request_id: str | None = Field(default=None, max_length=100)
