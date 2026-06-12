"""Elasticsearch/OpenSearch adapters (ELK wave-1, M4).

Importing this package registers the "elasticsearch" log-search provider with
the AdapterRegistry — same side-effect contract as apex.adapters.stubs and
apex.adapters.s3 (wired through apex/adapters/__init__.py at integration).
"""

from apex.adapters.elk.log_search import ElasticsearchLogSearchAdapter

__all__ = ["ElasticsearchLogSearchAdapter"]
