#!/usr/bin/env python3
"""
Measure estimated pubdata per L2 transaction.

Analyzes recent L2 blocks and L1 commit patterns to estimate the average
pubdata bytes each L2 transaction produces. This value is used in the TPS
scaling model (PUBDATA_PER_TX in .env).

Approach:
  1. Sample recent L2 blocks: count txs, measure calldata sizes, classify tx types
  2. Derive txs-per-batch from commit frequency and L2 block production
  3. Determine whether batches are sealing on block count or pubdata limit
  4. Compute upper bound (batch_pubdata_limit / txs_per_batch)
  5. Estimate per-tx pubdata from tx type distribution and known state-diff sizes
  6. Print a recommended PUBDATA_PER_TX value

Configuration is read from .env -- see .env.example.
"""

import json
import statistics
import urllib.request
from datetime import datetime, timezone

from config import load_config

cfg = load_config()

# Known constants from zksync-os-server source
BATCH_PUBDATA_LIMIT = 110_000   # bytes, from config default
BLOB_SIZE = 131_072             # EIP-4844 blob = 128KB

# Typical pubdata per storage slot write (compressed state diff):
#   key_derivation(1) + address(20) + key(32) + value(1-32) + overhead(~8)
# Compressed: ~50-70 bytes per slot on average
PUBDATA_PER_SLOT = 60  # bytes, rough average

# Typical storage slots touched per tx type:
TX_TYPE_SLOTS = {
    "simple_transfer": 3,    # sender balance, recipient balance, nonce
    "erc20_transfer":  4,    # sender token bal, recipient token bal, nonce, maybe allowance
    "contract_call":   6,    # varies widely, 4-20+ slots
    "system":          1,    # system txs, minimal state change
}

_id = 0
def _rpc(url, method, params=None):
    global _id; _id += 1
    body = json.dumps({"jsonrpc":"2.0","method":method,"params":params or[],"id":_id}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read())
    if "error" in d: raise RuntimeError(d["error"])
    return d["result"]

def get_block(url, n="latest", full=False):
    return _rpc(url, "eth_getBlockByNumber", [n, full])
def get_bn(url):
    return int(_rpc(url, "eth_blockNumber"), 16)
def get_nonce(url, a, b="latest"):
    return int(_rpc(url, "eth_getTransactionCount", [a, b]), 16)
def get_tx(url, h):
    return _rpc(url, "eth_getTransactionByHash", [h])


def classify_tx(tx):
    """Classify an L2 transaction by type based on its fields."""
    inp = tx.get("input", "0x")
    to = tx.get("to")
    value = int(tx.get("value", "0x0"), 16)

    if inp == "0x" and to and value > 0:
        return "simple_transfer"
    if not to:
        return "contract_deploy"
    input_len = (len(inp) - 2) // 2 if len(inp) > 2 else 0
    if input_len <= 4:
        return "simple_transfer"  # just a function selector or empty

    # Check for common ERC-20 selectors
    selector = inp[:10].lower() if len(inp) >= 10 else ""
    erc20_selectors = {
        "0xa9059cbb",  # transfer(address,uint256)
        "0x23b872dd",  # transferFrom(address,address,uint256)
        "0x095ea7b3",  # approve(address,uint256)
    }
    if selector in erc20_selectors:
        return "erc20_transfer"

    # System tx detection (type 255 or similar)
    tx_type = int(tx.get("type", "0x0"), 16)
    if tx_type >= 0x70:  # ZKsync system tx types
        return "system"

    return "contract_call"


