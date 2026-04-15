# HyperLiquid HIP-3 Copy Bot

This folder is a standalone copy bot for HyperLiquid HIP-3 (`xyz:*`) pairs.

It is intentionally separate from the standard-perp copy bots. The orchestration model matches `copy-bot-4`, but the market/execution layer is HIP-3-specific and follows the proven patterns from the archived `grid-bot` client.

## Source Of Truth

The source of truth for this bot is:

1. the code in this folder
2. the environment variables supplied at runtime (`.env` locally or Railway variables in production)

This README is meant to track structure and current defaults, but actual live wallet addresses, copied coins, leverage, and limits come from runtime environment variables.

## Current Status

- Project scaffold created on 2026-03-24.
- HIP-3-specific tracker, client, copier, and bot loop are in place.
- Python syntax check passed for all files in this folder.
- Subaccount setup is currently blocked: HyperLiquid requires $100,000 total trading volume before subaccounts can be created.
- Current total trading volume is approximately $66,000 as of 2026-03-24.
- Live API behavior has not been validated yet.
- No live wallet, subaccount, or Railway deployment has been configured yet.

## Scope

- HIP-3 only
- isolated wallet / isolated deployment
- one copied wallet per bot instance
- one configured set of `xyz:*` coins per instance

## Runtime Flow

1. Poll the copied wallet on the `xyz` dex.
2. Filter to configured HIP-3 coins.
3. Detect changes using `state`, `delta`, or `lifecycle` reconciliation.
4. Convert target size into our desired HIP-3 size.
5. Execute signed HIP-3 orders through `/exchange`.

## Current Defaults

These are the current defaults in [config.py](C:/Users/jmoss/Desktop/Project%20Money%202/copy-bot-hip3/config.py):

| Variable | Current default |
|---|---|
| `COPY_SCALING_MODE` | `fixed_notional` |
| `COPY_FIXED_RATIO` | `1.0` |
| `COPY_FIXED_SIZE` | `1.0` |
| `COPY_FIXED_NOTIONAL_USD` | `20.0` |
| `COPY_MAX_TRADE_USD` | `0.0` |
| `COPY_MAX_POSITION_USD` | `200.0` |
| `COPY_LEVERAGE` | `5` |
| `COPY_POLL_INTERVAL` | `3.0` |
| `COPY_RECONCILE_MODE` | `lifecycle` |
| `COPY_SLIPPAGE_BPS` | `10.0` |
| `COPY_MIN_TRADE_USD` | `11.0` |
| `COPY_COINS` | `xyz:SILVER` |
| `COPY_SYNC_STARTUP` | `false` |
| `COPY_MAX_DAILY_TRADES` | `200` |
| `COPY_DRY_RUN` | `true` |
| `COPY_LOG_LEVEL` | `INFO` |

Required values with no meaningful default:

- `HL_WALLET_ADDRESS`
- `HL_PRIVATE_KEY`
- `COPY_TARGET_ADDRESS`

Optional runtime value:

- `HL_ACCOUNT_ADDRESS`
  If omitted, it defaults to `HL_WALLET_ADDRESS`.

## Platform Notes

These notes are based on current HyperLiquid docs and are important for how this bot should be deployed.

### Account Model

- HyperLiquid API wallets are signer wallets only.
- To query account data, use the actual master account or subaccount address, not the API wallet address.
- If trading on behalf of a subaccount, requests should be signed by the master account and `vaultAddress` should be set to the subaccount address.

Implication for this bot:

- `HL_WALLET_ADDRESS` / `HL_PRIVATE_KEY` should represent the signer.
- `HL_ACCOUNT_ADDRESS` should represent the trading account address.
- If a subaccount is used, `HL_ACCOUNT_ADDRESS` should be that subaccount address.

### Nonces / API Wallets

- Nonces are tracked per signer, not per trading account.
- Multiple bots sharing the same API wallet can collide on nonce usage, even if they trade different subaccounts.
- HyperLiquid recommends a separate API wallet per trading process, and specifically recommends separate API wallets for separate subaccounts.

Implication for this bot:

- Do not reuse the same API wallet across multiple parallel bots if avoidable.
- Best practice is one dedicated API wallet for this HIP-3 bot.

### Rate Limits

- REST and websocket IP-based limits apply per IP address.
- Address-based action limits apply per user, with subaccounts treated as separate users.
- Address-based rate limits apply to trading actions, not info requests.

Implication for this bot:

- A subaccount helps isolate account-level trading limits and margin risk.
- A subaccount does not fully isolate IP-based limits if all bots run behind the same Railway egress IP.
- A new API key alone should not be assumed to isolate rate limits.

### Subaccounts

- HyperLiquid docs currently state that up to 10 subaccounts can be created after reaching $100,000 in volume.
- Subaccounts share fee tiers with the master account.
- API wallet count starts at 3 for all master accounts and increases by 2 per subaccount.

Current recommendation for this project:

1. Use a separate subaccount for HIP-3 if the master account is eligible.
2. Use a separate API wallet for this HIP-3 bot.
3. Treat `HL_ACCOUNT_ADDRESS` as the subaccount trading address.
4. Keep this bot on its own Railway service.
5. Still assume some IP-based rate limits may be shared with other bots.

### Remaining Unknowns

- When the master account will cross the $100,000 volume threshold for subaccount creation.
- Whether the chosen master account already has subaccount capability enabled.
- Whether Railway egress for this service will effectively share the same IP budget as the other bots.
- Whether the current SDK + this bot's HIP-3 order path works cleanly when `HL_ACCOUNT_ADDRESS` is a subaccount address.

## Files

- `bot.py`: main process, startup sync, polling loop, lifecycle/state reconciliation
- `config.py`: environment loading and validation
- `tracker.py`: target-wallet polling on `dex="xyz"`
- `hip3_client.py`: HIP-3 metadata, pricing, positions, leverage, and signed actions
- `copier.py`: copy-bot sizing, guards, and execution wrapper
- `.env.example`: local config template
- `requirements.txt`: Python dependencies

## Important Differences From Standard Copy Bots

- `COPY_COINS` must be `xyz:*` symbols.
- Price and size formatting come from HIP-3 metadata.
- Positions are queried from `user_state(..., dex="xyz")`.
- Account balance/equity still comes from the default account state, not `dex="xyz"`.
- Leverage is per-coin isolated margin.

## Run

```bash
python bot.py
```

Start in dry run. Prove metadata reads, position reads, and one tiny live order on the isolated account before trusting the full copy loop.

## Reference Docs

- HyperLiquid API wallets / nonces:
  https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets
- HyperLiquid exchange endpoint:
  https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint
- HyperLiquid rate limits:
  https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits
- HyperLiquid subaccounts:
  https://hyperliquid.gitbook.io/hyperliquid-docs/trading/sub-accounts
