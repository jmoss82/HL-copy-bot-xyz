# HyperLiquid Copy Trading Bot — XYZ HIP-3

Monitors a target trader's positions on HyperLiquid in real-time and mirrors their trades onto your account. Supports both standard HL perps **and XYZ HIP-3 pairs** (e.g. `xyz:GOLD`, `xyz:SILVER`, `xyz:TSLA`).

## How It Works

1. **Poll** the target wallet every 3 seconds via the public `/info` API (no auth needed)
2. **Open a lifecycle session** when the target goes from flat to nonzero on a copied coin
3. **Anchor your size** using the configured sizing mode, then keep a copy ratio for that trade lifecycle
4. **Mirror staged adds, trims, closes, and flips** as the target position evolves over time
5. **Execute** an IOC limit order through the spread on your account

Standard perps use the official HL SDK. XYZ HIP-3 pairs use raw `sign_l1_action` — the SDK does not support `xyz:` coins natively.

## XYZ HIP-3 Coins

XYZ pairs use the `xyz:` prefix in `COPY_COINS`. Example:

```
COPY_COINS=xyz:GOLD,xyz:SILVER,xyz:TSLA
```

You can mix standard perps and XYZ pairs in the same bot:

```
COPY_COINS=BTC,ETH,xyz:GOLD
```

Supported XYZ pairs: `xyz:XYZ100`, `xyz:TSLA`, `xyz:NVDA`, `xyz:GOLD`, `xyz:HOOD`, `xyz:INTC`, `xyz:PLTR`, `xyz:COIN`, `xyz:META`, `xyz:AAPL`, `xyz:MSFT`, `xyz:ORCL`, `xyz:GOOGL`, `xyz:AMZN`, `xyz:AMD`, `xyz:MU`, `xyz:SNDK`, `xyz:MSTR`, `xyz:CRCL`, `xyz:NFLX`, `xyz:COST`, `xyz:LLY`, `xyz:SKHX`, `xyz:TSM`, `xyz:JPY`, `xyz:EUR`, `xyz:SILVER`, `xyz:RIVN`, `xyz:BABA`, `xyz:CL`, `xyz:COPPER`, `xyz:NATGAS`, `xyz:URANIUM`, `xyz:ALUMINIUM`

## Files

| File | Purpose |
|---|---|
| `bot.py` | Main entry point, async loop, startup sync, lifecycle reconciliation, heartbeat logging |
| `config.py` | All settings loaded from environment variables with defaults |
| `tracker.py` | Polls target wallet for both standard and XYZ positions, diffs changes |
| `copier.py` | Executes mirrored trades — SDK for standard perps, sign_l1_action for XYZ |

## Deployment (Railway)

Railway is the source of truth for all configuration. Set environment variables in the service's Variables tab — no `.env` file needed. Entry point is `python bot.py`.

**Every push to the repo triggers a redeploy on Railway, which restarts the bot.**

On restart, the bot checks what the target currently has open. Any coins the target is already in are locked — the bot waits for them to close before following the next entry. `COPY_SYNC_STARTUP=true` overrides this and immediately enters to match the target.

## Environment Variables

**Required:**

| Variable | Description |
|---|---|
| `HL_WALLET_ADDRESS` | Your signer wallet address |
| `HL_PRIVATE_KEY` | Your private key |
| `HL_ACCOUNT_ADDRESS` | Your trading account (agent wallet) |
| `COPY_TARGET_ADDRESS` | Wallet address of the trader to copy |

**Configurable:**

| Variable | Default | Description |
|---|---|---|
| `COPY_SCALING_MODE` | `fixed_notional` | Anchor each new lifecycle with a fixed USD amount |
| `COPY_FIXED_NOTIONAL_USD` | `20` | USD notional used when the target opens a new lifecycle |
| `COPY_MAX_TRADE_USD` | `40` | Per-trade notional cap |
| `COPY_LEVERAGE` | `5` | Leverage applied to your positions |
| `COPY_IS_CROSS` | `false` | Isolated margin (XYZ always uses isolated regardless) |
| `COPY_COINS` | — | Coins to copy, comma-separated, use `xyz:` prefix for XYZ pairs |
| `COPY_SYNC_STARTUP` | `false` | Wait for next entry rather than entering existing positions |
| `COPY_MIN_TRADE_USD` | `11` | Skip trades below this notional (HL minimum ~$10) |
| `COPY_MAX_POSITION_USD` | `200` | Hard cap on resulting position exposure |
| `COPY_RECONCILE_MODE` | `lifecycle` | Mirror the target's full trade lifecycle instead of only the net state |
| `COPY_POLL_INTERVAL` | `3.0` | Seconds between target polls |
| `COPY_SLIPPAGE_BPS` | `10.0` | Max slippage for IOC orders (basis points) |
| `COPY_MAX_DAILY_TRADES` | `200` | Kill switch if something goes wrong |
| `COPY_DRY_RUN` | `false` | Live trading |
| `COPY_LOG_LEVEL` | `INFO` | `DEBUG` for verbose output |

## Reconcile Modes

- `state`: each poll targets a global desired position on your account.
- `delta`: trades only when the target's net position changes between snapshots.
- `lifecycle`: anchors a copy ratio when the target opens, then mirrors staged adds, trims, closes, and flips throughout that trade's lifecycle.

`lifecycle` is the best fit for wallets that scale in and out over time, whether they trade standard perps, XYZ HIP-3 pairs, or a mix of both.

## Startup Behaviour

By default (`COPY_SYNC_STARTUP=false`), the bot locks any coins the target already has open at startup and waits for them to close before following the next entry. In `lifecycle` mode this means the bot waits for a fresh `flat -> open` transition before anchoring a new copy ratio.

Set `COPY_SYNC_STARTUP=true` only when recovering from a crash where the bot was already in a position and needs to re-sync immediately. In `lifecycle` mode this joins the target's current lifecycle using the current observed position as the anchor.

## Risk Guards

- `COPY_MAX_TRADE_USD` caps a single order's notional.
- `COPY_MAX_POSITION_USD` caps resulting position exposure after each trade.
- `COPY_MIN_TRADE_USD` filters out orders below the exchange minimum.
- `COPY_MAX_DAILY_TRADES` halts trading if the daily limit is hit.

## Local Development

If running locally, create a `.env` file based on `.env.example`. Railway variables take precedence in production and no `.env` file is needed there.
