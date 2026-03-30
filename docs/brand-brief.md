# Hyperbot — Brand Brief

## One-liner

Local-first CLI that generates self-contained trading workspaces for Hyperliquid perpetual futures — no cloud dependencies, no LLM tokens, no black boxes.

## What Hyperbot Is

Hyperbot is an open-source command-line tool that scaffolds, configures, and runs automated trading workspaces for Hyperliquid perps. One command gets you from zero to a live dashboard with real-time signals, position tracking, and order execution. Everything runs on your machine, everything is deterministic Python, and the only external dependency is Hyperliquid's own API for market data and order submission.

## What Hyperbot Is Not

- Not a hosted SaaS or trading platform
- Not an AI trading bot (no model makes trade decisions)
- Not a single-strategy black box
- Not something that trades your money without explicit opt-in

## Core Position

**Hyperbot is the workspace generator for traders who want to own their stack.**

Most trading tools fall into two camps: fully managed platforms where you hand over control, or raw SDKs where you build everything from scratch. Hyperbot sits in the middle — it generates a complete, inspectable, modifiable workspace that you run locally. You get the speed of a turnkey setup with the transparency of code you can read line by line.

## Design Principles (Brand-Level)

**Local-first.** All core logic — signal detection, risk management, position sizing, order formatting — is deterministic Python running on your machine. No cloud model dependency. No API tokens beyond your Hyperliquid credentials.

**Safety by default.** Live trading is always opt-in (`--live --confirm-risk`). Leverage caps, daily loss limits, and position sizing are enforced at the workspace level. The system is designed to protect you from yourself at 3am.

**Transparent and inspectable.** Every strategy is a readable config file with a `pack_id`. Every signal is a deterministic calculation you can trace. Every revision recommendation shows you exactly what changed and why before you approve it.

**Composable, not monolithic.** Strategy packs are modular and installable. Workspaces are self-contained per trading pair. You can run `hyperbot-SOL/` and `hyperbot-LINK/` independently or manage multiple pairs from a single dashboard.

## Target Audience

### Primary: Crypto Traders Who Want Automation Without Complexity

Traders who use Hyperliquid for perps and want bot-assisted trading without needing to understand every technical detail. They want to pick tokens, choose a strategy, and let the bot handle the rest — while being able to understand what it's doing and why. They value transparency and education over raw data density. Comfortable enough with a terminal to run an install command, but not necessarily developers.

### Secondary: Developer-Traders and Quant Hobbyists

People who would build their own system but appreciate a well-structured starting point. They'll fork a strategy pack, write their own signal detectors, or extend the dashboard. They're evaluating Hyperbot as scaffolding, not as a finished product.

### Tertiary: Open-Source Contributors

Developers interested in trading infrastructure, Hyperliquid's SDK, or local-first tooling. They contribute strategy packs, improve the signal engine, or add features like backtesting and notifications.

## Brand Voice

**Technical but not academic.** Write like you're explaining something to a sharp peer, not lecturing a classroom. Use precise terminology (perps, not "derivatives"; szDecimals, not "decimal formatting") but don't gatekeep — explain when context helps.

**Direct and honest.** Say what something does and doesn't do. If a feature is experimental, say so. If a strategy pack is a baseline that needs tuning, say that too. Traders respect honesty about risk more than marketing polish.

**Confident without hype.** Hyperbot does real things — it generates workspaces, runs signal detection, executes orders on mainnet. Let the functionality speak. No "revolutionary" or "AI-powered" or "next-gen." The product is the proof.

**Builder-friendly.** Default to showing, not telling. Code examples over descriptions. CLI commands over conceptual diagrams. The README should get someone from install to running dashboard in under 60 seconds of reading.

## Key Messages

### For traders:
"Pick your tokens, choose a strategy, and let the bot trade. Every action comes with a plain-English explanation of what happened and why — so you learn as you earn."

