#!/usr/bin/env python3
"""LSR Trading Dashboard — FastAPI backend.

Proxies NT8 bridge data and parses trader logs for a live trading view.
"""

import asyncio
import re
import time
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, Query, WebSocket
from fastapi.staticfiles import StaticFiles
import websockets

app = FastAPI(title="LSR Dashboard")

# --- Config ---
WIN_HOST = "100.66.60.10"
SSH_TARGET = f"ryans@{WIN_HOST}"
BRIDGE = f"http://{WIN_HOST}:8080"
LOG_FILE = r"C:\Users\ryans\clawd\agents\trader\futures\trader-error.log"
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


def calc_ema(closes: list[float], period: int) -> list[float | None]:
    """Calculate EMA for a list of closes. Returns same-length list."""
    if len(closes) < period:
        return [None] * len(closes)
    k = 2 / (period + 1)
    ema = [None] * (period - 1)
    ema.append(sum(closes[:period]) / period)
    for i in range(period, len(closes)):
        ema.append(closes[i] * k + ema[-1] * (1 - k))
    return ema


TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}


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
    """Real-time candles from bridge with EMA overlays."""
    tf_secs = TF_SECONDS.get(tf, 300)
    cache_key = f"candles:{symbol}:{tf}:{hours}"
    cached = _cached(cache_key)
    if cached:
        return cached

    data = await bridge_get(f"/candles?symbol={symbol}&tf={tf_secs}&hours={hours}")
    if not data or "candles" not in data:
        return {"candles": [], "ema20": [], "ema50": [], "ema200": []}

    agg = data["candles"]
    if not agg:
        return _set(cache_key, {"candles": [], "ema20": [], "ema50": [], "ema200": []})

    closes = [c["close"] for c in agg]
    ema20_vals = calc_ema(closes, 20)
    ema50_vals = calc_ema(closes, 50)
    ema200_vals = calc_ema(closes, 200)

    ema20 = [{"time": agg[i]["time"], "value": round(v, 4)} for i, v in enumerate(ema20_vals) if v is not None]
    ema50 = [{"time": agg[i]["time"], "value": round(v, 4)} for i, v in enumerate(ema50_vals) if v is not None]
    ema200 = [{"time": agg[i]["time"], "value": round(v, 4)} for i, v in enumerate(ema200_vals) if v is not None]

    return _set(cache_key, {"candles": agg, "ema20": ema20, "ema50": ema50, "ema200": ema200})


@app.get("/api/swing-points")
async def swing_points(symbol: str = Query(None)):
    """Swing points from bridge (persisted by trader)."""
    cached = _cached(f"swings:{symbol}")
    if cached:
        return cached
    url = "/swing-points"
    if symbol:
        url += f"?symbol={symbol}"
    data = await bridge_get(url)
    return _set(f"swings:{symbol}", data or {"swingPoints": []})


@app.websocket("/api/ws")
async def websocket_proxy(websocket: WebSocket):
    """WebSocket proxy to NT8 bridge (for HTTPS compatibility)."""
    await websocket.accept()
    bridge_ws_url = f"ws://{WIN_HOST}:9998"
    
    try:
        async with websockets.connect(bridge_ws_url) as bridge_ws:
            async def forward_to_client():
                async for msg in bridge_ws:
                    await websocket.send_text(msg)
            
            async def forward_to_bridge():
                while True:
                    data = await websocket.receive_text()
                    await bridge_ws.send(data)
            
            await asyncio.gather(
                forward_to_client(),
                forward_to_bridge(),
                return_exceptions=True
            )
    except Exception:
        pass
    finally:
        await websocket.close()


# --- Static files ---
# Serve static files without catching API routes
from fastapi.responses import FileResponse, HTMLResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

STATIC_DIR = Path(__file__).parent / "static"

@app.exception_handler(404)
async def custom_404_handler(request, exc):
    """Serve static files for non-API routes when no API endpoint matches."""
    path = request.url.path
    
    # Never intercept /api/* routes - return 404 as-is
    if path.startswith("/api/"):
        raise exc
    
    # Serve static files
    if path == "/":
        index_file = STATIC_DIR / "index.html"
        if index_file.exists():
            return HTMLResponse(index_file.read_text())
    else:
        # Remove leading slash for file lookup
        file_path = STATIC_DIR / path.lstrip("/")
        if file_path.exists() and file_path.is_file():
            return FileResponse(file_path)
    
    # If no static file found, try index.html (SPA fallback)
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return HTMLResponse(index_file.read_text())
    
    raise exc

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3004)
