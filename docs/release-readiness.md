# Hyperbot Repo Readiness

Use the readiness gate before publishing or sharing the repo:

```bash
python3 scripts/release_readiness.py
```

## What It Checks

- required project files exist
- core Python scripts compile
- `scripts/validate_apply_revision.py` passes
- generated workspace templates remain assistant-agnostic
