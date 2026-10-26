from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, models, schemas
from app.database import get_db

router = APIRouter(prefix="/events", tags=["Timeline Events"])


async def _check_role_of_company(db: AsyncSession, role_track_id: Optional[int], company_id: int) -> None:
    """422 unless *role_track_id* is empty or one of the company's roles."""
    if role_track_id is None:
        return
    role = await db.get(models.RoleTrack, role_track_id)
    if role is None or role.company_id != company_id:
        raise HTTPException(
            status_code=422,
            detail=f"role_track_id {role_track_id} is not a role of company {company_id}.",
        )


@router.get(
    "/timeline",
    response_model=List[schemas.TimelineEventResponse],
    summary="Get timeline feed",
)
async def get_timeline(
    drive_type: str = Query(
        default="FTE",
        description="Drive type viewport: FTE | INTERNSHIP | COMBINED",
    ),
    env: Optional[str] = Query(
        default=None,
        description="Filter by feed environment: MAIN | TEST",
    ),
    db: AsyncSession = Depends(get_db),
) -> List[schemas.TimelineEventResponse]:
    """Return the active timeline feed filtered by drive_type and optional env, ordered by deadline."""
    events = await crud.get_timeline_events(db, drive_type=drive_type, env=env)
    return [schemas.TimelineEventResponse.model_validate(e) for e in events]


@router.post(
    "/",
    response_model=schemas.TimelineEventResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a custom timeline event",
)
async def create_event(
    event_in: schemas.TimelineEventCreate,
    db: AsyncSession = Depends(get_db),
) -> schemas.TimelineEventResponse:
    """Create a new custom stage or deadline event."""
    if await db.get(models.Company, event_in.company_id) is None:
        raise HTTPException(
            status_code=422,
            detail=f"company_id {event_in.company_id} does not exist.",
        )
    await _check_role_of_company(db, event_in.role_track_id, event_in.company_id)
    db_event = await crud.create_custom_event(db, event_in)
    return schemas.TimelineEventResponse.model_validate(db_event)


@router.patch(
    "/{event_id}/complete",
    response_model=schemas.TimelineEventResponse,
    summary="Toggle event completion",
)
async def complete_event(
    event_id: int,
    completed: bool = Query(default=True, description="True to mark complete, False to revert"),
    db: AsyncSession = Depends(get_db),
) -> schemas.TimelineEventResponse:
    """Mark an event as COMPLETED or revert it to PENDING."""
    event = await crud.set_event_completion(db, event_id, is_completed=completed)
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Event {event_id} not found.",
        )
    return schemas.TimelineEventResponse.model_validate(event)


@router.post(
    "/{event_id}/uncomplete",
    response_model=schemas.TimelineEventResponse,
    summary="Reopen / uncomplete an event",
)
@router.post(
    "/{event_id}/reopen",
    response_model=schemas.TimelineEventResponse,
    summary="Reopen / uncomplete an event (alias)",
    include_in_schema=False,
)
async def uncomplete_event(
    event_id: int,
    db: AsyncSession = Depends(get_db),
) -> schemas.TimelineEventResponse:
    """Reopen an event by reverting user_status to PENDING and completed_at to None."""
    event = await crud.reopen_timeline_event(db, event_id)
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Event {event_id} not found.",
        )
    return schemas.TimelineEventResponse.model_validate(event)


@router.patch(
    "/{event_id}/extend",
    response_model=schemas.TimelineEventResponse,
    summary="Extend an event deadline",
)
async def extend_event(
    event_id: int,
    payload: schemas.ExtendDeadlinePayload,
    db: AsyncSession = Depends(get_db),
) -> schemas.TimelineEventResponse:
    """Extend an event's end_time. A MISSED event naturally recovers to PENDING."""
    event = await crud.extend_event_deadline(db, event_id, payload.new_end_time)
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Event {event_id} not found.",
        )
    return schemas.TimelineEventResponse.model_validate(event)


@router.post(
    "/companies/{company_id}/replace-form",
    response_model=schemas.TimelineEventResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Replace the active form submission event",
)
async def replace_form(
    company_id: int,
    payload: schemas.ReplaceFormPayload,
    role_track_id: Optional[int] = Query(
        default=None, description="Scope replacement to a specific role track"
    ),
    db: AsyncSession = Depends(get_db),
) -> schemas.TimelineEventResponse:
    """Supersede the current FORM_SUBMISSION event with a new versioned entry."""
    if await db.get(models.Company, company_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Company {company_id} not found.",
        )
    await _check_role_of_company(db, role_track_id, company_id)
    new_event = await crud.replace_form_event(
        db, company_id=company_id, role_track_id=role_track_id, payload=payload
    )
    return schemas.TimelineEventResponse.model_validate(new_event)


@router.post(
    "/{event_id}/resolve-review",
    response_model=schemas.TimelineEventResponse,
    summary="Mark a flagged event as reviewed",
)
async def resolve_review(
    event_id: int,
    db: AsyncSession = Depends(get_db),
) -> schemas.TimelineEventResponse:
    """Clear needs_review (and review_reason) after the user has checked the event."""
    event = await crud.resolve_event_review(db, event_id)
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Event {event_id} not found.",
        )
    return schemas.TimelineEventResponse.model_validate(event)


@router.delete(
    "/{event_id}",
    summary="Soft-delete a timeline event",
)
async def delete_event(
    event_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Soft-delete a timeline event by setting is_deleted = True."""
    deleted = await crud.soft_delete_timeline_event(db, event_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Event {event_id} not found.",
        )
    return {"status": "deleted", "id": event_id}
