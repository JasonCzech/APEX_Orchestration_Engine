"""Pipeline graph state.

M0 placeholder: the full PipelineState (phase_results with per-phase deep-merge
reducers, artifacts, dialogue, engine_handle, ...) lands in M1 per the rebuild plan.
"""

from typing import TypedDict


class PipelineState(TypedDict, total=False):
    request: str
    plan: str
    summary: str
