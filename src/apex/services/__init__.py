"""Application services: orchestration-free glue between routers/graph nodes and
ports/persistence. Import service modules directly (no re-exports here) so that
loading one service never drags in another's dependencies.
"""
