"""
scripts/clean_test_data.py
Removes TEST-environment companies (with their roles and timeline events).

Safety (Phase 0.6):
  • Only companies with environment_type == "TEST" are ever touched — MAIN data
    is never deleted, whatever its name.
  • A dry run is the default: it lists what would be deleted. Pass --apply to
    delete.
  • --name narrows the selection to companies whose name contains the text.

Usage (from placement-engine/):
    .\\venv\\Scripts\\python.exe scripts/clean_test_data.py
    .\\venv\\Scripts\\python.exe scripts/clean_test_data.py --name acme --apply
"""
import argparse
import asyncio
import logging
import os
import sys
from typing import List, Optional

# Ensure placement-engine root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import delete, func, select

from app import database
from app.models import Company, RoleTrack, TimelineEvent

logger = logging.getLogger("clean_test_data")

TEST_ENVIRONMENT = "TEST"


async def find_test_companies(db, name_filter: Optional[str] = None) -> List[Company]:
    """TEST-environment companies, optionally only those whose name contains *name_filter*."""
    query = select(Company).where(Company.environment_type == TEST_ENVIRONMENT).order_by(Company.id)
    if name_filter:
        query = query.where(func.lower(Company.name).contains(name_filter.strip().lower()))
    return list((await db.execute(query)).scalars().all())


async def delete_companies(db, companies: List[Company]) -> int:
    """Delete *companies* with their events and roles. Refuses anything not TEST."""
    if any(c.environment_type != TEST_ENVIRONMENT for c in companies):
        raise ValueError("Refusing to delete a company outside the TEST environment.")
    ids = [c.id for c in companies]
    if not ids:
        return 0
    await db.execute(delete(TimelineEvent).where(TimelineEvent.company_id.in_(ids)))
    await db.execute(delete(RoleTrack).where(RoleTrack.company_id.in_(ids)))
    await db.execute(delete(Company).where(Company.id.in_(ids), Company.environment_type == TEST_ENVIRONMENT))
    await db.commit()
    return len(ids)


async def run(apply: bool = False, name_filter: Optional[str] = None) -> List[Company]:
    """List (and with *apply*, delete) the selected TEST companies; returns them."""
    async with database.AsyncSessionLocal() as db:
        companies = await find_test_companies(db, name_filter)
        for company in companies:
            logger.info(
                "%s TEST company id=%s name=%r subchannel_id=%s active=%s",
                "Deleting" if apply else "Would delete",
                company.id, company.name, company.subchannel_id, company.is_active,
            )
        if not companies:
            logger.info("No TEST companies match; nothing to do.")
        elif apply:
            deleted = await delete_companies(db, companies)
            logger.info("Deleted %d TEST company(ies) with their roles and events.", deleted)
        else:
            logger.info("Dry run: nothing was deleted. Re-run with --apply to delete these %d company(ies).", len(companies))
        return companies


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Remove TEST-environment companies (dry run unless --apply).")
    parser.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    parser.add_argument("--name", help="only companies whose name contains this text")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(run(apply=args.apply, name_filter=args.name))
    finally:
        asyncio.run(database.engine.dispose())


if __name__ == "__main__":
    main()
