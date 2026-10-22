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
            "PYTHONPATH": os.pathsep.join((str(root / "src"), str(root))),
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
    from copy import deepcopy

    import langgraph_api.auth.custom as native_auth
    import langgraph_api.models.run as native_run
    from langchain_core.runnables import RunnableConfig
    from langgraph_api.auth.custom import DotDict, ProxyUser
    from langgraph_api.utils import set_auth_ctx
    from langgraph_api.validation import sanitize_reserved_keys
    from langgraph_runtime_inmem.database import GLOBAL_STORE
    from langgraph_sdk import Auth

    from apex.app.security import _direct_langgraph_body_limit_error
    from apex.auth.handlers import (
        _SYSTEM_ASSISTANT_GRAPHS,
        _load_native_launch_defaults,
        auth,
        on_threads_create_run,
        user_payload,
    )
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
    from tests.unit.test_auth_handlers import public_pipeline

    async def admit_test_models(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        # This subprocess tests native attribution, while pricing admission has
        # its own database-backed coverage. Keep the native probe hermetic too.
        return {}

    async def prepare_test_manifest(configurable, _identity, _run_input):
        selected = configurable["prompt_selections"]
        assert configurable["engine"] == "apex_load"
        assert configurable["environment_target_version"] == 7
        configurable["prompt_manifest"] = {
            "schema_version": 1,
            "system": {
                "prompt_id": selected["system"]["prompt_id"],
                "version_id": selected["system"]["version_id"],
                "namespace": "system",
                "key": "shared",
                "version": 2,
                "content": "Frozen shared system",
            },
            "phases": {
                "execution": {
                    "prompt_id": selected["phases"]["execution"]["prompt_id"],
                    "version_id": selected["phases"]["execution"]["version_id"],
                    "namespace": "phase",
                    "phase": "execution",
                    "key": "execution",
                    "version": 3,
                    "content": "Frozen execution context",
                }
            },
            "application": [],
            "environment": [],
        }

    analysis_checks = []

    async def check_analysis_readiness(cfg):
        # The real builder injects auth/tracing keys into configurable. The
        # readiness boundary must receive only validated application fields.
        assert isinstance(cfg, PipelineConfigurable)
        assert cfg.analysis is not None
        assert cfg.analysis.selections[0].tool == "internal_rca"
        assert cfg.analysis.selections[0].connection_id == "rca-connection"
        assert cfg.project_id == "p1"
        analysis_checks.append(cfg)

    for consumer_id, trusted_loopback, stale_defaults in (
        ("operator-one", True, False),
        ("operator-two", False, False),
        ("operator-three", False, True),
    ):
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
            assistant_id = uuid4()
            thread_id = uuid4()
            payload: Any = {
                "assistant_id": str(assistant_id),
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
                trusted_loopback: bool = trusted_loopback,
                stale_defaults: bool = stale_defaults,
                **controls: Any,
            ) -> AsyncIterator[dict[str, Any]]:
                config = kwargs["config"]
                context = kwargs["context"]
                assert context is not config["configurable"]
                assert "langgraph_auth_user_id" not in context
                captured.update(config=config, context=context)
                # Runs.put has already read an assistant before invoking auth.
                # Reproduce an administrator removing those defaults before the
                # hook's authorized lookup sees the updated record.
                cached_defaults = deepcopy(GLOBAL_STORE["assistants"][-1]["config"]["configurable"])
                if stale_defaults:
                    GLOBAL_STORE["assistants"][-1]["config"]["configurable"] = {}
                    GLOBAL_STORE["assistants"][-1]["context"] = {}
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
                if stale_defaults:
                    for effective in (config["configurable"], context):
                        assert set(PipelineConfigurable.model_fields) <= effective.keys()
                        merged_after_hook = {**cached_defaults, **effective}
                        assert merged_after_hook["engine"] == "sim"
                        assert merged_after_hook["phases"] is None
                        assert merged_after_hook["prompt_selections"] is None
                        assert merged_after_hook["prompt_manifest"] is None
                        assert merged_after_hook["environment_target"] is None
                elif not trusted_loopback:
                    for effective in (config["configurable"], context):
                        assert effective["engine"] == "apex_load"
                        assert effective["phases"] == ["execution"]
                        assert effective["environment_target"] == "https://approved.example.test"
                        assert effective["prompt_manifest"]["system"]["version_id"] == "system-v2"
                        assert effective["prompt_manifest"]["phases"]["execution"]["version"] == 3
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
                    patch.setattr("apex.services.model_pricing.admit_models", admit_test_models)
                    patch.setattr(native_auth, "get_auth_instance", lambda: auth)
                    patch.setattr(
                        "apex.services.prompt_selections.prepare_prompt_manifest",
                        prepare_test_manifest,
                    )
                    patch.setattr(
                        "apex.services.analysis_readiness.require_analysis_readiness",
                        check_analysis_readiness,
                    )
                    store = cast(Any, public_pipeline).__wrapped__(patch)
                    if not trusted_loopback:
                        store.attach(
                            {
                                "thread_id": str(thread_id),
                                "kwargs": {"config": {"configurable": configurable}},
                            }
                        )
                    defaults = {
                        "engine": "apex_load",
                        "phases": ["execution"],
                        "analysis": {
                            "schema_version": 1,
                            "selections": [
                                {"tool": "internal_rca", "connection_id": "rca-connection"}
                            ],
                        },
                        "prompt_selections": {
                            "schema_version": 1,
                            "system": {"prompt_id": "system-p", "version_id": "system-v2"},
                            "phases": {
                                "execution": {
                                    "prompt_id": "execution-p",
                                    "version_id": "execution-v3",
                                }
                            },
                            "application": [],
                            "environment": [],
                        },
                    }
                    GLOBAL_STORE["assistants"].append(
                        {
                            "assistant_id": assistant_id,
                            "graph_id": "pipeline",
                            "config": {"configurable": deepcopy(defaults)},
                            "context": deepcopy(defaults),
                            "metadata": {"project_id": "p1", "created_by": "administrator"},
                        }
                    )
                    GLOBAL_STORE["threads"].append(
                        {
                            "thread_id": thread_id,
                            "config": {},
                            "metadata": {"project_id": "p1", "app_id": "a1"},
                            "status": "idle",
                        }
                    )
                    await native_run.create_valid_run(cast(Any, None), str(thread_id), payload, {})
                    if not trusted_loopback:
                        # Exercise actual storage filtering, without replacing the
                        # hydration helper or assistant read authorization.
                        GLOBAL_STORE["assistants"][-1]["metadata"]["project_id"] = "p2"
                        with pytest.raises(Auth.exceptions.HTTPException) as error:
                            await _load_native_launch_defaults(
                                ctx, str(assistant_id), str(thread_id)
                            )
                        assert error.value.status_code == 404
                        assert "p2" not in str(error.value.detail)
                        system_id = next(
                            key
                            for key, graph in _SYSTEM_ASSISTANT_GRAPHS.items()
                            if graph == "pipeline"
                        )
                        from uuid import UUID

                        GLOBAL_STORE["assistants"].append(
                            {
                                "assistant_id": UUID(system_id),
                                "graph_id": "pipeline",
                                "config": {},
                                "context": {},
                                "metadata": {"created_by": "system"},
                            }
                        )
                        assert await _load_native_launch_defaults(ctx, system_id, None) == (
                            "pipeline",
                            {},
                            {},
                        )
            finally:
                set_auth_ctx(None, None)
            assert captured
    assert len(analysis_checks) == 2


if __name__ == "__main__":
    asyncio.run(_native_probe())
