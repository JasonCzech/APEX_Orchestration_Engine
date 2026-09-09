# Validation — 2026-09-09

Both repositories contained existing changes, and a parallel task continued changing APEX Load during validation. No deployed native connection was available for a real cross-app launch/import/return round trip.

## Orchestration

- Production build and TypeScript checks pass.
- Dashboard lint: no errors; 22 existing Fast Refresh warnings.
- Python lint on changed implementation and test files passes.
- Shared event/assessment contract package: 46 tests pass, including the shared JSON fixture and unsupported-version/non-finite-duration rejection.
- Full backend unit run: 5,718 passed, 12 skipped, one projection assertion failed. Removed the unnecessary per-phase engine-handle projection; the subsequent 300-test run covering facade projection, structured outcomes, and the Load adapter passes.
- Full dashboard run: 888 passed, one asynchronous launch-navigation assertion failed. Changed it to await the rendered destination; the subsequent 28-test run covering launch navigation, release selection, outcome display helpers, and execution links passes.
- Browser inspection of the release-connection controls in the offline demo confirmed the layout and saved configuration. Screenshot: `output/playwright/cross-app-contracts/release-connection.png` in the Orchestration repository. Populated release selection and revision persistence are covered by the component test.

## APEX Load

- TypeScript, lint, and production build pass.
- Two-worker unit run: 4,000 passed, one new section-navigation fixture failed because no test was loaded. Corrected the fixture; all 48 report/navigation/outcome tests pass on recheck.
- Dev-admin browser profile: 76 passed, two Settings failures remain: returning to an unsaved Grafana settings form, and finding the Settings search control. The report and run-result journeys pass.
- Local-auth browser profile: all 9 pass on isolated port 18092. The first default-port attempt stopped during setup.
- Full Go validation was attempted twice. Generated Swagger and QA artifacts were refreshed. The later API suite hit its 10-minute timeout during substantial concurrent machine load, and a distributed-execution timing assertion failed. The outcome contract, affected execution tests, and Swagger freshness pass together with `GOMAXPROCS=2`; QA freshness passes separately.

## Remaining verification

The full Load suite cannot be described as clean. Resolve/recheck the two Settings workflows and repeat its full Go suite in a quiet environment. Configure an enabled, project-scoped Load connection, deploy Load's additive producer before Orchestration's consumer, and exercise a real mapped launch plus native import and return navigation before production rollout.

## Subsequent code review

The follow-up [code review](CODE_REVIEW.md) fixed five findings. Its full Load dashboard cycle passed, including both previously failing Settings workflows. The deployed cross-app handoff and earlier full Go-suite limitation remain unverified.
