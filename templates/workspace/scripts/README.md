# Scripts

This directory contains runner, validation, and research commands for the generated workspace.

Unit tests for the workspace runtime live under `tests/` so runtime code and test code stay separate.

## Token-Specific Revision

Workspace creation runs the default token-specific revision automatically for the selected symbol and installed strategy packs.

To rerun it manually:

```bash
python3 scripts/profile_symbol_strategy.py --days 90
```

To target a specific strategy:

```bash
python3 scripts/profile_symbol_strategy.py --days 90 --strategy-id <strategy_id>
```

This produces:
- a symbol profile under `research/profiles/`
- token-specific revision files under `research/revisions/`

The revisions are separate from the baseline strategy-pack defaults.

## Apply a Revision

Preview the latest revision for an installed strategy:

```bash
python3 scripts/apply_revision.py --strategy-id <strategy_id>
```

Apply it and back up the existing config first:

```bash
python3 scripts/apply_revision.py --strategy-id <strategy_id> --apply
```

You can also target a specific revision file:

```bash
python3 scripts/apply_revision.py --revision research/revisions/<file>.json --apply
```
