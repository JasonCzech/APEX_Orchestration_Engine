"""Read-side repository for exact durable artifact ownership records."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apex.persistence.models import ArtifactReference


class ArtifactReferencesRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_exact(self, artifact_key: str) -> ArtifactReference | None:
        return await self._session.scalar(
            select(ArtifactReference).where(ArtifactReference.artifact_key == artifact_key)
        )
