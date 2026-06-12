"""Concrete adapters behind the ports (ADR-0002).

Import apex.adapters.stubs / apex.adapters.sim_engine (or a future real-provider
package) to register their factories with the AdapterRegistry. Real-provider
packages with no extra wiring needs are imported here so any
`import apex.adapters.<subpackage>` registers them as a side effect (M3: s3).
"""

import apex.adapters.s3  # noqa: F401  (registers the "s3" artifact-store provider)
