from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app import models, schemas
from app.database import get_db

router = APIRouter(prefix="/settings", tags=["Settings"])


@router.get("", response_model=schemas.SystemConfigResponse)
async def get_settings(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(models.SystemConfig))
    config = result.scalars().first()
    
    if not config:
        # User requested: "If no row exists, return an empty default payload or a 404."
        # We will return 404 so the client knows it needs to PUT for the first time
        # Or alternatively return a default instance. Let's return a default instance.
        # But wait, 404 is requested as an option, which makes more RESTful sense. Let's return 404.
        raise HTTPException(status_code=404, detail="Settings not found")
        
    return config


@router.put("", response_model=schemas.SystemConfigResponse)
async def update_settings(
    payload: schemas.SystemConfigUpdate,
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(models.SystemConfig))
    config = result.scalars().first()
    
    if not config:
        config = models.SystemConfig()
        db.add(config)
    
    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(config, key, value)
        
    await db.commit()
    await db.refresh(config)
    
    return config
