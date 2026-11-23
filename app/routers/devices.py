from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import models, push, schemas
from app.database import get_db

router = APIRouter(tags=["Notifications"])


@router.post("/devices/register", response_model=schemas.DeviceResponse)
async def register_device(
    payload: schemas.DeviceRegister, db: AsyncSession = Depends(get_db)
):
    """Idempotent: registering a known token only refreshes last_registered_at."""
    query = select(models.DeviceToken).where(
        models.DeviceToken.expo_push_token == payload.expo_push_token
    )
    device = (await db.execute(query)).scalars().first()
    if device is None:
        device = models.DeviceToken(expo_push_token=payload.expo_push_token)
        db.add(device)
    else:
        device.last_registered_at = models.utc_now()
    try:
        await db.commit()
    except IntegrityError:  # registered concurrently
        await db.rollback()
        device = (await db.execute(query)).scalars().first()
    await db.refresh(device)
    return device


@router.post("/notify/test", response_model=schemas.NotifyTestResponse)
async def notify_test(db: AsyncSession = Depends(get_db)):
    """Send a test push to every registered device, to verify delivery end to end."""
    result = await push.push_to_all(
        db,
        title="Command Center",
        body="Test notification — push delivery works.",
        data={"type": "test"},
    )
    if result is None:
        raise HTTPException(status_code=409, detail="No device is registered for push notifications.")
    return schemas.NotifyTestResponse(
        devices=result.devices, sent=result.sent, failed=result.failed, removed=result.removed
    )
