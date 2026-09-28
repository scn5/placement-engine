from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, schemas
from app.database import get_db

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
    db: AsyncSession = Depends(get_db),
) -> List[schemas.CompanyResponse]:
    """Return all companies ordered by name. Optionally filter by `tracking_mode`."""
    companies = await crud.get_companies(db, tracking_mode=tracking_mode)
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
    db_track = await crud.create_role_track(db, track_in)
    return schemas.RoleTrackResponse.model_validate(db_track)
