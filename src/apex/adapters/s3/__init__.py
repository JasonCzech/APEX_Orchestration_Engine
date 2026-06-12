"""S3-compatible object store adapters (dev MinIO; any S3 endpoint in prod).

Importing this package registers the "s3" artifact-store provider with the
AdapterRegistry, mirroring apex.adapters.stubs / apex.adapters.sim_engine.
"""

from apex.adapters.s3.artifact_store import S3ArtifactStore

__all__ = ["S3ArtifactStore"]
