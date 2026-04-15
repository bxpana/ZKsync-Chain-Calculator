# ZKsync Chain Calculator

Tools for analyzing operator address runway and funding needs for ZKsync chains.

Works with any ZKsync chain (ZK Stack). Supports both **Rollup** (EIP-4844 blobs) and **Validium** (off-chain DA) modes.

## Setup

1. Copy the example environment file and fill in your chain's values:

```
cp .env.example .env
```

2. Edit `.env` with your chain's RPC endpoints, operator addresses, and DA mode.

Required fields:
- `L1_RPC` -- Ethereum mainnet RPC (Alchemy recommended for transfer history queries)
- `L2_RPC` -- Your L2 chain's RPC endpoint
- `COMMIT_OPERATOR`, `PROVE_OPERATOR`, `EXECUTE_OPERATOR` -- L1 operator addresses
- `WATCHDOG_ADDRESS` -- Watchdog address (same address used on both L1 and L2)

Optional fields:
- `CHAIN_NAME` -- Display name (default: "ZKsync Chain")
- `DA_MODE` -- `rollup` or `validium` (default: `rollup`)
- `PUBDATA_PER_TX` -- Estimated bytes of pubdata per L2 transaction (default: `300`). Run `measure_pubdata.py` to measure this for your chain (see below).

## Scripts

### `measure_pubdata.py` -- Measure PUBDATA_PER_TX

Run this first to determine the right `PUBDATA_PER_TX` value for your chain. It samples recent L2 blocks, classifies transaction types, and estimates pubdata based on the observed mix.

```
python3 measure_pubdata.py
```

The script will:
- Sample 50 recent L2 blocks and classify each transaction (simple transfer, ERC-20, contract call, etc.)
- Derive the current batch size from L1 commit frequency
- Estimate pubdata per tx based on storage slots written per tx type (~60 bytes/slot)
- Determine whether batches are sealing on block count or pubdata limit
- Print a recommended `PUBDATA_PER_TX` value

Typical ranges by chain activity:
| Chain type | PUBDATA_PER_TX |
|---|---|
| Mostly simple transfers | 150-200 |
| Mixed transfers + tokens | 200-400 |
| DeFi heavy (swaps, LPs) | 400-800 |
| Gaming / NFTs | 300-600 |
| Complex contract interactions | 800-2000 |

After running, update your `.env`:
```
PUBDATA_PER_TX=200   # or whatever the script recommends
```

### `runway_report.py` -- Detailed CLI Report

Produces a comprehensive text report covering:
- Current balances for all operator addresses (L1) and watchdog (L1 + L2, tracked separately)
- Historical spending rates over 7-day and 30-day windows
- L1 gas price history (sampled baseFee from block headers)
- Runway estimates at current, historical, and stress-test gas prices
- Funding recommendations for 3, 6, and 12 month horizons
- TPS scaling model showing how L1 costs change at higher throughput
- L2 TPS and chain statistics

```
cd ZKsync-Chain-Calculator
python3 runway_report.py
```

### `funding_calculator.py` -- Interactive HTML Calculator

Generates a self-contained HTML file that partners can open in any browser. No server or dependencies needed.

Interactive controls:
- **DA Mode toggle** -- switch between Rollup and Validium to compare costs
- **TPS slider** -- see how funding needs change from 0.01 to 300 TPS
- **Pubdata/tx input** -- adjust the estimated pubdata per transaction

Tables show funding needed (per address) for 3/6/12 months at historical gas prices and stress scenarios (1, 5, 10, 20 gwei).

```
cd ZKsync-Chain-Calculator
python3 funding_calculator.py
# Opens: <chain_name>_funding.html
```

## DA Modes

### Rollup

Pubdata is posted to Ethereum L1 via EIP-4844 blob transactions. Commit transactions carry a blob sidecar.

- L1 costs **scale with TPS**: more transactions means more pubdata, which triggers smaller batches and more frequent L1 commits.
- Each batch fits in 1 blob (batch pubdata limit of 110KB < blob size of 128KB).
- Cost per commit = execution gas + blob gas.

### Validium

Pubdata is stored off-chain (DA committee or external DA layer). Only commitments/hashes are posted to L1.

- L1 costs are **flat regardless of TPS**: batch frequency stays at the configured rate since pubdata doesn't constrain batch size.
- No blob gas charged on commits.
- Costs only increase if the 10,000 transactions-per-batch limit is reached (very high TPS).

## Requirements

- Python 3.8+
- No external packages needed (uses only stdlib: `json`, `urllib`, `statistics`)
- L1 RPC should support Alchemy's `alchemy_getAssetTransfers` for operator transaction history and deposit tracking
