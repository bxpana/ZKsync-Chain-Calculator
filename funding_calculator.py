#!/usr/bin/env python3
"""
ZKsync Chain - Interactive Funding Calculator (HTML generator)

Fetches live data from L1/L2 RPCs, then produces a self-contained HTML file
that partners can open in any browser to explore funding needs.

Controls: DA mode toggle (Rollup/Validium), TPS slider, pubdata-per-tx input.
Tables update instantly showing 3/6/12 month funding at historical and stress
gas price levels.

Configuration is read from .env -- see .env.example.
"""

import json
import re
import statistics
import urllib.request
from datetime import datetime, timezone

from config import load_config

cfg = load_config()

# ── RPC helpers (same as runway_report.py) ─────────────────────────────────────

_id = 0
def _rpc(url, method, params=None):
    global _id; _id += 1
    body = json.dumps({"jsonrpc":"2.0","method":method,"params":params or[],"id":_id}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read())
    if "error" in d: raise RuntimeError(d["error"])
    return d["result"]

def get_bal(u,a,b="latest"):   return int(_rpc(u,"eth_getBalance",[a,b]),16)
def get_nonce(u,a,b="latest"): return int(_rpc(u,"eth_getTransactionCount",[a,b]),16)
def get_bn(u):                 return int(_rpc(u,"eth_blockNumber"),16)
def get_gp(u):                 return int(_rpc(u,"eth_gasPrice"),16)
def get_block(u,n="latest"):   return _rpc(u,"eth_getBlockByNumber",[n,False])
def get_receipt(u,h):          return _rpc(u,"eth_getTransactionReceipt",[h])
def alchemy_xfers(u, **kw):
    p = {k:v for k,v in kw.items() if v is not None}
    if "from_block" in p: p["fromBlock"] = hex(p.pop("from_block"))
    if "max_count" in p: p["maxCount"]   = hex(p.pop("max_count"))
    p.setdefault("withMetadata", True)
    return _rpc(u, "alchemy_getAssetTransfers", [p]).get("transfers",[])

# ── Data collection ────────────────────────────────────────────────────────────

