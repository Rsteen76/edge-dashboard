#!/usr/bin/env python3
"""LSR Trading Dashboard — FastAPI backend.

Proxies NT8 bridge data and parses trader logs for a live trading view.
"""

import asyncio
import math
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="LSR Dashboard")

# --- Config ---
WIN_HOST = "100.66.60.10"
SSH_TARGET = f"ryans@{WIN_HOST}"
BRIDGE = f"http://{WIN_HOST}:8080"
LOG_FILE = r"C:\Users\ryans\clawd\agents\trader\futures\trader-error.log"
DB_REMOTE = "C:/Users/ryans/clawd/agents/trader/futures/futures_trades.db"
DB_LOCAL = "/tmp/futures_trades.db"
DB_SYNC_INTERVAL = 300  # 5 minutes
CACHE_TTL = 5

# --- Cache ---
_cache: dict[str, tuple[float, object]] = {}


def _cached(key: str):
    entry = _cache.get(key)
    if entry and time.time() - entry[0] < CACHE_TTL:
        return entry[1]
    return None


def _set(key: str, val: object):
    _cache[key] = (time.time(), val)
    return val


_db_last_sync: float = 0


async def sync_db():
    """SCP the trader DB from Windows. Non-blocking."""
    global _db_last_sync
    if time.time() - _db_last_sync < DB_SYNC_INTERVAL:
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            "scp", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
            f"{SSH_TARGET}:{DB_REMOTE}", DB_LOCAL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)
        _db_last_sync = time.time()
    except Exception:
        pass  # Use stale cache


def get_db() -> sqlite3.Connection | None:
    """Open local copy of trader DB."""
    try:
        return sqlite3.connect(f"file:{DB_LOCAL}?mode=ro", uri=True)
    except Exception:
        return None


def calc_ema(closes: list[float], period: int) -> list[float | None]:
    """Calculate EMA for a list of closes. Returns same-length list."""
    if len(closes) < period:
        return [None] * len(closes)
    k = 2 / (period + 1)
    ema = [None] * (period - 1)
    # Seed with SMA
    ema.append(sum(closes[:period]) / period)
    for i in range(period, len(closes)):
        ema.append(closes[i] * k + ema[-1] * (1 - k))
    return ema


TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}


