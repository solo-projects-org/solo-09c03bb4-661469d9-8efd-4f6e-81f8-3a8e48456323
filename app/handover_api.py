"""HTTP layer for handover sheet batches and ticket lookups."""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import AliasChoices, BaseModel, Field

from .handovers import Change, HandoverConflict, Sheet, get_store

router = APIRouter()

_MONTH_HINT = "结算月，格式 YYYY-MM，例如 2026-10"


def _month_error(value: str) -> Optional[str]:
    value = value.strip()
    if len(value) != 7 or value[4] != "-" or not (
            value[:4].isdigit() and value[5:].isdigit()):
        return "必须是 YYYY-MM 格式"
    if not 1 <= int(value[5:]) <= 12:
        return "月份必须在 01-12 之间"
    return None


def _blank(value: str) -> bool:
    return not value.strip()


class SheetInput(BaseModel):
    """One handover sheet; both ends are (carrier, waybill number)."""

    handover_no: str = Field(..., description="交接单号（同一结算月内唯一）")
    carrier_a: str = Field(..., description="交接单 A 端承运商")
    waybill_no_a: str = Field(..., description="A 端运单号")
    carrier_b: str = Field(..., description="交接单 B 端承运商")
    waybill_no_b: str = Field(..., description="B 端运单号")

    def normalized(self) -> Sheet:
        return Sheet(
            handover_no=self.handover_no.strip(),
            carrier_a=self.carrier_a.strip(),
            waybill_no_a=self.waybill_no_a.strip(),
            carrier_b=self.carrier_b.strip(),
            waybill_no_b=self.waybill_no_b.strip(),
        )


class HandoverBatchRequest(BaseModel):
    settlement_month: str = Field(
        ...,
        validation_alias=AliasChoices("settlement_month", "month"),
        description=_MONTH_HINT,
    )
    sheets: list[SheetInput] = Field(
        default_factory=list,
        validation_alias=AliasChoices("sheets", "handovers"),
        description="新增或按交接单号更正的有效交接单；同批内交接单号不得重复",
    )
    revoke: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("revoke", "revoked"),
        description="要撤销的交接单号；可与录入/更正混在一批",
    )

    model_config = {
        "populate_by_name": True,
        "json_schema_extra": {
            "examples": [
                {
                    "settlement_month": "2026-10",
                    "sheets": [
                        {
                            "handover_no": "HJ-0001",
                            "carrier_a": "承运商甲",
                            "waybill_no_a": "YD-1001",
                            "carrier_b": "承运商乙",
                            "waybill_no_b": "YT-7001",
                        },
                        {
                            "handover_no": "HJ-0002",
                            "carrier_a": "承运商乙",
                            "waybill_no_a": "YT-7001",
                            "carrier_b": "承运商丙",
                            "waybill_no_b": "SF-9001",
                        },
                    ],
                    "revoke": [],
                },
                {
                    "settlement_month": "2026-10",
                    "sheets": [],
                    "revoke": ["HJ-0002"],
                },
            ]
        },
    }



class EndpointOut(BaseModel):
    carrier: str
    waybill_no: str


class TicketOut(BaseModel):
    ticket_id: str
    endpoints: list[EndpointOut]


class HandoverBatchResponse(BaseModel):
    settlement_month: str
    accepted_sheets: int = Field(..., description="本批实际写入的新增/更正交接单数")
    revoked_sheets: int = Field(..., description="本批实际撤销的交接单数")
    rebuilt_tickets: list[TicketOut] = Field(
        ..., description="本批重建的对照票；未受改动影响的票不在其中")


class LookupResponse(BaseModel):
    settlement_month: str
    carrier: str
    waybill_no: str
    ticket_id: Optional[str] = Field(
        None, description="同票标识；该号码没有有效交接单时为空")
    waybill_nos: list[EndpointOut] = Field(
        default_factory=list,
        description="同票各承运商的运单号；无对照时为空")


class ConflictDetail(BaseModel):
    carrier: str = Field(..., description="出现多个运单号的承运商")
    waybill_nos: list[str] = Field(..., description="该承运商在本票上的冲突运单号")
    handover_nos: list[str] = Field(
        default_factory=list, description="构成该票（含本次改动）的交接单号")
    message: str


class ErrorItem(BaseModel):
    location: Optional[str] = None
    message: str


