from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, models, schemas
from app.database import get_db
from sqlalchemy import select

router = APIRouter(prefix="/companies", tags=["Companies"])


@router.post(
    "/",
    response_model=schemas.CompanyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a company",
)
async def create_company(
    company_in: schemas.CompanyCreate,
    db: AsyncSession = Depends(get_db),
) -> schemas.CompanyResponse:
    """Create a company entry. Supports custom manual entries via `is_custom=True`."""
    db_company = await crud.create_company(db, company_in)
    return schemas.CompanyResponse.model_validate(db_company)


@router.get(
    "/",
    response_model=List[schemas.CompanyResponse],
    summary="List companies",
)
async def list_companies(
    tracking_mode: Optional[str] = Query(
        default=None,
        description="Filter by tracking mode: ELIGIBLE | SILENT_TRACK | IGNORED",
    ),
    env: Optional[str] = Query(
        default=None,
        description="Filter by feed environment: MAIN | TEST",
    ),
    db: AsyncSession = Depends(get_db),
) -> List[schemas.CompanyResponse]:
    """Return all companies ordered by name. Optionally filter by `tracking_mode` and/or `env`."""
    companies = await crud.get_companies(db, tracking_mode=tracking_mode, env=env)
    return [schemas.CompanyResponse.model_validate(c) for c in companies]


@router.patch(
    "/{company_id}/tracking",
    response_model=schemas.CompanyResponse,
    summary="Toggle company tracking mode",
)
async def toggle_tracking(
    company_id: int,
    payload: schemas.ToggleTrackingPayload,
    db: AsyncSession = Depends(get_db),
) -> schemas.CompanyResponse:
    """Update a company's tracking mode to ELIGIBLE, SILENT_TRACK, or IGNORED."""
    company = await crud.update_company_tracking(db, company_id, payload.tracking_mode)
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Company {company_id} not found.",
        )
    return schemas.CompanyResponse.model_validate(company)


@router.get(
    "/{company_id}/events",
    response_model=List[schemas.TimelineEventResponse],
    summary="List all timeline events for a company",
)
async def list_company_events(
    company_id: int,
    db: AsyncSession = Depends(get_db),
) -> List[schemas.TimelineEventResponse]:
    """Return every timeline event scoped to one company, ordered chronologically. Return 404 if not found."""
    check_stmt = select(models.Company.id).where(models.Company.id == company_id)
    res = await db.execute(check_stmt)
    if res.scalars().first() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Company {company_id} not found.",
        )
    events = await crud.get_company_events(db, company_id)
    return [schemas.TimelineEventResponse.model_validate(e) for e in events]


@router.post(
    "/{company_id}/tracks",
    response_model=schemas.RoleTrackResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a role track to a company",
)
async def add_role_track(
    company_id: int,
    track_in: schemas.RoleTrackCreate,
    db: AsyncSession = Depends(get_db),
) -> schemas.RoleTrackResponse:
    """Add a new FTE / Internship / Combined role track to an existing company."""
    if track_in.company_id != company_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Body company_id must match path company_id.",
        )
    try:
        db_track = await crud.create_role_track(db, track_in)
    except crud.TrackingLocked as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return schemas.RoleTrackResponse.model_validate(db_track)


@router.delete(
    "/{company_id}",
    summary="Soft-delete a company and its timeline events",
)
async def delete_company(
    company_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Soft-delete a company (is_active=False) and mark all its events as is_deleted=True."""
    deleted = await crud.soft_delete_company(db, company_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Company {company_id} not found.",
        )
    return {"status": "deleted", "id": company_id}
