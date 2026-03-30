# Next — Hyperbot Session Handoff

Last updated: 2026-03-30

## Just Completed
- Rebuilt dashboard from bento-grid to 3-column layout (signals/chart+thesis/position)
- Added SL/TP/entry overlay on SVG chart, R-multiple progress bar, condition checklist
- Built landing page (`website/index.html`) — Brutalist Signal aesthetic, all sections
- Created Netlify project `hyperbot-landing` (ID: 7a8e6d3d-4864-4c60-8928-6593a8e3429b)
- Added deploy script `scripts/deploy.sh`
- Updated AGENTS.md with website and dashboard sections

## Pending — Ready to Pick Up
1. **Deploy landing page** — run `./scripts/deploy.sh` (or drag-drop `website/` at Netlify)
2. **Post-entry plan tracking** — persist originating signal's plan into PairState after trade execution so thesis survives beyond the signal firing moment
3. **Auto-add live pairs** — in the poll loop, detect HL positions for coins not in `self.pairs` and auto-create PairState entries
4. **Update .task-scoper-map.md** — add `website/` area entry

## Blockers
- Cowork sandbox blocks npm registry and direct API calls to Netlify — deploy must happen via user CLI or pre-logged browser

## Decisions Made
- Dashboard is vanilla HTML/CSS/JS inside a Python string, not React
- Chart is simple SVG line (no TradingView dependency)
- Landing page is single-file HTML (no build step needed)
- Brand aesthetic: "Brutalist Signal" — raw, precise, no-hype, control-room feel
