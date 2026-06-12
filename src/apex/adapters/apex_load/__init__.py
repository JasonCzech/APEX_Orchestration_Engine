"""APEX Load execution-engine adapter (Kubernetes-native internal load tester).

Importing this package registers the "apex_load" execution-engine provider with
the AdapterRegistry, mirroring apex.adapters.jira / apex.adapters.s3.
"""

from apex.adapters.apex_load.engine import ApexLoadExecutionEngine

__all__ = ["ApexLoadExecutionEngine"]
