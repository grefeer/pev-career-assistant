from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.app.api.dependencies import (
    _get_db,
    get_current_device,
    get_current_user,
    get_device_service,
)
from backend.app.db.models import Device, User
from backend.app.services.devices import (
    DeviceNotFoundError,
    DeviceService,
    InvalidPairingTicketError,
)


router = APIRouter(prefix="/devices", tags=["devices"])


class PairRequest(BaseModel):
    code: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=120)
    public_key_pem: str = Field(min_length=1)


class HeartbeatRequest(BaseModel):
    version: str = Field(min_length=1, max_length=40)


def device_summary(device: Device, *, online: bool) -> dict[str, object]:
    return {
        "id": device.id,
        "name": device.name,
        "platform": device.platform.value,
        "status": device.status.value,
        "version": device.version,
        "paired_at": device.paired_at,
        "last_seen_at": device.last_seen_at,
        "online": online,
    }


@router.post("/pairing-tickets")
def create_pairing_ticket(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
    service: Annotated[DeviceService, Depends(get_device_service)],
) -> dict[str, str | datetime]:
    ticket = service.create_pairing_ticket(db, user_id=current_user.id)
    return {"code": ticket.code, "expires_at": ticket.expires_at}


@router.post("/pair")
def pair_device(
    body: PairRequest,
    db: Annotated[Session, Depends(_get_db)],
    service: Annotated[DeviceService, Depends(get_device_service)],
) -> dict[str, object]:
    try:
        issued = service.redeem_pairing_ticket(
            db,
            code=body.code,
            name=body.name,
            public_key_pem=body.public_key_pem,
        )
    except InvalidPairingTicketError:
        raise HTTPException(status_code=400, detail="配对码无效、已过期或已使用。") from None
    return {
        "device": device_summary(issued.device, online=False),
        "device_token": issued.plaintext_token,
    }


@router.get("")
def list_devices(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
    service: Annotated[DeviceService, Depends(get_device_service)],
) -> dict[str, object]:
    return {
        "devices": [
            device_summary(item.device, online=item.online)
            for item in service.list_for_user(db, current_user.id)
        ]
    }


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_device(
    device_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
    service: Annotated[DeviceService, Depends(get_device_service)],
) -> Response:
    try:
        service.revoke(db, user_id=current_user.id, device_id=device_id)
    except DeviceNotFoundError:
        raise HTTPException(status_code=404, detail="设备不存在。") from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me")
def current_device_summary(
    device: Annotated[Device, Depends(get_current_device)],
    service: Annotated[DeviceService, Depends(get_device_service)],
) -> dict[str, object]:
    return device_summary(
        device, online=service.redis.exists(f"device-online:{device.id}") == 1
    )


@router.post("/heartbeat")
def heartbeat(
    body: HeartbeatRequest,
    device_token: Annotated[str, Header(alias="X-Device-Token")],
    _device: Annotated[Device, Depends(get_current_device)],
    db: Annotated[Session, Depends(_get_db)],
    service: Annotated[DeviceService, Depends(get_device_service)],
) -> dict[str, str | int]:
    if service.heartbeat(db, device_token, version=body.version) is None:
        raise HTTPException(status_code=401, detail="设备令牌无效。")
    return {"status": "online", "expires_in": 90}
