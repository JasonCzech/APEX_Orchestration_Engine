"""Jira Cloud work-tracking adapter (REST API v3).

Importing this package registers the "jira" work-tracking provider with the
AdapterRegistry, mirroring apex.adapters.s3.
"""

from apex.adapters.jira.work_tracking import JiraWorkTrackingAdapter

__all__ = ["JiraWorkTrackingAdapter"]
