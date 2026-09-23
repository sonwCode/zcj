from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlmodel import Session

from customer_portal_api.app.deps import get_db_session
from customer_portal_api.app.security import verify_payment_callback
from customer_portal_api.app.services.portal import PortalService


router = APIRouter(prefix="/payment", tags=["payment"])


class PaymentCallbackRequest(BaseModel):
    payment_no: str | None = None
    order_no: str | None = None
    status: str = "success"
    channel_trade_no: str | None = None
    amount: float | None = None
    payload: dict = Field(default_factory=dict)


@router.post("/callback/{channel_code}")
async def payment_callback(
    channel_code: str,
    request: Request,
    session: Session = Depends(get_db_session),
):
    # Verify the channel signature over the raw body before trusting anything.
    raw_body = await request.body()
    verify_payment_callback(
        channel_code,
        raw_body,
        request.headers.get("x-portal-signature", ""),
    )
    try:
        raw_json = json.loads(raw_body or b"{}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="回调内容不是合法 JSON") from exc
    if not isinstance(raw_json, dict):
        raise HTTPException(status_code=400, detail="回调内容必须是 JSON 对象")
    body = PaymentCallbackRequest(**raw_json)
    data = body.model_dump()
    # Keep provider-specific keys (e.g. amount) that the model does not declare,
    # otherwise validation below would silently never see them.
    for extra_key, extra_value in raw_json.items():
        data.setdefault(extra_key, extra_value)
    if data.get("payload") and isinstance(data["payload"], dict):
        merged = dict(data["payload"])
        merged.update({k: v for k, v in data.items() if k != "payload"})
        data = merged
    return PortalService(session).handle_payment_callback(channel_code, data)
