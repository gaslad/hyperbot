# SimpleX Implementation

Last updated: 2026-04-13

Hyperbot now uses a single reporting pipeline that generates the report, validates
it, stages it durably, and only then sends the plain-text summary through the
local `simplex-chat` CLI.

## Stable Entry Points

- `scripts/daily_simplex_report.py` - daily automation wrapper
- `scripts/report_pipeline.py` - main orchestrator for daily or weekly runs
- `scripts/send_simplex_report.py` - strict sender used by the orchestrator

## Current Flow

1. `scripts/report_pipeline.py` locates the active workspace.
2. It syncs fresh fills when credentials and write access are available.
3. It loads `data/trade_journal.jsonl`, pairs fills with nearby intent logs, and
   refuses to invent missing intent data.
4. It renders Markdown plus a SimpleX plain-text summary.
5. It validates the output before sending.
6. It writes the report to the workspace when possible, otherwise to a temp
   staging directory under `/tmp/hyperbot-reports/...`.
7. It sends the validated plain-text report through `scripts/send_simplex_report.py`.

## Environment Variables

- `HYPERBOT_WORKSPACE` - override workspace discovery
- `HYPERBOT_SIMPLEX_DB_PREFIX` - local SimpleX profile prefix
- `HYPERBOT_SIMPLEX_FALLBACK_DB_PREFIX` - writable fallback profile prefix used when the primary path cannot be opened or written
- `HYPERBOT_SIMPLEX_CONTACT` - local contact name
- `HYPERBOT_SIMPLEX_BINARY` - path or name of the `simplex-chat` binary
- `HYPERBOT_SIMPLEX_PORTABLE_ROOT` - temp root used when the sender stages a portable `simplex-chat` copy
- `HYPERBOT_SIMPLEX_OPENSSL_LIB_DIR` - override directory containing compatible `libcrypto.3.dylib` and `libssl.3.dylib`
- `HYPERBOT_SIMPLEX_OPENSSL_LIBCRYPTO` / `HYPERBOT_SIMPLEX_OPENSSL_LIBSSL` - explicit library paths for the portable binary fallback
- `HYPERBOT_REPORT_PATH` - explicit report file path for direct sender runs

## Reliability Behavior

- If fills or credentials are unavailable, the pipeline produces a failure note
  instead of fabricating a report.
- If the workspace is not writable, it falls back to temp staging and still keeps
  the report artifact.
- If the SimpleX profile database is read-only, missing, or not writable, the
  sender retries once against a writable temp-backed fallback profile after
  seeding it from the readable primary profile files when available.
- If the configured `simplex-chat` binary depends on missing or incompatible
  OpenSSL libraries, the sender stages a portable temp copy, copies in a
  compatible `libcrypto.3.dylib` / `libssl.3.dylib` pair, rewrites the binary
  to use that temp library directory, and retries with the portable copy.
- Transient sender failures are retried; permanent configuration problems are not.

## Operational Notes

- The report generator should run on a schedule from a stable entrypoint, not
  from the dashboard process.
- The sender assumes the target contact exists in the local SimpleX profile.
- For machine portability, keep the profile path and contact name in environment
  variables rather than hard-coded defaults.
