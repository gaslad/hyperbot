# Hyperbot Release Readiness

Use the release gate before publishing or distributing the plugin:

```bash
python3 scripts/release_readiness.py
```

## What It Checks

- required project files exist
- core Python scripts compile
- `scripts/validate_apply_revision.py` passes
- `.codex-plugin/plugin.json` contains the expected release metadata

## Current Metadata Expectations

For a release-ready plugin manifest:
- `homepage` should be a real product or project URL
- `repository` should be the actual source repository URL
- `interface.websiteURL` should be the public plugin page or product site
- `interface.privacyPolicyURL` should be a dedicated privacy policy URL
- `interface.termsOfServiceURL` should be a dedicated terms URL

The release check intentionally blocks when those URLs are reused as placeholders.
