#!/usr/bin/env python3
"""
ZKsync Chain - Operator Address Runway Report

Queries L1 (Ethereum mainnet) and L2 RPCs to produce a detailed report covering:
- Current balances (L1 operators + L2 watchdog, tracked separately)
- Historical spending rates (7-day and 30-day windows)
- Transaction frequency per operator
- Per-transaction gas costs (including blob gas for rollup commits)
- Historical L1 gas prices (baseFee sampling)
- Runway estimates at various gas price levels
- Funding recommendations for 3/6/12 month horizons
- L2 TPS
- TPS scaling model (Rollup: costs scale with TPS; Validium: costs stay flat)

Configuration is read from .env -- see .env.example.
"""

import json
import math
import statistics
import urllib.request
from datetime import datetime, timezone

from config import load_config

cfg = load_config()

# ── Constants ──────────────────────────────────────────────────────────────────

L1_ADDRESSES = {
    "Commit":      cfg.commit_operator,
    "Prove":       cfg.prove_operator,
    "Execute":     cfg.execute_operator,
    "Watchdog_L1": cfg.watchdog_address,
}
L2_ADDRESSES = {"Watchdog_L2": cfg.watchdog_address}

L1_OPS = list(L1_ADDRESSES)
L2_OPS = list(L2_ADDRESSES)

GAS_SCENARIOS   = [0.15, 1.0, 5.0, 10.0, 20.0, 50.0]
FUND_MONTHS     = [3, 6, 12]
TPS_SCENARIOS   = [0.04, 0.1, 0.5, 1.0, 5.0, 10.0, 50.0, 100.0]

BATCH_PUBDATA_LIMIT = 110_000
BLOB_SIZE           = 131_072
BLOB_GAS_PER_BLOB   = 131_072

# ── RPC helpers ────────────────────────────────────────────────────────────────

_id = 0
def _rpc(url, method, params=None):
    global _id; _id += 1
    body = json.dumps({"jsonrpc":"2.0","method":method,"params":params or [],"id":_id}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read())
    if "error" in d: raise RuntimeError(f"RPC error: {d['error']}")
    return d["result"]

def get_bal(url, a, b="latest"):   return int(_rpc(url,"eth_getBalance",[a,b]),16)
def get_nonce(url, a, b="latest"): return int(_rpc(url,"eth_getTransactionCount",[a,b]),16)
def get_block(url, n="latest"):    return _rpc(url,"eth_getBlockByNumber",[n,False])
def get_bn(url):                   return int(_rpc(url,"eth_blockNumber"),16)
def get_gp(url):                   return int(_rpc(url,"eth_gasPrice"),16)
def get_receipt(url, h):           return _rpc(url,"eth_getTransactionReceipt",[h])

def alchemy_xfers(url, **kw):
    p = {k:v for k,v in kw.items() if v is not None}
    if "from_block" in p: p["fromBlock"] = hex(p.pop("from_block"))
    if "max_count" in p: p["maxCount"]   = hex(p.pop("max_count"))
    p.setdefault("withMetadata", True)
    return _rpc(url, "alchemy_getAssetTransfers", [p]).get("transfers",[])

