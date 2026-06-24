"""Concrete adapters behind the ports (ADR-0002).

Adapters register their factories with the AdapterRegistry on import. Keep the
bulk registration behind an explicit function so importing low-level helpers
such as apex.adapters.registry does not eagerly import every provider.
"""

_registered = False


def register_builtin_adapters() -> None:
    """Import bundled adapter packages for their AdapterRegistry side effects."""
    global _registered
    if _registered:
        return
    import apex.adapters.ado  # noqa: F401  (registers the "ado" work-tracking provider)
    import apex.adapters.apex_load  # noqa: F401  (registers the "apex_load" execution-engine provider)
    import apex.adapters.elk  # noqa: F401  (registers the "elasticsearch" log-search provider)
    import apex.adapters.jira  # noqa: F401  (registers the "jira" work-tracking provider)
    import apex.adapters.k8s  # noqa: F401  (registers the "kubernetes" cluster-inventory provider)
    import apex.adapters.loadrunner  # noqa: F401  (registers the "loadrunner" execution-engine provider)
    import apex.adapters.s3  # noqa: F401  (registers the "s3" artifact-store provider)
    import apex.adapters.sim_engine  # noqa: F401  (registers the "sim" execution-engine provider)
    import apex.adapters.stubs  # noqa: F401  (registers built-in stub providers)

    _registered = True