### For developers:
"Hyperbot generates the workspace — you own it from there. Fork a strategy pack, write your own signals, extend the dashboard. It's scaffolding, not a cage."

### For the ecosystem:
"Local-first trading infrastructure for Hyperliquid. Deterministic signal detection, policy-driven risk management, and a CLI that respects your time."

## Competitive Differentiation

| Dimension | Hosted platforms | Raw SDK | Hyperbot |
|---|---|---|---|
| Setup time | Minutes | Days/weeks | Minutes |
| Transparency | Black box | Full (you wrote it) | Full (generated, readable) |
| Customization | Limited | Unlimited | High (modular packs) |
| Local execution | No | Yes | Yes |
| LLM dependency | Often | No | No |
| Safety defaults | Varies | None (your problem) | Built-in (policy-driven) |

## Visual & Naming Principles

**Name:** Hyperbot — a compound of Hyper(liquid) + bot. Short, memorable, implies automation on Hyperliquid specifically. The name should always be written as "Hyperbot" (capital H, no space, no hyphen).

**Workspace naming:** Generated workspaces follow `hyperbot-{COIN}` convention (e.g., `hyperbot-SOL`, `hyperbot-BTC`). This reinforces the brand in every directory listing.

**CLI personality:** The CLI should feel fast and competent. Minimal output by default, detailed when asked (`--verbose`). The thinking ticker on the dashboard gives the system a sense of presence without being chatty.

## What Success Looks Like

- A trader installs Hyperbot, adds 3 tokens, and understands what the bot is doing within 5 minutes
- After a week of use, the trader has learned basic trading concepts (entries, stop losses, R-multiples) from the bot's educational explanations — without reading a textbook
- A developer forks a strategy pack and has a custom signal detector running within an afternoon
- The README is the primary onboarding surface — no docs site, no video tutorial needed
- The community contributes strategy packs the way people contribute VS Code extensions
- "It's like having a trading mentor that shows its work" becomes a repeated sentiment

## Current Strategy Packs (v1)

- **trend_pullback** — SMA trend following with pullback entries
- **compression_breakout** — Bollinger Band compression into expansion
- **liquidity_sweep_reversal** — Wick analysis for sweep-and-reverse setups

These are baseline packs with sensible defaults. Token-specific revision (90-day historical profile) tunes parameters per pair automatically.

## Dashboard Philosophy

The dashboard is the primary interface for non-developer users. Its design follows these principles:

**Simplicity over density.** The dashboard is not a professional trading terminal. It's a card-based interface where each active token is a card and a `+` button adds new ones. No 3-column layouts, no raw signal checklists, no chart-first design.

**Educational transparency.** Every bot action (entry, exit, stop-loss move) comes with a plain-English explanation of *what* happened and *why*. Users learn trading concepts naturally by watching the bot work. The "Activity & Insights" notification center is the primary educational surface.

**Cards, not columns.** The main view is a responsive grid of token cards. Each card shows: token, strategy name, trade status, and P&L. Clicking an active trade card expands it to reveal TP/SL controls and a close button. A dashed `+` card lets users add new tokens with a two-step flow (pick token → pick strategy).

**Unmanaged position awareness.** The dashboard detects positions on the user's Hyperliquid account that weren't opened by Hyperbot. These appear as "Unmanaged" cards with a risk rating, issues list, and suggested actions (add stop loss, reduce leverage, let Hyperbot manage).

**Progressive disclosure.** Simple status on the card surface, controls on click/expand, educational context in the notification panel. Advanced data (charts, raw signals, logs) is available but hidden by default.

## Roadmap Context (Brand-Relevant)

Completed: workspace generation, CLI, web dashboard, live trading, multi-pair support, policy-driven auto-apply, local-only mode, wallet connect (EIP-6963 + WalletConnect), install script.

In progress: dashboard redesign (card-based simplification with educational UX).

Next: backtesting, notifications, performance tracking, strategy tuning UI. Each of these reinforces the core position — more power to the local operator, less dependency on external services.