def measure():
    print(f"Measuring pubdata for: {cfg.chain_name}")
    print(f"DA mode: {cfg.da_mode}\n")

    # ── L2 block sampling ──────────────────────────────────────────────────────
    print("Sampling L2 blocks...")
    l2_head = get_bn(cfg.l2_rpc)

    # Sample 50 recent blocks with full tx data
    sample_count = 50
    step = max(1, min(100, l2_head // sample_count))
    blocks = []
    tx_types = {"simple_transfer": 0, "erc20_transfer": 0, "contract_call": 0,
                "contract_deploy": 0, "system": 0}
    tx_input_sizes = []
    total_txs = 0

    for i in range(sample_count):
        bn = l2_head - i * step
        if bn < 1:
            break
        blk = get_block(cfg.l2_rpc, hex(bn), full=True)
        txs = blk.get("transactions", [])
        blocks.append({"number": bn, "tx_count": len(txs)})
        total_txs += len(txs)

        for tx in txs:
            if isinstance(tx, str):
                # Block returned hashes only, fetch full tx
                tx = get_tx(cfg.l2_rpc, tx)
                if not tx:
                    continue

            typ = classify_tx(tx)
            tx_types[typ] = tx_types.get(typ, 0) + 1

            inp = tx.get("input", "0x")
            input_bytes = (len(inp) - 2) // 2 if len(inp) > 2 else 0
            tx_input_sizes.append(input_bytes)

    avg_txs_per_block = total_txs / len(blocks) if blocks else 0

    # ── L2 block time ──────────────────────────────────────────────────────────
    latest = get_block(cfg.l2_rpc, "latest")
    l2_ts = int(latest["timestamp"], 16)
    old_blk = get_block(cfg.l2_rpc, hex(max(1, l2_head - 5000)))
    old_ts = int(old_blk["timestamp"], 16)
    old_n = int(old_blk["number"], 16)
    block_time = (l2_ts - old_ts) / (l2_head - old_n) if l2_head > old_n else 25.8

    # ── Batch sizing from L1 commit data ───────────────────────────────────────
    print("Querying L1 commit operator...")
    l1_bn = int(_rpc(cfg.l1_rpc, "eth_blockNumber"), 16)
    l1_7d = l1_bn - (7 * 86400 // 12)

    nonce_now = get_nonce(cfg.l1_rpc, cfg.commit_operator)
    nonce_7d = get_nonce(cfg.l1_rpc, cfg.commit_operator, hex(l1_7d))
    commits_7d = nonce_now - nonce_7d

    l2_blocks_7d = int(7 * 86400 / block_time)
    blocks_per_batch = l2_blocks_7d / commits_7d if commits_7d > 0 else 350
    txs_per_batch = blocks_per_batch * avg_txs_per_block

    # ── Analysis ───────────────────────────────────────────────────────────────
    print("Analyzing...\n")

    # Upper bound: if pubdata is the seal trigger
    upper_bound = BATCH_PUBDATA_LIMIT / txs_per_batch if txs_per_batch > 0 else 0

    # Estimate from tx type distribution
    type_total = sum(tx_types.values()) or 1
    weighted_slots = 0
    for typ, count in tx_types.items():
        slots = TX_TYPE_SLOTS.get(typ, 4)
        weighted_slots += slots * count
    avg_slots_per_tx = weighted_slots / type_total
    estimated_from_types = avg_slots_per_tx * PUBDATA_PER_SLOT

    # Check if batches are pubdata-bound or block-count-bound
    # If pubdata per batch < limit at current rates, blocks is the binding constraint
    estimated_batch_pubdata = txs_per_batch * estimated_from_types
    pubdata_is_binding = estimated_batch_pubdata >= BATCH_PUBDATA_LIMIT * 0.8

    # Input calldata size as complexity proxy (larger input = more storage writes)
    avg_input = statistics.mean(tx_input_sizes) if tx_input_sizes else 0
    median_input = statistics.median(tx_input_sizes) if tx_input_sizes else 0

    # Recommended value
    recommended = int(round(estimated_from_types, -1))  # round to nearest 10
    recommended = max(100, min(2000, recommended))  # clamp to reasonable range

    # ── Report ─────────────────────────────────────────────────────────────────
    print("=" * 70)
    print(f"  PUBDATA PER TX MEASUREMENT - {cfg.chain_name}")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 70)

    print(f"\n  L2 CHAIN STATE")
    print(f"  Block height:         {l2_head:,}")
    print(f"  Block time:           {block_time:.1f}s")
    print(f"  Avg txs per block:    {avg_txs_per_block:.2f}")
    print(f"  Current TPS:          {avg_txs_per_block / block_time:.4f}")

    print(f"\n  BATCH SIZING (from L1 commits, 7d)")
    print(f"  Commits in 7d:        {commits_7d}")
    print(f"  L2 blocks per batch:  {blocks_per_batch:.0f}")
    print(f"  L2 txs per batch:     {txs_per_batch:.0f}")

    print(f"\n  TRANSACTION TYPE DISTRIBUTION ({total_txs} txs sampled)")
    for typ, count in sorted(tx_types.items(), key=lambda x: -x[1]):
        pct = count / type_total * 100
        slots = TX_TYPE_SLOTS.get(typ, 4)
        print(f"    {typ:20s}  {count:>5d}  ({pct:5.1f}%)  ~{slots} storage slots")

    print(f"\n  TX CALLDATA SIZE (complexity proxy)")
    print(f"    Mean:   {avg_input:,.0f} bytes")
    print(f"    Median: {median_input:,.0f} bytes")
    if tx_input_sizes:
        print(f"    P90:    {sorted(tx_input_sizes)[min(len(tx_input_sizes)-1, int(len(tx_input_sizes)*0.9))]:,} bytes")
        print(f"    Max:    {max(tx_input_sizes):,} bytes")

    print(f"\n  PUBDATA ESTIMATION")
    print(f"    Avg storage slots per tx:    {avg_slots_per_tx:.1f}")
    print(f"    Pubdata per slot (est):      ~{PUBDATA_PER_SLOT} bytes")
    print(f"    Pubdata per tx (type-based): ~{estimated_from_types:.0f} bytes")
    print(f"    Pubdata per batch (est):     ~{estimated_batch_pubdata:,.0f} bytes")
    print(f"    Batch pubdata limit:         {BATCH_PUBDATA_LIMIT:,} bytes")
    print(f"    Batch utilization (est):     {estimated_batch_pubdata / BATCH_PUBDATA_LIMIT * 100:.0f}%")
    print(f"    Binding constraint:          {'pubdata limit' if pubdata_is_binding else 'blocks-per-batch limit'}")

    print(f"\n  UPPER BOUND (if pubdata were the binding constraint)")
    print(f"    {BATCH_PUBDATA_LIMIT:,} bytes / {txs_per_batch:.0f} txs = {upper_bound:.0f} bytes/tx")

    print(f"\n  {'=' * 50}")
    print(f"  RECOMMENDED: PUBDATA_PER_TX={recommended}")
    print(f"  {'=' * 50}")

    if pubdata_is_binding:
        print(f"\n  Pubdata appears to be the binding batch seal criterion.")
        print(f"  The recommended value is close to the upper bound.")
    else:
        print(f"\n  Blocks-per-batch appears to be the binding seal criterion,")
        print(f"  meaning actual pubdata per batch is well under the 110KB limit.")
        print(f"  The estimate is based on the observed transaction type mix")
        print(f"  and typical state-diff sizes per storage slot write.")

    print(f"\n  To use this value, set in your .env:")
    print(f"    PUBDATA_PER_TX={recommended}")

    print(f"\n  Reference ranges by tx type:")
    print(f"    Simple ETH transfers:   150-200 bytes")
    print(f"    ERC-20 transfers:       200-400 bytes")
    print(f"    Complex DeFi (swaps):   400-1000 bytes")
    print(f"    NFT mints / gaming:     300-800 bytes")
    print(f"    Heavy contract calls:   800-2000+ bytes")

    print()
    return recommended


if __name__ == "__main__":
    measure()
