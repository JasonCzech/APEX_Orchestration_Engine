# Production-runtime check (2026-10-23)

The review's live evidence came from `langgraph dev` (in-memory runtime). This check repeats a wizard launch on the production Compose stack: the licensed `langchain/langgraph-api` image with the Postgres runtime, Redis, the MinIO gateway, `migrate` and `bootstrap`.

**Build:** `main` plus the usage consumer name fix (`claude/usage-consumer-name`) and the status fix below, built with `langgraph build`. The stack ran as its own Compose project, so the dev database was not touched.

## Result

A Simulated launch with all seven phases run by agents and "Run automatically" completed **7/7** through the dashboard wizard, with test result **Passed** and 8 artifacts stored in MinIO.

| Check | Evidence | Result |
|---|---|---|
| Prompt manifest frozen at launch | The stored run config has `prompt_manifest.phases` for all 7 phases | Pass |
| Model admission on a `/v1` launch | One `model_run_admissions` row for the thread, with every agent phase pinned and `actor_id` set to the launching consumer | Pass |
| Run attribution | `apex_actor_id` in the run config is the launching consumer | Pass |
| Usage attribution | 7 `usage_events`, each with `consumer_id` and `consumer_name` set to the launching consumer's ID | Pass |
| Idempotent retry | The retry after the failure below adopted the same deterministic thread | Pass |

## Findings

1. **Every wizard launch failed on the production runtime: 503 "pipeline launch recovery unavailable". Fixed on `claude/runtime-http-status`.**
   - The Postgres runtime maps gRPC errors to `HTTPException` with `http.HTTPStatus` members. The global handler in `src/apex/app/errors.py` accepted only an exact `int` status, so every runtime 404/409/422/403/401 became a 500.
   - The facade's deterministic-thread lookup then read "thread not found" as a failure and refused the launch.
   - The in-memory runtime raises plain ints, which is why neither dev nor the test suite saw this.
   - The fix accepts `HTTPStatus` exactly; other `int` subclasses still fail closed.
2. **The Compose stack cannot migrate as shipped. Open.** `migrate`, `bootstrap` and Alembic's `env.py` accept plaintext connections only to loopback hosts, but `docker-compose.yaml` points them at `postgres:5432` with `ssl_mode: disable`. `migrate` exits with "schema compatibility check failed". The check worked around this by running both services in Postgres's network namespace, so they connect to `127.0.0.1`.
3. **The dashboard container never reports healthy. Open.** Its health check fetches `http://localhost:8080/config.json`. Inside the container, `localhost` resolves to `::1`, and nginx listens only on IPv4. The page serves normally, but `docker compose up --wait` fails.
4. **The pinned MinIO server image can no longer be pulled. Open.** `minio/minio:RELEASE.2024-07-16T23-46-41Z` is refused by Docker Hub and Quay. The check used a locally cached MinIO build.
5. **License key wording. Docs.**
   - `LANGGRAPH_CLOUD_LICENSE_KEY` must be a license token. A LangSmith API key placed there fails startup with "Not enough segments".
   - With that variable empty and `LANGSMITH_API_KEY` set, the runtime starts in lite mode.
   - The Compose header comment says the license key is required, but Compose does not pass `LANGSMITH_API_KEY` to the server.
6. **`consumer_name` shows the consumer ID, not its display name (`apex-admin`).** This is what the usage name fix intends: the run carries only the actor ID. Showing display names would need a lookup when usage is recorded or read.