def collect():
    print("Fetching chain data...")
    d = {}
    l1b = get_bn(cfg.l1_rpc); bpd = 86400//12
    b7 = l1b - 7*bpd; b30 = l1b - 30*bpd
    d["l1_gas_gwei"] = get_gp(cfg.l1_rpc)/1e9

    print("  Sampling L1 gas history...")
    fees_7d, fees_30d = [], []
    for i in range(50):
        bn = b7 + i*((l1b-b7)//50)
        bf = get_block(cfg.l1_rpc, hex(bn)).get("baseFeePerGas")
        if bf: fees_7d.append(int(bf,16)/1e9)
    for i in range(50):
        bn = b30 + i*((l1b-b30)//50)
        bf = get_block(cfg.l1_rpc, hex(bn)).get("baseFeePerGas")
        if bf: fees_30d.append(int(bf,16)/1e9)
    d["gas_7d"]  = {"mean":statistics.mean(fees_7d),"median":statistics.median(fees_7d),
                     "p90":sorted(fees_7d)[min(len(fees_7d)-1,int(len(fees_7d)*0.9))]}
    d["gas_30d"] = {"mean":statistics.mean(fees_30d),"median":statistics.median(fees_30d),
                     "p90":sorted(fees_30d)[min(len(fees_30d)-1,int(len(fees_30d)*0.9))]}

    addrs = {"Commit":cfg.commit_operator, "Prove":cfg.prove_operator,
             "Execute":cfg.execute_operator, "Watchdog_L1":cfg.watchdog_address}
    d["balances"], d["nonce_now"], d["nonce_7d"] = {}, {}, {}
    for name, addr in addrs.items():
        print(f"  {name}...")
        d["balances"][name] = get_bal(cfg.l1_rpc, addr)/1e18
        d["nonce_now"][name] = get_nonce(cfg.l1_rpc, addr)
        d["nonce_7d"][name]  = get_nonce(cfg.l1_rpc, addr, hex(b7))

    print("  Watchdog_L2...")
    d["balances"]["Watchdog_L2"] = get_bal(cfg.l2_rpc, cfg.watchdog_address)/1e18
    d["l2_gas_gwei"] = get_gp(cfg.l2_rpc)/1e9

    l2l = get_block(cfg.l2_rpc,"latest")
    l2n = int(l2l["number"],16); l2t = int(l2l["timestamp"],16)
    l2o = get_block(cfg.l2_rpc, hex(max(1,l2n-5000)))
    on = int(l2o["number"],16); ot = int(l2o["timestamp"],16)
    d["l2_block_time"] = (l2t-ot)/(l2n-on) if l2n>on else 25.8
    d["l2_tps"] = 1.0/d["l2_block_time"]
    d["l2_block_height"] = l2n

    gen = get_block(cfg.l2_rpc,"0x1")
    d["chain_age_days"] = (l2t - int(gen["timestamp"],16))/86400

    c7 = d["nonce_now"]["Commit"] - d["nonce_7d"]["Commit"]
    bl7 = int(7*86400/d["l2_block_time"])
    d["blocks_per_batch"] = bl7/c7 if c7>0 else 350

    d["wd_l1_txs_day"] = (d["nonce_now"]["Watchdog_L1"] - d["nonce_7d"]["Watchdog_L1"])/7

    bpd2 = 86400/d["l2_block_time"]
    l2_30 = max(1, int(l2n - 30*bpd2))
    wold = get_bal(cfg.l2_rpc, cfg.watchdog_address, hex(l2_30))/1e18
    d["wd_l2_daily_spend"] = (wold - d["balances"]["Watchdog_L2"])/30

    print("  Sampling tx gas...")
    d["avg_gas"] = {}; d["avg_blob_gas_price_gwei"] = 0.01; blob_p = []
    for name, addr in addrs.items():
        xf = alchemy_xfers(cfg.l1_rpc, fromAddress=addr, from_block=b7,
                           category=["external"], excludeZeroValue=False, max_count=5, order="desc")
        gs = []
        for t in xf[:3]:
            h = t.get("hash")
            if not h: continue
            r = get_receipt(cfg.l1_rpc, h)
            if r:
                gs.append(int(r["gasUsed"],16))
                bgp = int(r.get("blobGasPrice","0x0"),16)
                if bgp>0 and name=="Commit": blob_p.append(bgp/1e9)
        d["avg_gas"][name] = int(statistics.mean(gs)) if gs else 200000
    if blob_p: d["avg_blob_gas_price_gwei"] = statistics.mean(blob_p)

    d["da_mode"] = cfg.da_mode
    d["pubdata_per_tx"] = cfg.pubdata_per_tx
    d["chain_name"] = cfg.chain_name
    d["generated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print("  Done.\n")
    return d

# ── HTML generation ────────────────────────────────────────────────────────────

def generate_html(d):
    js_data = json.dumps(d, indent=2)
    cn = d["chain_name"]
    default_mode = d["da_mode"]

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{cn} - Operator Funding Calculator</title>
<style>
  :root {{ --bg:#f8f9fa; --card:#fff; --border:#dee2e6; --text:#212529;
           --muted:#6c757d; --accent:#0d6efd; --accent2:#198754;
           --warn:#dc3545; --head-bg:#e9ecef; }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,monospace;
          background:var(--bg); color:var(--text); padding:20px; max-width:1200px; margin:0 auto; }}
  h1 {{ font-size:1.4rem; margin-bottom:4px; }}
  h2 {{ font-size:1.1rem; margin:24px 0 12px; border-bottom:2px solid var(--border); padding-bottom:6px; }}
  .sub {{ color:var(--muted); font-size:0.85rem; margin-bottom:16px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:12px; margin-bottom:20px; }}
  .card {{ background:var(--card); border:1px solid var(--border); border-radius:8px; padding:14px; }}
  .card .lb {{ font-size:0.75rem; color:var(--muted); text-transform:uppercase; letter-spacing:0.5px; }}
  .card .vl {{ font-size:1.3rem; font-weight:600; margin-top:4px; }}
  .card .dt {{ font-size:0.8rem; color:var(--muted); margin-top:2px; }}
  .ctrls {{ background:var(--card); border:1px solid var(--border); border-radius:8px;
            padding:16px; margin-bottom:20px; display:flex; flex-wrap:wrap; gap:20px; align-items:center; }}
  .cg {{ display:flex; flex-direction:column; gap:4px; }}
  .cg label {{ font-size:0.8rem; font-weight:600; color:var(--muted); text-transform:uppercase; }}
  .cg input,.cg select {{ font-size:1rem; padding:6px 10px; border:1px solid var(--border); border-radius:4px; background:var(--bg); }}
  .cg input[type=range] {{ width:200px; }}
  .tpsd {{ font-size:1.3rem; font-weight:700; color:var(--accent); min-width:80px; }}
  table {{ width:100%; border-collapse:collapse; font-size:0.85rem; margin-bottom:16px; background:var(--card);
           border:1px solid var(--border); border-radius:8px; overflow:hidden; }}
  th {{ background:var(--head-bg); text-align:right; padding:8px 12px; font-weight:600; font-size:0.75rem;
       text-transform:uppercase; color:var(--muted); }}
  th:first-child {{ text-align:left; }}
  td {{ padding:8px 12px; text-align:right; border-top:1px solid var(--border); }}
  td:first-child {{ text-align:left; font-weight:600; }}
  tr.tot {{ background:var(--head-bg); font-weight:700; }}
  tr.tot td {{ border-top:2px solid var(--border); }}
  .needed {{ color:var(--warn); font-weight:600; }}
  .ok {{ color:var(--accent2); }}
  .mt {{ display:flex; border:2px solid var(--accent); border-radius:6px; overflow:hidden; }}
  .mt button {{ padding:8px 20px; border:none; cursor:pointer; font-weight:600;
                background:var(--card); color:var(--accent); font-size:0.9rem; }}
  .mt button.active {{ background:var(--accent); color:white; }}
  .note {{ font-size:0.8rem; color:var(--muted); margin:8px 0; }}
  .info {{ background:#fff3cd; border:1px solid #ffc107; border-radius:6px; padding:10px 14px;
           font-size:0.85rem; margin-bottom:16px; }}
  .pi {{ width:80px; }}
</style>
</head>
<body>
<h1>{cn} - Operator Funding Calculator</h1>
<p class="sub">Data fetched: {d["generated"]} | Chain age: {d["chain_age_days"]:.0f} days | L2 block height: {d["l2_block_height"]:,}</p>
<div class="grid" id="cards"></div>
<h2>Controls</h2>
<div class="ctrls">
  <div class="cg"><label>DA Mode</label>
    <div class="mt">
      <button id="btn-r" onclick="setMode('rollup')">Rollup</button>
      <button id="btn-v" onclick="setMode('validium')">Validium</button>
    </div></div>
  <div class="cg"><label>Target TPS</label>
    <input type="range" id="sl" min="-2" max="2.5" step="0.01" value="-1.4" oninput="upd()">
    <div class="tpsd" id="tpsd">0.04</div></div>
  <div class="cg"><label>Pubdata/tx (bytes)</label>
    <input type="number" class="pi" id="pi" value="{d['pubdata_per_tx']}" min="50" max="5000" step="50" oninput="upd()"></div>
  <div class="cg"><label>Info</label>
    <div id="mi" class="note" style="max-width:320px"></div></div>
</div>
<div id="si" class="info"></div>
<h2>Funding Needed - Historical Gas Prices</h2>
<p class="note">Based on observed L1 baseFee over the last 30 days. Watchdog_L1 cost is independent of TPS.</p>
<div id="ht"></div>
<h2>Funding Needed - Stress Scenarios</h2>
<p class="note">What if gas goes to 1, 5, 10, or 20 gwei? These levels are common during market events.</p>
<div id="st"></div>
<h2>Watchdog L2</h2>
<div id="wl"></div>

<script>
const D={js_data};
const BG=131072, BB=131072, BPL=110000;
let mode="{default_mode}", tps=0.04;

function setMode(m){{
  mode=m;
  document.getElementById("btn-r").className=m==="rollup"?"active":"";
  document.getElementById("btn-v").className=m==="validium"?"active":"";
  document.getElementById("mi").textContent=m==="rollup"
    ?"Rollup: pubdata posted to L1 via EIP-4844 blobs. Batch frequency scales with TPS."
    :"Validium: pubdata stored off-chain. L1 costs stay flat regardless of TPS. No blob gas.";
  upd();
}}

function fmt(v){{ return v<0.0001?v.toFixed(8):v<1?v.toFixed(6):v.toFixed(4); }}
function fN(v){{ return v<0.0001?'<span class="ok">funded</span>':'<span class="needed">'+fmt(v)+'</span>'; }}

function cBPD(t){{
  const bt=D.l2_block_time, bpd=86400/bt, cb=D.blocks_per_batch;
  const pp=parseInt(document.getElementById("pi").value)||300;
  if(mode==="validium"){{
    const tb=t*bt*cb; let e=cb;
    if(tb>10000) e=Math.max(1,Math.floor(10000/(t*bt)));
    return {{bpd:bpd/e,bl:0,e:e,pub:0}};
  }}
  const tpb=t*bt, ppb=tpb*pp; let e=cb;
  if(ppb>0&&ppb*cb>BPL) e=Math.max(1,Math.floor(BPL/ppb));
  const pb=e*ppb, bl=Math.max(1,Math.ceil(pb/BB));
  return {{bpd:bpd/e,bl:bl,e:e,pub:pb}};
}}

function cC(gw){{
  const i=cBPD(tps), b=i.bpd, bl=i.bl, bg=D.avg_blob_gas_price_gwei;
  const ce=b*D.avg_gas.Commit*gw*1e-9;
  const cb=mode==="rollup"?b*bl*BG*bg*1e-9:0;
  return {{Commit:ce+cb, Prove:b*D.avg_gas.Prove*gw*1e-9,
           Execute:b*D.avg_gas.Execute*gw*1e-9,
           Watchdog_L1:D.wd_l1_txs_day*D.avg_gas.Watchdog_L1*gw*1e-9, info:i}};
}}

function mT(lb,gw){{
  const c=cC(gw), ns=["Commit","Prove","Execute","Watchdog_L1"];
  let h='<table><tr><th>'+lb+' ('+gw.toFixed(3)+' gwei)</th><th>ETH/day</th>'+
    '<th>3mo cost</th><th>3mo needed</th><th>6mo cost</th><th>6mo needed</th>'+
    '<th>12mo cost</th><th>12mo needed</th></tr>';
  let t={{d:0}}; [3,6,12].forEach(m=>{{t["c"+m]=0;t["n"+m]=0;}});
  ns.forEach(n=>{{
    const dc=c[n], bl=D.balances[n]; t.d+=dc;
    h+='<tr><td>'+n+'</td><td>'+fmt(dc)+'</td>';
    [3,6,12].forEach(m=>{{
      const cc=dc*m*30, nn=Math.max(0,cc-bl);
      t["c"+m]+=cc; t["n"+m]+=nn;
      h+='<td>'+fmt(cc)+'</td><td>'+fN(nn)+'</td>';
    }}); h+='</tr>';
  }});
  h+='<tr class="tot"><td>TOTAL</td><td>'+fmt(t.d)+'</td>';
  [3,6,12].forEach(m=>{{h+='<td>'+fmt(t["c"+m])+'</td><td>'+fN(t["n"+m])+'</td>';}});
  return h+'</tr></table>';
}}

function upd(){{
  tps=Math.pow(10,parseFloat(document.getElementById("sl").value));
  document.getElementById("tpsd").textContent=tps<1?tps.toFixed(3):tps.toFixed(1);
  const i=cBPD(tps), td=tps*86400, ml=mode==="rollup"?"Rollup":"Validium";
  let ih='<b>'+ml+'</b> at <b>'+(tps<1?tps.toFixed(3):tps.toFixed(1))+' TPS</b> (~'+
    td.toLocaleString(undefined,{{maximumFractionDigits:0}})+' txs/day): '+
    '<b>'+i.bpd.toFixed(1)+'</b> batches/day, '+i.e+' blocks/batch';
  if(mode==="rollup") ih+=', '+i.bl+' blob(s)/batch, ~'+(i.pub/1024).toFixed(1)+' KB pubdata/batch';
  else ih+=', no L1 pubdata (off-chain DA)';
  document.getElementById("si").innerHTML=ih;
  let hh=''; hh+=mT("30d mean",D.gas_30d.mean); hh+=mT("30d median",D.gas_30d.median); hh+=mT("30d P90",D.gas_30d.p90);
  document.getElementById("ht").innerHTML=hh;
  let sh=''; [1,5,10,20].forEach(g=>{{sh+=mT(g+" gwei",g);}});
  document.getElementById("st").innerHTML=sh;
  const wb=D.balances.Watchdog_L2, wd=D.wd_l2_daily_spend, wr=wd>0?(wb/wd):Infinity;
  document.getElementById("wl").innerHTML=
    '<div class="card" style="display:inline-block"><div class="lb">Watchdog L2</div>'+
    '<div class="vl">'+wb.toFixed(4)+' ETH</div>'+
    '<div class="dt">'+wd.toFixed(6)+' ETH/day | L2 gas: '+D.l2_gas_gwei.toFixed(4)+' gwei</div>'+
    '<div class="dt">Runway: '+(wr>3650?'10+ years':Math.round(wr)+' days')+
    ' (independent of TPS and DA mode)</div></div>';
}}

(function init(){{
  const ns=["Commit","Prove","Execute","Watchdog_L1","Watchdog_L2"];
  let h=''; ns.forEach(n=>{{
    const ch=n.endsWith("L2")?"L2":"L1";
    h+='<div class="card"><div class="lb">'+n+' ('+ch+')</div>'+
      '<div class="vl">'+D.balances[n].toFixed(4)+' ETH</div>'+
      '<div class="dt">'+(n.endsWith("L2")?D.l2_gas_gwei.toFixed(4):D.l1_gas_gwei.toFixed(2))+' gwei gas</div></div>';
  }});
  const tl1=D.balances.Commit+D.balances.Prove+D.balances.Execute+D.balances.Watchdog_L1;
  h+='<div class="card"><div class="lb">Total L1</div><div class="vl">'+tl1.toFixed(4)+' ETH</div>'+
    '<div class="dt">Current L1 gas: '+D.l1_gas_gwei.toFixed(2)+' gwei</div></div>';
  document.getElementById("cards").innerHTML=h;
  setMode("{default_mode}");
}})();
</script>
</body></html>'''


if __name__ == "__main__":
    data = collect()
    html = generate_html(data)
    slug = re.sub(r'[^a-z0-9]+', '_', cfg.chain_name.lower()).strip('_')
    path = f"{slug}_funding.html"
    with open(path, "w") as f:
        f.write(html)
    print(f"Calculator written to: {path}")
    print("Open in a browser to use.")
