"""Concrete adapters behind the ports (ADR-0002).

Import apex.adapters.stubs / apex.adapters.sim_engine (or a future real-provider
package) to register their factories with the AdapterRegistry. Real-provider
packages with no extra wiring needs are imported here so any
`import apex.adapters.<subpackage>` registers them as a side effect (M3: s3).
"""

import apex.adapters.ado  # noqa: F401  (registers the "ado" work-tracking provider)
import apex.adapters.elk  # noqa: F401  (registers the "elasticsearch" log-search provider)
import apex.adapters.jira  # noqa: F401  (registers the "jira" work-tracking provider)
import apex.adapters.k8s  # noqa: F401  (registers the "kubernetes" cluster-inventory provider)
import apex.adapters.s3  # noqa: F401  (registers the "s3" artifact-store provider)
