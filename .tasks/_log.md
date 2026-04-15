# Task Log

2026-03-30T19:49 | claude | hyperbot | Fixed empty workspace creation, wrote session handoff for v2 dashboard testing
2026-03-31T18:30 | claude | hyperbot | Disabled RVOL filter, auto-enable live trading on card Go Live, passthrough dashboard risk% to blaze sizing
2026-03-31T18:47 | codex | hyperbot | Verified Go Live auto-enables live trading, fixed dashboard deadlock, and fixed trigger order triggerPx typing for Hyperliquid
2026-03-31T18:50 | codex | hyperbot | Restored blaze RVOL minimum to 0.5 and prepared the repo for push plus dashboard resync
2026-04-02T10:41 | codex | hyperbot | Added auto strategy routing and educational card explainers, relaunched the live dashboard, and refreshed the session handoff
2026-04-10T17:38 | codex | repo-collaboration | restored `.tasks/` with a protocol, assistant inboxes, and a dedicated handoff log
2026-04-15T16:30 | claude | hyperbot | Diagnosed and fixed SL cascade bug: removed double-rounding (position_manager + hl_client), added SL cooldown in dashboard