def sample_fees(url, lo, hi, n=50):
    step = max(1, (hi-lo)//n)
    out = []
    for i in range(n):
        bn = lo + i*step
        if bn > hi: break
        bf = get_block(url, hex(bn)).get("baseFeePerGas")
        if bf: out.append(int(bf,16)/1e9)
    return out

def fee_stats(f):
    if not f: return {}
    s = sorted(f); n = len(s)
    return {"mean":statistics.mean(s),"median":statistics.median(s),
            "p10":s[int((n-1)*0.10)],"p25":s[int((n-1)*0.25)],
            "p75":s[int((n-1)*0.75)],"p90":s[int((n-1)*0.90)],
            "min":s[0],"max":s[-1],"n":n}

def wei2eth(w): return w/1e18
def fe(e):      return f"{e:.6f}"
def fd(d):
    if d>3650: return f"{d:.0f}d (10y+)"
    if d>365:  return f"{d:.0f}d ({d/365:.1f}y)"
    if d>60:   return f"{d:.0f}d ({d/30:.1f}mo)"
    return f"{d:.1f}d"

# ── Collect ────────────────────────────────────────────────────────────────────

def collect():
    print("Fetching data from L1 and L2 RPCs...\n")
    d = {}

    l1b = get_bn(cfg.l1_rpc); d["l1_block"] = l1b
    bpd = 86400//12
    blk7 = l1b - 7*bpd;  blk30 = l1b - 30*bpd
    d["l1_gp"] = get_gp(cfg.l1_rpc)/1e9

    print("  Sampling L1 gas history...")
    d["g7s"]  = fee_stats(sample_fees(cfg.l1_rpc, blk7, l1b, 50))
    d["g30s"] = fee_stats(sample_fees(cfg.l1_rpc, blk30, l1b, 50))

    for k in ("bn","b7","b30","nn","n7","n30"): d[k] = {}
    for name, addr in L1_ADDRESSES.items():
        print(f"  {name}...")
        d["bn"][name] = get_bal(cfg.l1_rpc, addr)
        d["b7"][name] = get_bal(cfg.l1_rpc, addr, hex(blk7))
        d["b30"][name]= get_bal(cfg.l1_rpc, addr, hex(blk30))
        d["nn"][name] = get_nonce(cfg.l1_rpc, addr)
        d["n7"][name] = get_nonce(cfg.l1_rpc, addr, hex(blk7))
        d["n30"][name]= get_nonce(cfg.l1_rpc, addr, hex(blk30))
    d["blk7"] = blk7; d["blk30"] = blk30

    print("  Watchdog_L2...")
    wa = cfg.watchdog_address
    d["bn"]["Watchdog_L2"]  = get_bal(cfg.l2_rpc, wa)
    d["nn"]["Watchdog_L2"]  = get_nonce(cfg.l2_rpc, wa)
    d["l2_gp"] = get_gp(cfg.l2_rpc)/1e9

    l2l = get_block(cfg.l2_rpc,"latest")
    l2n = int(l2l["number"],16); l2t = int(l2l["timestamp"],16)
    d["l2n"] = l2n; d["l2t"] = l2t

    gen = get_block(cfg.l2_rpc,"0x1")
    d["genesis_ts"] = int(gen["timestamp"],16)

    offs = [o for o in [0,100,500,1000,2000,3000,4000,5000] if l2n-o >= 1]
    samps = []
    for o in offs:
        blk = get_block(cfg.l2_rpc, hex(l2n-o))
        samps.append((l2n-o, int(blk["timestamp"],16), len(blk.get("transactions",[]))))
    d["l2s"] = samps
    d["bt"] = (samps[0][1]-samps[-1][1])/(samps[0][0]-samps[-1][0]) if len(samps)>=2 else 25.8

    bpd2 = 86400/d["bt"]
    l2_7  = max(1, int(l2n - 7*bpd2))
    l2_30 = max(1, int(l2n - 30*bpd2))
    d["b7"]["Watchdog_L2"]  = get_bal(cfg.l2_rpc, wa, hex(l2_7))
    d["b30"]["Watchdog_L2"] = get_bal(cfg.l2_rpc, wa, hex(l2_30))
    d["n7"]["Watchdog_L2"]  = get_nonce(cfg.l2_rpc, wa, hex(l2_7))
    d["n30"]["Watchdog_L2"] = get_nonce(cfg.l2_rpc, wa, hex(l2_30))

    print("  Incoming deposits (L1, 7d + 30d)...")
    d["inc7"] = {}; d["inc30"] = {}; d["inc_det"] = {}
    for name, addr in L1_ADDRESSES.items():
        xf = alchemy_xfers(cfg.l1_rpc, toAddress=addr, from_block=d["blk30"],
                           category=["external","internal"], max_count=100)
        tot30 = sum(float(t.get("value",0) or 0) for t in xf)
        d["inc30"][name] = tot30
        # Compute 7d subset from the same transfer list
        tot7 = sum(float(t.get("value",0) or 0) for t in xf
                   if int(t.get("blockNum","0x0"),16) >= d["blk7"])
        d["inc7"][name] = tot7
        if tot30 > 0:
            d["inc_det"][name] = [{"time":t["metadata"]["blockTimestamp"],
                                   "value":float(t.get("value",0) or 0),
                                   "from":t.get("from","?")} for t in xf]

    print("  Sampling L1 tx gas...")
    d["ag"] = {}; d["blob_gp"] = []
    for name, addr in L1_ADDRESSES.items():
        xf = alchemy_xfers(cfg.l1_rpc, fromAddress=addr, from_block=d["blk7"],
                           category=["external"], excludeZeroValue=False, max_count=5, order="desc")
        gs = []
        for t in xf[:3]:
            h = t.get("hash")
            if not h: continue
            r = get_receipt(cfg.l1_rpc, h)
            if not r: continue
            gs.append(int(r["gasUsed"],16))
            bgp = int(r.get("blobGasPrice","0x0"),16)
            if bgp > 0 and name == "Commit":
                d["blob_gp"].append(bgp/1e9)
        d["ag"][name] = int(statistics.mean(gs)) if gs else 200000
    d["ag"].setdefault("Watchdog_L2", 0)

    c7 = d["nn"]["Commit"] - d["n7"]["Commit"]
    bl7 = int(7*bpd2)
    d["bpb"] = bl7/c7 if c7 > 0 else 350

    print("  Done.\n")
    return d

# ── Report ─────────────────────────────────────────────────────────────────────

def report(d):
    o = []; w = o.append
    da = cfg.da_mode.upper()
    is_val = cfg.da_mode == "validium"

    gen_dt = datetime.fromtimestamp(d["genesis_ts"], tz=timezone.utc)
    age = (datetime.now(timezone.utc) - gen_dt).days
    g1 = d["l1_gp"]; g2 = d["l2_gp"]
    bt = d["bt"]
    smp = d["l2s"]
    avg_txpb = sum(s[2] for s in smp)/len(smp) if smp else 0
    tps = avg_txpb/bt if bt>0 else 0; dtxs = tps*86400
    bpd = 86400/bt
    avg_blob_gp = statistics.mean(d["blob_gp"]) if d["blob_gp"] else 0.01

    w("="*100)
    w(f"  {cfg.chain_name.upper()} - OPERATOR RUNWAY REPORT  [{da}]")
    w(f"  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    w("="*100)

    w("\n  CHAIN OVERVIEW\n  "+"-"*60)
    w(f"  Chain genesis:          {gen_dt.strftime('%Y-%m-%d')} ({age} days ago)")
    w(f"  DA mode:                {da}")
    w(f"  L2 block height:        {d['l2n']:,}")
    w(f"  L2 block time:          {bt:.1f}s")
    w(f"  L2 TPS:                 {tps:.4f} (~{dtxs:,.0f} txs/day)")
    w(f"  Current L1 gas price:   {g1:.2f} gwei")
    w(f"  Current L2 gas price:   {g2:.4f} gwei")
    w(f"  L1 block:               {d['l1_block']:,}")
    w(f"  Blocks per batch (7d):  {d['bpb']:.0f}")
    if not is_val:
        w(f"  Commit tx type:         EIP-4844 blob (1 blob/commit)")
    else:
        w(f"  Commit tx type:         Standard (no blobs, off-chain DA)")

    # Balances
    w("\n  CURRENT BALANCES\n  "+"-"*60)
    tl1 = 0
    for n in L1_OPS:
        b = wei2eth(d["bn"][n]); tl1 += b
        w(f"  {n:20s}  {fe(b):>12s} ETH")
    w(f"  {'':20s}  {'----------':>12s}")
    w(f"  {'Total L1':20s}  {fe(tl1):>12s} ETH")
    w(f"\n  {'Watchdog_L2':20s}  {fe(wei2eth(d['bn']['Watchdog_L2'])):>12s} ETH")

    # Tx activity
    w("\n  TRANSACTION ACTIVITY\n  "+"-"*60)
    w(f"  {'Operator':14s} {'Chain':>5s} {'Total':>8s} {'Last 7d':>9s} {'7d/day':>8s} {'Prev 23d':>9s} {'23d/day':>8s}")
    dtx7 = {}
    for n in L1_OPS + L2_OPS:
        ch = "L2" if n.endswith("_L2") else "L1"
        nn = d["nn"][n]; n7 = d["n7"][n]; n30 = d["n30"][n]
        t7 = nn-n7; t23 = n7-n30; r7 = t7/7; r23 = t23/23 if t23>0 else 0
        dtx7[n] = r7
        w(f"  {n:14s} {ch:>5s} {nn:>8,d} {t7:>9,d} {r7:>8.1f} {t23:>9,d} {r23:>8.1f}")

    # Gas per tx
    w("\n  GAS USAGE PER L1 TRANSACTION (recent samples)\n  "+"-"*60)
    for n in L1_OPS:
        ag = d["ag"].get(n, 200000)
        w(f"  {n:14s} Avg gas: {ag:>10,d}")

    # L2 watchdog spending
    w("\n  WATCHDOG L2 SPENDING\n  "+"-"*60)
    for bk, nk, days in [("b7","n7",7),("b30","n30",30)]:
        old_b = wei2eth(d[bk]["Watchdog_L2"]); now_b = wei2eth(d["bn"]["Watchdog_L2"])
        sp = max(0, old_b - now_b); daily = sp/days if sp>0 else 0
        nd = d["nn"]["Watchdog_L2"] - d[nk]["Watchdog_L2"]
        w(f"  {days}d: {fe(old_b)} -> {fe(now_b)}, spent {fe(sp)} ({fe(daily)}/day, {nd} txs)")
    sp30 = max(0, wei2eth(d["b30"]["Watchdog_L2"]) - wei2eth(d["bn"]["Watchdog_L2"]))
    dl2 = sp30/30 if sp30>0 else 0
    rw_l2 = wei2eth(d["bn"]["Watchdog_L2"])/dl2 if dl2>0 else float("inf")
    w(f"  L2 gas: {g2:.4f} gwei | Runway: {fd(rw_l2)}")

    # Historical L1 spending
    w("\n  HISTORICAL L1 SPENDING (balance delta)\n  "+"-"*60)
    for label, days, bk in [("7-day",7,"b7"),("30-day",30,"b30")]:
        w(f"\n  {label} window:")
        w(f"  {'Operator':14s} {'Old Bal':>12s} {'Incoming':>10s} {'Now':>12s} {'Spent':>12s} {'Per Day':>12s}")
        td = 0
        for n in L1_OPS:
            ob = wei2eth(d[bk][n]); nb = wei2eth(d["bn"][n])
            inc = d["inc30"].get(n,0) if days==30 else d["inc7"].get(n,0)
            sp = (ob+inc)-nb; dy = sp/days
            if dy>0: td += dy
            w(f"  {n:14s} {fe(ob):>12s} {fe(inc):>10s} {fe(nb):>12s} {fe(sp):>12s} {fe(dy):>12s}")
        w(f"  {'TOTAL':14s} {'':>12s} {'':>10s} {'':>12s} {'':>12s} {fe(td):>12s}")

    # Historical gas
    w("\n  HISTORICAL L1 GAS PRICES\n  "+"-"*60)
    for lb, st in [("7-day",d["g7s"]),("30-day",d["g30s"])]:
        if st:
            w(f"\n  {lb} ({st['n']} samples): mean={st['mean']:.3f} median={st['median']:.3f} P90={st['p90']:.3f} gwei")
    if d["blob_gp"]:
        w(f"  Blob gas ({len(d['blob_gp'])} samples): mean={avg_blob_gp:.6f} gwei")

    # Helper for daily cost.
    # Blob gas scales proportionally with exec gas (correlated during congestion).
    def blob_gp_for(gwei):
        if g1 > 0:
            return avg_blob_gp * (gwei / g1)
        return avg_blob_gp

    def daily_cost(name, gwei):
        ag = d["ag"].get(name, 200000)
        dc = dtx7.get(name,0) * ag * gwei * 1e-9
        if name == "Commit" and not is_val:
            dc += dtx7.get(name,0) * BLOB_GAS_PER_BLOB * blob_gp_for(gwei) * 1e-9
        return dc

    # Runway table
    w(f"\n  RUNWAY ESTIMATES - L1 (current 7-day tx rates) [{da}]\n  "+"-"*96)
    hdr = f"  {'Gas':>10s}"
    for n in L1_OPS: hdr += f" {'|'+n:>17s}"
    hdr += f" {'|Total/day':>14s}"; w(hdr); w("  "+"-"*96)
    for gw in GAS_SCENARIOS:
        lb = f"{gw:.2f}gw" if gw<1 else f"{gw:.0f}gw"
        row = f"  {lb:>10s}"; td = 0
        for n in L1_OPS:
            dc = daily_cost(n, gw); td += dc
            bal = wei2eth(d["bn"][n])
            rw = bal/dc if dc>0 else float("inf")
            row += f" | {fd(rw):>14s}"
        row += f" | {fe(td):>10s}"; w(row)

    # Historical runway
    w(f"\n  RUNWAY AT HISTORICAL GAS [{da}]\n  "+"-"*96)
    for lb, st in [("7d",d["g7s"]),("30d",d["g30s"])]:
        if not st: continue
        for mn, mk in [("mean","mean"),("median","median"),("P90","p90")]:
            gw = st[mk]; parts = []; td = 0
            for n in L1_OPS:
                dc = daily_cost(n, gw); td += dc
                bal = wei2eth(d["bn"][n])
                rw = bal/dc if dc>0 else float("inf")
                parts.append(f"{n}={fd(rw)}")
            w(f"  {lb+' '+mn:>14s} ({gw:.3f}gw): {' | '.join(parts)} | {fe(td)}/day")
        w("")

    # Funding at historical gas
    w(f"  FUNDING RECOMMENDATIONS - HISTORICAL GAS (L1) [{da}]\n  "+"-"*96)
    gs = d["g30s"]
    if gs:
        for mn, mk in [("30d mean","mean"),("30d P75","p75"),("30d P90","p90")]:
            gw = gs[mk]
            w(f"  At {mn} ({gw:.3f} gwei):")
            hdr = f"  {'Operator':14s} {'ETH/day':>12s}"
            for m in FUND_MONTHS: hdr += f" | {str(m)+'mo cost':>10s} {'needed':>10s}"
            w(hdr)
            tp = {"d":0}
            for m in FUND_MONTHS: tp[f"c{m}"]=0; tp[f"n{m}"]=0
            for n in L1_OPS:
                dc = daily_cost(n, gw); bal = wei2eth(d["bn"][n])
                row = f"  {n:14s} {fe(dc):>12s}"; tp["d"] += dc
                for m in FUND_MONTHS:
                    c = dc*m*30; need = max(0, c-bal)
                    row += f" | {fe(c):>10s} {fe(need):>10s}"
                    tp[f"c{m}"] += c; tp[f"n{m}"] += need
                w(row)
            row = f"  {'TOTAL':14s} {fe(tp['d']):>12s}"
            for m in FUND_MONTHS: row += f" | {fe(tp[f'c{m}']):>10s} {fe(tp[f'n{m}']):>10s}"
            w(row); w("")

    # Stress scenarios
    w(f"  FUNDING - STRESS SCENARIOS (L1) [{da}]\n  "+"-"*96)
    for gw in [1.0, 5.0, 10.0, 20.0]:
        w(f"  At {gw:.0f} gwei:")
        hdr = f"  {'Operator':14s}"
        for m in FUND_MONTHS: hdr += f" | {str(m)+'mo cost':>10s} {'needed':>10s}"
        w(hdr)
        tr = {}
        for m in FUND_MONTHS: tr[f"c{m}"]=0; tr[f"n{m}"]=0
        for n in L1_OPS:
            dc = daily_cost(n, gw); bal = wei2eth(d["bn"][n])
            row = f"  {n:14s}"
            for m in FUND_MONTHS:
                c = dc*m*30; need = max(0,c-bal)
                row += f" | {fe(c):>10s} {fe(need):>10s}"
                tr[f"c{m}"] += c; tr[f"n{m}"] += need
            w(row)
        row = f"  {'TOTAL':14s}"
        for m in FUND_MONTHS: row += f" | {fe(tr[f'c{m}']):>10s} {fe(tr[f'n{m}']):>10s}"
        w(row); w("")

    # ── TPS scaling model ──
    w("="*100)
    w(f"  TPS SCALING MODEL  [{da}]")
    w("="*100)

    cg = d["ag"].get("Commit",157000)
    pg = d["ag"].get("Prove",495000)
    eg = d["ag"].get("Execute",153000)
    wg = d["ag"].get("Watchdog_L1",236000)
    wr = dtx7.get("Watchdog_L1",24)
    cbpb = int(d["bpb"])

    # Max TPS where a single block fits in the batch pubdata limit
    max_valid_tps = BATCH_PUBDATA_LIMIT / (bt * cfg.pubdata_per_tx) if (bt * cfg.pubdata_per_tx) > 0 else 999

    w(f"\n  Assumptions:")
    w(f"    L2 block time:            {bt:.1f}s")
    w(f"    DA mode:                  {da}")
    w(f"    Batch pubdata limit:      {BATCH_PUBDATA_LIMIT:,} bytes")
    w(f"    Tx per batch limit:       10,000")
    w(f"    Pubdata per L2 tx:        {cfg.pubdata_per_tx} bytes")
    if not is_val:
        w(f"    Blob gas per blob:        {BLOB_GAS_PER_BLOB:,}")
        w(f"    Blob gas scales with exec gas in stress scenarios")
    else:
        w(f"    Blob gas:                 none (off-chain DA)")
    w(f"    Commit exec gas:          {cg:,}")
    w(f"    Prove gas:                {pg:,}")
    w(f"    Execute gas:              {eg:,}")
    w(f"    Watchdog_L1 txs/day:      {wr:.0f}")
    w(f"    Current blocks/batch:     {cbpb:.0f}")
    w(f"    Max modeled TPS:          {max_valid_tps:.1f} (1 block = 1 batch beyond this)")
    w("")

    if is_val:
        w("  Validium: pubdata is stored off-chain, so no blob gas is charged on commits.")
        w("  However, batch frequency DOES still scale with TPS because the batch sealing")
        w("  criteria (pubdata limit, tx-per-batch limit) still apply regardless of DA mode.")
        w(f"  The saving vs Rollup is the blob gas (~{BLOB_GAS_PER_BLOB:,} gas/blob per commit).\n")
    else:
        w("  Rollup: higher TPS -> more pubdata per block -> smaller batches -> more")
        w("  L1 txs per day. Each batch fits in 1 blob (pubdata limit < blob size).\n")

    def tps_row(target_tps, gwei):
        txpb = target_tps * bt
        ppb = txpb * cfg.pubdata_per_tx

        eff = cbpb
        # Pubdata limit constrains batch size (both rollup and validium)
        if ppb > 0 and ppb * eff > BATCH_PUBDATA_LIMIT:
            eff = max(1, int(BATCH_PUBDATA_LIMIT / ppb))
        # Tx-per-batch limit (10,000) constrains batch size
        if txpb > 0 and txpb * eff > 10000:
            eff = min(eff, max(1, int(10000 / txpb)))

        pub_batch = eff * ppb
        blobs = 0 if is_val else max(1, math.ceil(pub_batch / BLOB_SIZE))
        # Flag if single block exceeds pubdata limit (model is at floor)
        capped = ppb > BATCH_PUBDATA_LIMIT

        bpd_val = bpd / eff if eff > 0 else 0
        cd = bpd_val * cg * gwei * 1e-9
        if not is_val:
            cd += bpd_val * blobs * BLOB_GAS_PER_BLOB * blob_gp_for(gwei) * 1e-9
        pd = bpd_val * pg * gwei * 1e-9
        ed = bpd_val * eg * gwei * 1e-9
        wdd = wr * wg * gwei * 1e-9
        tot = cd + pd + ed + wdd
        return target_tps, target_tps*86400, eff, bpd_val, blobs, cd, pd, ed, wdd, tot, capped

    hdr = f"  {'TPS':>7s} {'txs/day':>10s} {'blk/bat':>8s} {'bat/day':>8.1s}"
    if not is_val: hdr += f" {'blobs':>6s}"
    hdr += f" | {'Commit':>10s} {'Prove':>10s} {'Execute':>10s} {'WD_L1':>10s} {'Total/day':>12s} {'Total/mo':>12s}"

    for glabel, gw in [("current", g1),
                        ("30d avg", d["g30s"].get("mean",g1) if d["g30s"] else g1),
                        ("5 gwei", 5.0), ("10 gwei", 10.0), ("20 gwei", 20.0)]:
        w(f"\n  At {glabel} ({gw:.3f} gwei):")
        w(hdr)
        for tp in TPS_SCENARIOS:
            r = tps_row(tp, gw)
            capped = r[10]
            mark = " *" if capped else ""
            row = f"  {tp:>7.2f} {r[1]:>10,.0f} {r[2]:>8d} {r[3]:>8.1f}"
            if not is_val: row += f" {r[4]:>6d}"
            row += f" | {fe(r[5]):>10s} {fe(r[6]):>10s} {fe(r[7]):>10s} {fe(r[8]):>10s} {fe(r[9]):>12s} {fe(r[9]*30):>12s}{mark}"
            w(row)
        w("  * = single block exceeds batch pubdata limit; model capped at 1 block/batch")

    # TPS funding table
    w(f"\n  TPS SCALING - 6 MONTH FUNDING (L1, 30d avg gas) [{da}]\n  "+"-"*100)
    gw_avg = d["g30s"].get("mean", g1) if d["g30s"] else g1
    w(f"  At {gw_avg:.3f} gwei:")
    hdr2 = f"  {'TPS':>7s} {'bat/day':>8s}"
    for n in L1_OPS: hdr2 += f" | {n:>12s}"
    hdr2 += f" | {'Total 6mo':>12s} {'Needed':>12s}"; w(hdr2)
    for tp in TPS_SCENARIOS:
        r = tps_row(tp, gw_avg)
        costs6 = {}; need6 = {}
        per_op = [r[5], r[6], r[7], r[8]]
        tot6 = 0; totn = 0
        row = f"  {tp:>7.2f} {r[3]:>8.1f}"
        for i, n in enumerate(L1_OPS):
            c6 = per_op[i]*180; n6 = max(0, c6 - wei2eth(d["bn"][n]))
            tot6 += c6; totn += n6
            row += f" | {fe(n6):>12s}"
        row += f" | {fe(tot6):>12s} {fe(totn):>12s}"; w(row)

    # Deposits
    w(f"\n  RECENT INCOMING DEPOSITS (L1, 30d)\n  "+"-"*60)
    any_d = False
    for n in L1_OPS:
        for dd in d.get("inc_det",{}).get(n,[]):
            any_d = True
            w(f"  {n:14s}  {dd['time']}  {dd['value']:.6f} ETH  from {dd['from'][:20]}...")
    if not any_d: w("  None")

    # Risk
    w(f"\n  RISK SUMMARY\n  "+"-"*60)
    mr = float("inf"); bn_n = None
    for n in L1_OPS:
        dc = daily_cost(n, g1); bal = wei2eth(d["bn"][n])
        if dc > 0:
            rw = bal/dc
            if rw < mr: mr = rw; bn_n = n
    if bn_n:
        w(f"  L1 bottleneck:          {bn_n} ({fe(wei2eth(d['bn'][bn_n]))} ETH)")
        w(f"  Runway at current gas:  {fd(mr)}")
    else:
        w("  L1 bottleneck:          N/A (no operator activity in last 7 days)")
    if gs := d.get("g30s"):
        for mn, mk in [("30d avg","mean"),("30d P90","p90")]:
            gw = gs[mk]; mr2 = float("inf")
            for n in L1_OPS:
                dc = daily_cost(n, gw); bal = wei2eth(d["bn"][n])
                if dc>0: mr2 = min(mr2, bal/dc)
            w(f"  Runway at {mn} ({gw:.3f}gw): {fd(mr2)}")
    for gw in [5.0, 20.0]:
        mr2 = float("inf")
        for n in L1_OPS:
            dc = daily_cost(n, gw); bal = wei2eth(d["bn"][n])
            if dc>0: mr2 = min(mr2, bal/dc)
        w(f"  Runway at {gw:.0f} gwei:         {fd(mr2)}")
    w(f"\n  L2 Watchdog runway:     {fd(rw_l2)}")
    w("\n"+"="*100)
    return "\n".join(o)

if __name__ == "__main__":
    print(report(collect()))