def _validate(request: HandoverBatchRequest) -> list[dict]:
    """Semantic checks that need the whole batch; all problems reported at once."""
    errors: list[dict] = []
    if not request.sheets and not request.revoke:
        errors.append({"location": "sheets/revoke",
                       "message": "sheets 与 revoke 不能同时为空"})
    month_error = _month_error(request.settlement_month)
    if month_error:
        errors.append({"location": "settlement_month", "message": month_error})

    numbers: list[str] = []
    for index, sheet in enumerate(request.sheets):
        where = f"sheets[{index}]"
        fields = {
            "handover_no": sheet.handover_no,
            "carrier_a": sheet.carrier_a,
            "waybill_no_a": sheet.waybill_no_a,
            "carrier_b": sheet.carrier_b,
            "waybill_no_b": sheet.waybill_no_b,
        }
        for name, value in fields.items():
            if _blank(value):
                errors.append({"location": f"{where}.{name}", "message": "不能为空"})
        normalized = sheet.normalized()
        numbers.append(normalized.handover_no)
        if (normalized.carrier_a, normalized.waybill_no_a) == (
                normalized.carrier_b, normalized.waybill_no_b):
            errors.append({
                "location": where,
                "message": "交接单两端不能是同一承运商的同一运单号"
                           "（自环不构成交接路径）",
            })

    duplicates = sorted({n for n in numbers if n and numbers.count(n) > 1})
    if duplicates:
        errors.append({"location": "sheets",
                       "message": f"同一批内交接单号只能出现一次: {duplicates}"})

    revoked = []
    for index, number in enumerate(request.revoke):
        if _blank(number):
            errors.append({"location": f"revoke[{index}]", "message": "不能为空"})
        revoked.append(number.strip())
    overlap = sorted({n for n in numbers if n and n in set(revoked)})
    if overlap:
        errors.append({
            "location": "sheets/revoke",
            "message": f"同一交接单号不能在同一批内既录入/更正又撤销: {overlap}",
        })
    return errors


def _changes(request: HandoverBatchRequest) -> list[Change]:
    changes = [
        Change(handover_no=sheet.normalized().handover_no,
               sheet=sheet.normalized())
        for sheet in request.sheets
    ]
    changes.extend(
        Change(handover_no=number.strip(), sheet=None) for number in request.revoke
    )
    return changes


@router.post(
    "/handovers",
    response_model=HandoverBatchResponse,
    responses={
        400: {
            "description": "同票同承运商冲突（detail 逐项给出 carrier、waybill_nos、"
                           "handover_nos）或批次参数不合法；整批不生效",
        },
    },
    summary="按交接单号批量录入、更正或撤销一个结算月的交接单",
)
def apply_handovers(request: HandoverBatchRequest) -> HandoverBatchResponse:
    errors = _validate(request)
    if errors:
        raise HTTPException(400, errors)
    try:
        result = get_store().apply_batch(
            request.settlement_month.strip(), _changes(request))
    except HandoverConflict as exc:
        raise HTTPException(400, [{
            "carrier": conflict.carrier,
            "waybill_nos": list(conflict.waybill_nos),
            "handover_nos": list(conflict.handover_nos),
            "message": (
                f"同票在承运商 {conflict.carrier} 只能有一个运单号，"
                f"候选为 {', '.join(conflict.waybill_nos)}"),
        } for conflict in exc.conflicts])
    return HandoverBatchResponse(
        settlement_month=result.settlement_month,
        accepted_sheets=result.sheets_upserted,
        revoked_sheets=result.sheets_revoked,
        rebuilt_tickets=[
            TicketOut(
                ticket_id=ticket.ticket_id,
                endpoints=[
                    EndpointOut(carrier=e.carrier, waybill_no=e.waybill_no)
                    for e in ticket.endpoints
                ],
            )
            for ticket in result.rebuilt_tickets
        ],
    )


@router.get(
    "/mapping",
    response_model=LookupResponse,
    responses={400: {"description": "查询参数不合法（detail 逐项给出 location、message）"}},
    summary="凭结算月、承运商和运单号查询同票各家的运单号（只读对照）",
)
def lookup_mapping(
    settlement_month: Annotated[str, Query(
        ..., description=_MONTH_HINT, examples=["2026-10"])],
    carrier: Annotated[str, Query(..., description="承运商")],
    waybill_no: Annotated[str, Query(..., description="运单号")],
) -> LookupResponse:
    month, carrier, waybill_no = (
        settlement_month.strip(), carrier.strip(), waybill_no.strip())
    errors: list[dict] = []
    month_error = _month_error(month)
    if month_error:
        errors.append({"location": "settlement_month", "message": month_error})
    if not carrier:
        errors.append({"location": "carrier", "message": "不能为空"})
    if not waybill_no:
        errors.append({"location": "waybill_no", "message": "不能为空"})
    if errors:
        raise HTTPException(400, errors)
    ticket = get_store().lookup(month, carrier, waybill_no)
    return LookupResponse(
        settlement_month=month,
        carrier=carrier,
        waybill_no=waybill_no,
        ticket_id=ticket.ticket_id if ticket else None,
        waybill_nos=[
            EndpointOut(carrier=e.carrier, waybill_no=e.waybill_no)
            for e in (ticket.endpoints if ticket else ())
        ],
    )
