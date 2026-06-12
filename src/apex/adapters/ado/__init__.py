"""Azure DevOps work-tracking adapter (REST 7.1, WIQL).

Importing this package registers the "ado" work-tracking provider with the
AdapterRegistry, mirroring apex.adapters.s3.
"""

from apex.adapters.ado.work_tracking import AdoWorkTrackingAdapter

__all__ = ["AdoWorkTrackingAdapter"]
