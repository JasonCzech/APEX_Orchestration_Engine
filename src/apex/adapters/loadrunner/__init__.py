"""LoadRunner Enterprise execution-engine adapter (LRE REST API).

Importing this package registers the "loadrunner" execution-engine provider
with the AdapterRegistry, mirroring apex.adapters.s3.
"""

from apex.adapters.loadrunner.engine import LoadRunnerExecutionEngine

__all__ = ["LoadRunnerExecutionEngine"]
