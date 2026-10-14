"""
scripts/clean_test_data.py
Database cleanup script: purges dummy/test companies from MAIN environment
and verifies database isolation.
"""
import asyncio
import logging
import os
import sys

# Ensure placement-engine root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, delete
from app.database import AsyncSessionLocal, engine
from app.models import Base, Company, RoleTrack, TimelineEvent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("clean_test_data")

DUMMY_PATTERNS = ["test", "dummy", "fallback", "mock", "acme"]


async def clean_database():
    async with AsyncSessionLocal() as session:
        # Fetch all companies
        result = await session.execute(select(Company))
        companies = result.scalars().all()

        to_delete = []
        for c in companies:
            is_dummy_main = (
                c.environment_type == "MAIN"
                and any(p in c.name.lower() for p in DUMMY_PATTERNS)
            )
            # Also purge any legacy sandbox/test artifacts with test titles in MAIN
            if is_dummy_main:
                to_delete.append(c)

        if not to_delete:
            logger.info("No dummy/test companies found in MAIN. Database is clean.")
        else:
            for c in to_delete:
                logger.info(
                    "Purging dummy company from MAIN: id=%s name=%r env=%s",
                    c.id, c.name, c.environment_type,
                )
                # Cascading delete of events & role tracks if needed
                await session.execute(
                    delete(TimelineEvent).where(TimelineEvent.company_id == c.id)
                )
                await session.execute(
                    delete(RoleTrack).where(RoleTrack.company_id == c.id)
                )
                await session.delete(c)

            await session.commit()
            logger.info("Purged %d dummy companies from MAIN successfully.", len(to_delete))

        # Verification report
        active_res = await session.execute(select(Company))
        remaining = active_res.scalars().all()
        logger.info("Current companies in database:")
        for rem in remaining:
            logger.info(
                "  • id=%s name=%r env=%s subchannel_id=%s active=%s",
                rem.id, rem.name, rem.environment_type, rem.subchannel_id, rem.is_active,
            )


if __name__ == "__main__":
    asyncio.run(clean_database())