def aggregate_candles(rows: list, tf_secs: int) -> list[dict]:
    """Aggregate 15s candle rows into larger timeframe.
    rows: [(timestamp, open, high, low, close, volume), ...]
    """
    if not rows:
        return []
    buckets: dict[int, list] = {}
    for ts, o, h, l, c, v in rows:
        bucket = (ts // tf_secs) * tf_secs
        if bucket not in buckets:
            buckets[bucket] = []
        buckets[bucket].append((ts, o, h, l, c, v))

    candles = []
    for bucket in sorted(buckets):
        bars = buckets[bucket]
        bars.sort(key=lambda x: x[0])
        candles.append({
            "time": bucket,
            "open": bars[0][1],
            "high": max(b[2] for b in bars),
            "low": min(b[3] for b in bars),
            "close": bars[-1][4],
            "volume": sum(b[5] for b in bars),
        })
    return candles


def strip_prefix(line: str) -> str:
    """Remove INFO:module: log prefixes."""
    return re.sub(r"^(INFO|WARNING|ERROR|DEBUG):\S+:", "", line).strip()


async def ssh_grep(pattern: str, last: int = 30) -> str:
    cmd = (
        f"powershell -Command \"Select-String -Path '{LOG_FILE}' "
        f"-Pattern '{pattern}' | Select-Object -Last {last} | "
        f"ForEach-Object {{ $_.Line }}\""
    )
    proc = await asyncio.create_subprocess_exec(
        "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
        SSH_TARGET, cmd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    return stdout.decode("utf-8", errors="replace")


async def bridge_get(path: str) -> dict | list | None:
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{BRIDGE}{path}")
            r.raise_for_status()
            return r.json()
    except Exception:
        return None


# --- Endpoints ---

@app.get("/api/status")
async def status():
    cached = _cached("status")
    if cached:
        return cached
    bridge = await bridge_get("/status")
    ok = bridge is not None
    return _set("status", {"status": "running" if ok else "offline", "bridge": ok, "ts": datetime.utcnow().isoformat()})


@app.get("/api/account")
async def account():
    cached = _cached("account")
    if cached:
        return cached
    data = await bridge_get("/account")
    return _set("account", data or {"error": "bridge unreachable"})


@app.get("/api/positions")
async def positions():
    cached = _cached("positions")
    if cached:
        return cached
    pos = await bridge_get("/positions")
    orders = await bridge_get("/orders")

    pos_list = []
    if pos and "positions" in pos:
        order_list = orders.get("orders", []) if orders else []
        for p in pos["positions"]:
            sym = p.get("symbol", "")
            sl = tp = None
            for o in order_list:
                if o.get("symbol") != sym or o.get("state") != "Working":
                    continue
                if o.get("orderType") == "StopMarket":
                    sl = o.get("price")
                elif o.get("orderType") == "Limit":
                    tp = o.get("price")
            pos_list.append({
                "symbol": sym,
                "direction": p.get("direction", ""),
                "quantity": p.get("quantity", 0),
                "avgPrice": p.get("avgPrice", 0),
                "unrealizedPnl": p.get("unrealizedPnl", 0),
                "sl": sl,
                "tp": tp,
            })
    return _set("positions", {"positions": pos_list})


@app.get("/api/orders")
async def orders():
    cached = _cached("orders")
    if cached:
        return cached
    data = await bridge_get("/orders")
    return _set("orders", data or {"error": "bridge unreachable"})


@app.get("/api/quotes")
async def quotes():
    cached = _cached("quotes")
    if cached:
        return cached
    data = await bridge_get("/quotes")
    return _set("quotes", data or {})


@app.get("/api/levels")
async def levels():
    cached = _cached("levels")
    if cached:
        return cached
    try:
        text = await ssh_grep("LSR SCAN:", last=50)
    except Exception:
        return _set("levels", {"instruments": []})

    instruments: dict[str, dict] = {}
    pat = re.compile(
        r"(\w{2})\s+LSR SCAN:\s+PDH=\$?([\d.]+)\s+PDL=\$?([\d.]+)\s+PDC=\$?([\d.]+)",
    )
    for line in text.splitlines():
        m = pat.search(line)
        if m:
            sym = m.group(1)
            instruments[sym] = {
                "symbol": sym,
                "pdh": float(m.group(2)),
                "pdl": float(m.group(3)),
                "pdc": float(m.group(4)),
            }

    quote_data = await bridge_get("/quotes")
    if quote_data:
        for inst in instruments.values():
            q = quote_data.get(inst["symbol"])
            if q:
                inst["last"] = q.get("last")
                inst["bid"] = q.get("bid")
                inst["ask"] = q.get("ask")

    return _set("levels", {"instruments": list(instruments.values())})


@app.get("/api/trades")
async def trades():
    """Session-scoped trades: only from latest trader restart."""
    cached = _cached("trades")
    if cached:
        return cached

    try:
        text = await ssh_grep(
            "Startup levels|PLACING LIMIT|Expected R:R|Trade logged|Canceling LIMIT|FILLED",
            last=250,
        )
    except Exception:
        text = ""

    lines = text.splitlines()

    # Find last "Startup levels" — everything after is current session
    session_start = 0
    for i, line in enumerate(lines):
        if "Startup levels" in line:
            session_start = i
    session_lines = lines[session_start:]

    # Patterns
    place_pat = re.compile(
        r"\[(\w+)\]\s+PLACING LIMIT ORDER:\s+(BUY|SELL)\s+\w+\s+@\s+([\d.]+)"
    )
    detail_pat = re.compile(
        r"Expected R:R:\s+([\d.]+)R\s*\|\s*Setup:\s+(\S+)\s*\|\s*Session:\s+(\S+)\s*\|\s*Time:\s+([\d:]+)\s+PT"
    )
    exit_pat = re.compile(
        r"\[(\w+)\]\s+Trade logged.*?([+-]?[\d.]+)\s*ticks\s*\|\s*\$([+-]?[\d.]+)\s*\|\s*([+-]?[\d.]+)R"
    )
    cancel_pat = re.compile(r"\[(\w+)\]\s+Canceling LIMIT order")

    entries = []
    exits = []
    cancelled_syms = set()
    current_entry = None

    for line in session_lines:
        m = place_pat.search(line)
        if m:
            current_entry = {
                "symbol": m.group(1),
                "side": m.group(2),
                "price": float(m.group(3)),
            }
            entries.append(current_entry)
            continue

        m = detail_pat.search(line)
        if m and current_entry:
            current_entry["rr"] = float(m.group(1))
            current_entry["setup"] = m.group(2)
            current_entry["session"] = m.group(3)
            current_entry["time"] = m.group(4)
            current_entry = None
            continue

        m = exit_pat.search(line)
        if m:
            pnl = float(m.group(3))
            exits.append({
                "symbol": m.group(1),
                "ticks": float(m.group(2)),
                "pnl": pnl,
                "r": float(m.group(4)),
                "result": "win" if pnl > 0 else "loss",
            })
            continue

        m = cancel_pat.search(line)
        if m:
            cancelled_syms.add(m.group(1))

    # Live state from bridge
    pos_data = await bridge_get("/positions")
    pos_map = {}
    if pos_data and "positions" in pos_data:
        for p in pos_data["positions"]:
            pos_map[p.get("symbol", "")] = p

    # Build trade list — each entry becomes a trade record
    trade_list = []
    exit_pool = list(exits)  # copy for consumption

    for e in entries:
        sym = e["symbol"]
        pos = pos_map.get(sym)

        trade = {
            "symbol": sym,
            "side": e["side"],
            "price": e["price"],
            "rr": e.get("rr"),
            "setup": e.get("setup"),
            "session": e.get("session"),
            "time": e.get("time"),
        }

        # Is this the currently open position? Match direction too.
        direction_match = (
            pos and (
                (e["side"] == "BUY" and pos.get("direction") == "Long") or
                (e["side"] == "SELL" and pos.get("direction") == "Short")
            )
        )
        if direction_match and abs(pos.get("avgPrice", 0) - e["price"]) < 2:
            trade["status"] = "open"
            trade["pnl"] = pos.get("unrealizedPnl", 0)
            trade["direction"] = pos.get("direction", "")
        else:
            # Check for matching exit (consume first match)
            matched = False
            for j, x in enumerate(exit_pool):
                if x["symbol"] == sym:
                    trade["status"] = "closed"
                    trade["pnl"] = x["pnl"]
                    trade["ticks"] = x["ticks"]
                    trade["r_actual"] = x["r"]
                    trade["result"] = x["result"]
                    exit_pool.pop(j)
                    matched = True
                    break
            if not matched:
                trade["status"] = "cancelled" if sym in cancelled_syms else "pending"

        trade_list.append(trade)

    # Any unmatched exits (edge case)
    for x in exit_pool:
        trade_list.append({
            "symbol": x["symbol"],
            "status": "closed",
            "pnl": x["pnl"],
            "ticks": x["ticks"],
            "r_actual": x["r"],
            "result": x["result"],
        })

    # Summary
    closed = [t for t in trade_list if t.get("status") == "closed"]
    wins = sum(1 for t in closed if t.get("result") == "win")
    losses = len(closed) - wins
    total_pnl = sum(t.get("pnl", 0) for t in closed)

    return _set("trades", {
        "trades": trade_list,
        "summary": {
            "total": len(closed),
            "wins": wins,
            "losses": losses,
            "winRate": round(wins / len(closed) * 100, 1) if closed else 0,
            "pnl": total_pnl,
        },
    })


@app.get("/api/signals")
async def signals():
    cached = _cached("signals")
    if cached:
        return cached
    try:
        text = await ssh_grep(
            "SWEEP|PLACING|RECLAIM|FILLED|Cancel|expired|reject|Trade logged|PENDING|ORDER placed|ORDER|Startup",
            last=40,
        )
    except Exception:
        return _set("signals", {"signals": []})

    # Find session boundary
    raw_lines = text.splitlines()
    session_start = 0
    for i, line in enumerate(raw_lines):
        if "Startup levels" in line:
            session_start = i

    lines = raw_lines[session_start:]

    # Clean and filter
    cleaned = []
    for line in lines:
        line = strip_prefix(line).strip()
        if not line:
            continue
        # Skip noise
        if "LSR SCAN:" in line:
            continue
        if line.startswith("Placing order:") or line.startswith("{"):
            continue
        if "Order will fill when price returns" in line:
            continue
        if "Order placed: {" in line or "Order canceled: {" in line:
            continue
        if line.startswith("Canceling order None"):
            continue
        if "LIMIT order placed, waiting for fill" in line:
            continue
        if not cleaned or line != cleaned[-1]:
            cleaned.append(line)

    return _set("signals", {"signals": cleaned[-30:]})


@app.get("/api/candles")
async def candles(
    symbol: str = Query("ES"),
    tf: str = Query("5m"),
    hours: int = Query(24),
):
    """Aggregated candles with EMA overlays from trader DB."""
    await sync_db()
    db = get_db()
    if not db:
        return {"error": "DB not available", "candles": [], "ema20": [], "ema50": [], "ema200": []}

    tf_secs = TF_SECONDS.get(tf, 300)
    cutoff = int(time.time()) - (hours * 3600)

    try:
        rows = db.execute(
            "SELECT timestamp, open, high, low, close, volume FROM candles_15s "
            "WHERE symbol = ? AND timestamp >= ? ORDER BY timestamp",
            (symbol, cutoff),
        ).fetchall()
        db.close()
    except Exception:
        return {"error": "DB query failed", "candles": [], "ema20": [], "ema50": [], "ema200": []}

    agg = aggregate_candles(rows, tf_secs)
    if not agg:
        return {"candles": [], "ema20": [], "ema50": [], "ema200": []}

    closes = [c["close"] for c in agg]
    ema20_vals = calc_ema(closes, 20)
    ema50_vals = calc_ema(closes, 50)
    ema200_vals = calc_ema(closes, 200)

    ema20 = [{"time": agg[i]["time"], "value": round(v, 4)} for i, v in enumerate(ema20_vals) if v is not None]
    ema50 = [{"time": agg[i]["time"], "value": round(v, 4)} for i, v in enumerate(ema50_vals) if v is not None]
    ema200 = [{"time": agg[i]["time"], "value": round(v, 4)} for i, v in enumerate(ema200_vals) if v is not None]

    return {"candles": agg, "ema20": ema20, "ema50": ema50, "ema200": ema200}


@app.get("/api/chart-trades")
async def chart_trades(symbol: str = Query("ES")):
    """Trades for a specific symbol from the DB."""
    await sync_db()
    db = get_db()
    if not db:
        return {"trades": []}

    try:
        rows = db.execute(
            "SELECT trade_id, direction, entry_price, entry_time, exit_price, exit_time, "
            "stop, target, r_multiple, pnl_dollars, status, session, setup_type "
            "FROM futures_trades WHERE symbol = ? ORDER BY entry_time DESC LIMIT 20",
            (symbol,),
        ).fetchall()
        db.close()
    except Exception:
        return {"trades": []}

    cols = ["trade_id", "direction", "entry_price", "entry_time", "exit_price", "exit_time",
            "stop", "target", "r_multiple", "pnl_dollars", "status", "session", "setup_type"]
    return {"trades": [dict(zip(cols, r)) for r in rows]}


@app.on_event("startup")
async def startup_sync():
    """Sync DB on startup."""
    await sync_db()


# --- Static files ---
app.mount("/", StaticFiles(directory=str(Path(__file__).parent / "static"), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3004)
