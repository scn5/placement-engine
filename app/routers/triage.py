from typing import List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, models, schemas
from app.database import AsyncSessionLocal, get_db

router = APIRouter(prefix="/triage", tags=["Triage"])


def _to_triage_item(track: models.RoleTrack) -> schemas.TriageItemResponse:
    env_type = (
        track.company.environment_type
        if track.company and getattr(track.company, "environment_type", None)
        else "MAIN"
    )
    return schemas.TriageItemResponse(
        id=track.id,
        company_name=track.company.name if track.company else "",
        role_name=track.role_name,
        drive_type=track.drive_type,
        environment_type=env_type,
        ctc_stipend_info=track.ctc_stipend_info,
        latest_status_note=track.latest_status_note,
        created_at=track.created_at,
    )


@router.get(
    "/pending",
    response_model=List[schemas.TriageItemResponse],
    summary="Get pending triage role tracks",
)
@router.get(
    "",
    response_model=List[schemas.TriageItemResponse],
    summary="Get pending triage role tracks (alias)",
    include_in_schema=False,
)
async def get_pending_triage(
    env: Optional[str] = Query(
        default=None,
        description="Filter by feed environment: MAIN | TEST",
    ),
    db: AsyncSession = Depends(get_db),
) -> List[schemas.TriageItemResponse]:
    """Retrieve all role tracks awaiting triage (is_triaged == False),
    ordered by created_at DESC."""
    tracks = await crud.get_pending_triage_items(db, env=env)
    return [_to_triage_item(t) for t in tracks]


@router.post(
    "/{role_track_id}",
    response_model=schemas.TriageItemResponse,
    summary="Triage a role track",
)
async def triage_role_track(
    role_track_id: int,
    payload: schemas.TriageActionPayload,
    db: AsyncSession = Depends(get_db),
) -> schemas.TriageItemResponse:
    """Triage a role track by setting its tracking_mode to ELIGIBLE, SILENT_TRACK, or IGNORED,
    and strictly setting is_triaged = True."""
    track = await crud.triage_role_track(db, role_track_id, payload.action)
    if track is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Role track {role_track_id} not found.",
        )
    return _to_triage_item(track)


@router.post(
    "/{role_track_id}/accept",
    response_model=schemas.TriageItemResponse,
    summary="Accept a role track into ELIGIBLE",
)
async def accept_role_track(
    role_track_id: int,
    db: AsyncSession = Depends(get_db),
) -> schemas.TriageItemResponse:
    """Convenience endpoint to accept a role track directly into ELIGIBLE tracking mode."""
    return await triage_role_track(
        role_track_id=role_track_id,
        payload=schemas.TriageActionPayload(action="ELIGIBLE"),
        db=db,
    )


@router.post(
    "/{role_track_id}/ignore",
    response_model=schemas.TriageItemResponse,
    summary="Discard a role track into IGNORED",
)
async def ignore_role_track(
    role_track_id: int,
    db: AsyncSession = Depends(get_db),
) -> schemas.TriageItemResponse:
    """Convenience endpoint to discard a role track into IGNORED tracking mode."""
    return await triage_role_track(
        role_track_id=role_track_id,
        payload=schemas.TriageActionPayload(action="IGNORED"),
        db=db,
    )


# ---------------------------------------------------------------------------
# Re-triage Logic Utility
# ---------------------------------------------------------------------------
async def flag_for_retriage(
    role_track_id_or_db: Union[int, AsyncSession],
    note_or_role_track_id: Union[str, int],
    note_param: Optional[str] = None,
    db_param: Optional[AsyncSession] = None,
) -> Optional[models.RoleTrack]:
    """Flag a role track for re-triage by updating latest_status_note.

    Only if the current tracking_mode == 'SILENT_TRACK', it also sets
    is_triaged = False so it reappears in the pending triage queue.

    Supports both signatures:
      - flag_for_retriage(db, role_track_id, note)
      - flag_for_retriage(role_track_id, note, [db])
    """
    if isinstance(role_track_id_or_db, AsyncSession):
        session = role_track_id_or_db
        rt_id = int(note_or_role_track_id)
        note = note_param or ""
        return await crud.flag_for_retriage(session, rt_id, note)

    rt_id = int(role_track_id_or_db)
    note = str(note_or_role_track_id)
    if db_param is not None:
        return await crud.flag_for_retriage(db_param, rt_id, note)

    async with AsyncSessionLocal() as session:
        return await crud.flag_for_retriage(session, rt_id, note)
