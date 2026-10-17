"""Exercise the installed native run builder before its database write boundary."""

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest


def test_native_run_builder_preserves_server_actor_in_config_and_context(tmp_path: Path) -> None:
    # Native runtime configuration is process-global. Isolate its import and any
    # in-memory runtime files from the other unit tests and developer's server.
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(root / "src"),
            "DATABASE_URI": ":memory:",
            "REDIS_URI": "redis://127.0.0.1:1",
            "LANGGRAPH_RUNTIME_EDITION": "inmem",
            "APEX_DATABASE__URI": "postgresql+asyncpg://apex:apex@127.0.0.1:1/apex",
        },
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def _native_probe() -> None:
    import langgraph_api.models.run as native_run
    from langchain_core.runnables import RunnableConfig
    from langgraph_api.auth.custom import DotDict, ProxyUser
    from langgraph_api.utils import set_auth_ctx
    from langgraph_api.validation import sanitize_reserved_keys
    from langgraph_sdk import Auth

    from apex.app.security import _direct_langgraph_body_limit_error
    from apex.auth.handlers import on_threads_create_run, user_payload
    from apex.auth.identity import (
        RUNTIME_ACTOR_ID_KEY,
        ConsumerIdentity,
        ConsumerType,
        Role,
        ScopeRef,
    )
    from apex.domain.pipeline import Phase
    from apex.graphs.pipeline.configurable import PipelineConfigurable
    from apex.graphs.pipeline.gates import make_approval
    from apex.services.pipeline_public import public_pipeline_state

    for consumer_id, trusted_loopback in (("operator-one", True), ("operator-two", False)):
        for source in ("config", "context"):
            identity = ConsumerIdentity(
                consumer_id=consumer_id,
                name="Native test operator",
                consumer_type=ConsumerType.DASHBOARD,
                role=Role.OPERATOR,
                scopes=[ScopeRef(project_id="p1")],
            )
            user = ProxyUser(DotDict(user_payload(identity, trusted_loopback=trusted_loopback)))
            ctx = Auth.types.AuthContext(
                user=cast(Any, user), permissions=[], resource="threads", action="create_run"
            )
            configurable: dict[str, Any] = {"project_id": "p1"}
            if trusted_loopback:
                configurable[RUNTIME_ACTOR_ID_KEY] = consumer_id
            payload: Any = {
                "assistant_id": str(uuid4()),
                "multitask_strategy": "reject",
            }
            if trusted_loopback:
                payload["command"] = {"resume": {"gate-id": {"action": "approve"}}}
            else:
                payload["input"] = {"title": "Native run"}
            if source == "config":
                payload["config"] = {"configurable": configurable}
            else:
                payload["context"] = configurable
            assert (
                _direct_langgraph_body_limit_error(
                    "run_create", json.dumps(payload).encode(), trusted_loopback=trusted_loopback
                )
                is None
            )
            sanitize_reserved_keys(payload)
            if trusted_loopback:
                assert configurable[RUNTIME_ACTOR_ID_KEY] == consumer_id
            # The old transport must remain rejected; no credential-guard bypass.
            assert (
                _direct_langgraph_body_limit_error(
                    "run_create",
                    json.dumps(
                        {"config": {"configurable": {"langgraph_auth_user_id": consumer_id}}}
                    ).encode(),
                    trusted_loopback=True,
                )
                is not None
            )

            captured: dict[str, Any] = {}

            async def put(
                _conn: Any,
                assistant_id: Any,
                kwargs: dict[str, Any],
                *,
                ctx: Any = ctx,
                captured: dict[str, Any] = captured,
                consumer_id: str = consumer_id,
                **controls: Any,
            ) -> AsyncIterator[dict[str, Any]]:
                config = kwargs["config"]
                context = kwargs["context"]
                assert context is not config["configurable"]
                assert "langgraph_auth_user_id" not in context
                captured.update(config=config, context=context)
                await on_threads_create_run(
                    ctx=ctx,
                    value=cast(
                        Any,
                        {
                            "assistant_id": str(assistant_id),
                            "thread_id": str(controls["thread_id"]),
                            "metadata": controls["metadata"],
                            "kwargs": kwargs,
                        },
                    ),
                )
                # Assert the hook mutated the exact mappings captured by the
                # real create_valid_run, rather than an unobserved shallow copy.
                assert captured["config"]["configurable"][RUNTIME_ACTOR_ID_KEY] == consumer_id
                assert captured["context"][RUNTIME_ACTOR_ID_KEY] == consumer_id
                for runtime_config in (config, {"configurable": context}):
                    approval = make_approval(
                        Phase.STORY_ANALYSIS,
                        1,
                        "prompt_review",
                        "approve",
                        cast(RunnableConfig, runtime_config),
                    )
                    assert approval["actor"] == consumer_id
                durable = PipelineConfigurable.from_config(config).snapshot()
                assert RUNTIME_ACTOR_ID_KEY not in durable
                public = public_pipeline_state(
                    {"run_config": durable, RUNTIME_ACTOR_ID_KEY: consumer_id}
                )
                assert RUNTIME_ACTOR_ID_KEY not in public

                async def rows() -> AsyncIterator[dict[str, Any]]:
                    yield {"run_id": controls["run_id"]}

                return rows()

            set_auth_ctx(user, [])
            try:
                with pytest.MonkeyPatch.context() as patch:
                    patch.setattr(native_run.Runs, "put", put)
                    await native_run.create_valid_run(cast(Any, None), str(uuid4()), payload, {})
            finally:
                set_auth_ctx(None, None)
            assert captured


if __name__ == "__main__":
    asyncio.run(_native_probe())
