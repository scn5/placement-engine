from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, schemas
from app.database import get_db

router = APIRouter(prefix="/events", tags=["Timeline Events"])


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
    new_event = await crud.replace_form_event(
        db, company_id=company_id, role_track_id=role_track_id, payload=payload
    )
    return schemas.TimelineEventResponse.model_validate(new_event)
