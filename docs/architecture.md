# Hyperbot Architecture

## Goal

Generate a complete automated trading workspace from a small set of user inputs.

## Layers

1. Input collection
- project name
- symbols
- strategy set
- account mode
- leverage / capital allocation
- notification settings

2. Workspace generation
- copy template skeleton
- fill config files
- install strategy packs
- write operator docs and runbooks

3. Connectivity validation
- environment template
- Hyperliquid market mapping
- signed dry-run path
- smoke-test path

4. Operations bootstrap
- prepare operator commands and operating notes
- status/restart/stop commands
- notification configuration

5. Symbol-specific revision
- automatic second-stage review for the chosen symbol at workspace creation time
- default window: most recent 90 days
- rank installed packs for the symbol
- generate a token-specific revision artifact for each installed strategy pack
- keep pack defaults separate from token-specific revisions
- allow explicit reruns later through `scripts/profile_symbol_strategy.py`
- allow safe adoption through `scripts/apply_revision.py`

## Non-Goals

- do not become the runtime trading engine itself
- do not hardcode one strategy or one symbol
- do not auto-place live trades without a validation step
