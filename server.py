#!/usr/bin/env python3
"""LSR Trading Dashboard — FastAPI backend.

Proxies NT8 bridge data and parses trader logs for a live trading view.
"""

import asyncio
import json
import logging
import math
import os
import re
import time
from collections import OrderedDict, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
import websockets

app = FastAPI(title="LSR Dashboard")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("edge_dashboard")

# --- Config ---
WIN_HOST = "100.66.60.10"
SSH_TARGET = f"ryans@{WIN_HOST}"
BRIDGE = f"http://{WIN_HOST}:8080"
LOG_FILE = r"C:\Users\ryans\clawd\agents\trader\futures\trader-error.log"
CACHE_TTL = 5
CACHE_STALE_TTL = 60
CACHE_MAX_ITEMS = 256
MAX_BRIDGE_BYTES = 4 * 1024 * 1024
MAX_CANDLES = 5000
MAX_SSH_STDOUT_BYTES = 256 * 1024
HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX_REQUESTS = 240
STATUS_CACHE_TTL = 2
ACCOUNT_CACHE_TTL = 5
ORDERS_CACHE_TTL = 2
QUOTES_CACHE_TTL = 1
POSITIONS_CACHE_TTL = 1
LEVELS_CACHE_TTL = 3
TRADES_CACHE_TTL = 2
SIGNALS_CACHE_TTL = 2
CANDLES_CACHE_TTL = 2
SWINGS_CACHE_TTL = 10


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        parsed = int(raw)
        return parsed if parsed > 0 else default
    except ValueError:
        return default


CACHE_TTL = _env_int("DASHBOARD_CACHE_TTL", CACHE_TTL)
CACHE_STALE_TTL = _env_int("DASHBOARD_CACHE_STALE_TTL", CACHE_STALE_TTL)
CACHE_MAX_ITEMS = _env_int("DASHBOARD_CACHE_MAX_ITEMS", CACHE_MAX_ITEMS)
MAX_BRIDGE_BYTES = _env_int("DASHBOARD_MAX_BRIDGE_BYTES", MAX_BRIDGE_BYTES)
MAX_CANDLES = _env_int("DASHBOARD_MAX_CANDLES", MAX_CANDLES)
MAX_SSH_STDOUT_BYTES = _env_int("DASHBOARD_MAX_SSH_STDOUT_BYTES", MAX_SSH_STDOUT_BYTES)
RATE_LIMIT_WINDOW_SECONDS = _env_int("DASHBOARD_RATE_LIMIT_WINDOW_SECONDS", RATE_LIMIT_WINDOW_SECONDS)
RATE_LIMIT_MAX_REQUESTS = _env_int("DASHBOARD_RATE_LIMIT_MAX_REQUESTS", 1800)

# --- Cache ---
_cache: OrderedDict[str, tuple[float, object]] = OrderedDict()
_rate_buckets: defaultdict[str, deque[float]] = defaultdict(deque)
_bridge_state = {
    "consecutive_failures": 0,
    "last_success_ts": None,
    "last_failure_ts": None,
}


def _prune_cache(now: float | None = None):
    now = now or time.time()
    expired = [k for k, (ts, _) in _cache.items() if now - ts >= CACHE_STALE_TTL]
    for key in expired:
        _cache.pop(key, None)
    while len(_cache) > CACHE_MAX_ITEMS:
        _cache.popitem(last=False)


def _cached(key: str, fresh_ttl: int = CACHE_TTL):
    now = time.time()
    _prune_cache(now)
    entry = _cache.get(key)
    if entry and now - entry[0] < fresh_ttl:
        _cache.move_to_end(key)
        return entry[1]
    return None


def _set(key: str, val: object):
    now = time.time()
    _cache[key] = (now, val)
    _cache.move_to_end(key)
    _prune_cache(now)
    return val


def _cached_stale(key: str, max_age: int = CACHE_STALE_TTL):
    now = time.time()
    _prune_cache(now)
    entry = _cache.get(key)
    if entry and now - entry[0] < max_age:
        _cache.move_to_end(key)
        return entry[1]
    return None


def _safe_float(value: str) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _bridge_record_result(ok: bool):
    now = time.time()
    if ok:
        _bridge_state["consecutive_failures"] = 0
        _bridge_state["last_success_ts"] = now
    else:
        _bridge_state["consecutive_failures"] += 1
        _bridge_state["last_failure_ts"] = now


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    # Only protect API HTTP endpoints (exclude websocket path).
    if request.url.path.startswith("/api/") and request.url.path != "/api/ws":
        now = time.time()
        client_ip = request.client.host if request.client else "unknown"
        bucket_key = f"{client_ip}:{request.url.path}"
        bucket = _rate_buckets[bucket_key]
        cutoff = now - RATE_LIMIT_WINDOW_SECONDS
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= RATE_LIMIT_MAX_REQUESTS:
            retry_after = max(1, int(bucket[0] + RATE_LIMIT_WINDOW_SECONDS - now))
            return JSONResponse(
                status_code=429,
                content={"error": "rate limit exceeded", "retryAfterSeconds": retry_after},
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)
    return await call_next(request)


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
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        logger.warning("ssh_grep timed out for pattern=%r", pattern)
        raise
    if len(stdout) > MAX_SSH_STDOUT_BYTES:
        logger.warning("ssh_grep output exceeded %s bytes; truncating", MAX_SSH_STDOUT_BYTES)
        stdout = stdout[-MAX_SSH_STDOUT_BYTES:]
    return stdout.decode("utf-8", errors="replace")


async def bridge_get(path: str) -> dict | list | None:
    url = f"{BRIDGE}{path}"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as c:
            async with c.stream("GET", url) as r:
                r.raise_for_status()
                content_length = r.headers.get("Content-Length")
                if content_length and int(content_length) > MAX_BRIDGE_BYTES:
                    logger.warning("bridge payload too large for %s: %s", path, content_length)
                    return None
                payload = bytearray()
                async for chunk in r.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > MAX_BRIDGE_BYTES:
                        logger.warning("bridge payload exceeded limit for %s", path)
                        _bridge_record_result(False)
                        return None
                parsed = json.loads(payload.decode("utf-8"))
                _bridge_record_result(True)
                return parsed
    except (httpx.HTTPError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        _bridge_record_result(False)
        logger.exception("bridge_get failed for %s", path)
        return None


# --- Endpoints ---

@app.get("/api/status")
async def status():
    cached = _cached("status", STATUS_CACHE_TTL)
    if cached:
        return cached
    bridge = await bridge_get("/status")
    fail_count = _bridge_state["consecutive_failures"]
    if bridge is not None:
        state = "running"
        bridge_ok = True
    elif fail_count < 3:
        state = "degraded"
        bridge_ok = False
    else:
        state = "offline"
        bridge_ok = False
    return _set("status", {"status": state, "bridge": bridge_ok, "ts": datetime.now(timezone.utc).isoformat()})


@app.get("/api/health")
async def health():
    fail_count = _bridge_state["consecutive_failures"]
    status_name = "healthy" if fail_count == 0 else "degraded" if fail_count < 3 else "offline"
    return {
        "status": status_name,
        "bridge": {
            "consecutiveFailures": fail_count,
            "lastSuccessTs": _bridge_state["last_success_ts"],
            "lastFailureTs": _bridge_state["last_failure_ts"],
        },
        "cache": {
            "entries": len(_cache),
            "maxEntries": CACHE_MAX_ITEMS,
            "ttlSeconds": CACHE_TTL,
            "staleTtlSeconds": CACHE_STALE_TTL,
        },
        "rateLimit": {
            "windowSeconds": RATE_LIMIT_WINDOW_SECONDS,
            "maxRequests": RATE_LIMIT_MAX_REQUESTS,
        },
    }


@app.get("/api/account")
async def account():
    cached = _cached("account", ACCOUNT_CACHE_TTL)
    if cached:
        return cached
    data = await bridge_get("/account")
    if data:
        return _set("account", data)
    stale = _cached_stale("account")
    if stale:
        logger.warning("Serving stale account data")
        return stale
    return _set("account", {"error": "bridge unreachable"})


@app.get("/api/positions")
async def positions():
    cached = _cached("positions", POSITIONS_CACHE_TTL)
    if cached:
        return cached
    pos = await bridge_get("/positions")
    orders = await bridge_get("/orders")
    if pos is None:
        stale = _cached_stale("positions")
        if stale:
            logger.warning("Serving stale positions data")
            return stale

    pos_list = []
    if pos and "positions" in pos:
        order_list = orders.get("orders", []) if orders else []
        order_map: dict[str, dict[str, float | None]] = {}
        for o in order_list:
            sym = o.get("symbol")
            if not sym or o.get("state") != "Working":
                continue
            slot = order_map.setdefault(sym, {"sl": None, "tp": None})
            if o.get("orderType") == "StopMarket":
                slot["sl"] = o.get("price")
            elif o.get("orderType") == "Limit":
                slot["tp"] = o.get("price")
        for p in pos["positions"]:
            sym = p.get("symbol", "")
            mapped = order_map.get(sym, {"sl": None, "tp": None})
            pos_list.append({
                "symbol": sym,
                "direction": p.get("direction", ""),
                "quantity": p.get("quantity", 0),
                "avgPrice": p.get("avgPrice", 0),
                "unrealizedPnl": p.get("unrealizedPnl", 0),
                "sl": mapped["sl"],
                "tp": mapped["tp"],
            })
    return _set("positions", {"positions": pos_list})


@app.get("/api/orders")
async def orders():
    cached = _cached("orders", ORDERS_CACHE_TTL)
    if cached:
        return cached
    data = await bridge_get("/orders")
    if data:
        return _set("orders", data)
    stale = _cached_stale("orders")
    if stale:
        logger.warning("Serving stale orders data")
        return stale
    return _set("orders", {"error": "bridge unreachable"})


@app.get("/api/quotes")
async def quotes():
    cached = _cached("quotes", QUOTES_CACHE_TTL)
    if cached:
        return cached
    data = await bridge_get("/quotes")
    if data:
        return _set("quotes", data)
    stale = _cached_stale("quotes")
    if stale:
        logger.warning("Serving stale quotes data")
        return stale
    return _set("quotes", {})


@app.get("/api/levels")
async def levels():
    cached = _cached("levels", LEVELS_CACHE_TTL)
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
            pdh = _safe_float(m.group(2))
            pdl = _safe_float(m.group(3))
            pdc = _safe_float(m.group(4))
            if pdh is None or pdl is None or pdc is None:
                logger.warning("Skipping malformed levels line: %r", line)
                continue
            sym = m.group(1)
            instruments[sym] = {
                "symbol": sym,
                "pdh": pdh,
                "pdl": pdl,
                "pdc": pdc,
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
    cached = _cached("trades", TRADES_CACHE_TTL)
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
            price = _safe_float(m.group(3))
            if price is None:
                logger.warning("Skipping malformed entry line: %r", line)
                continue
            current_entry = {
                "symbol": m.group(1),
                "side": m.group(2),
                "price": price,
            }
            entries.append(current_entry)
            continue

        m = detail_pat.search(line)
        if m and current_entry:
            rr = _safe_float(m.group(1))
            if rr is None:
                logger.warning("Skipping malformed detail line: %r", line)
                current_entry = None
                continue
            current_entry["rr"] = rr
            current_entry["setup"] = m.group(2)
            current_entry["session"] = m.group(3)
            current_entry["time"] = m.group(4)
            current_entry = None
            continue

        m = exit_pat.search(line)
        if m:
            pnl = _safe_float(m.group(3))
            ticks = _safe_float(m.group(2))
            r_actual = _safe_float(m.group(4))
            if pnl is None or ticks is None or r_actual is None:
                logger.warning("Skipping malformed exit line: %r", line)
                continue
            exits.append({
                "symbol": m.group(1),
                "ticks": ticks,
                "pnl": pnl,
                "r": r_actual,
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
    exits_by_symbol: dict[str, deque] = defaultdict(deque)
    for x in exits:
        exits_by_symbol[x["symbol"]].append(x)

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
            x = exits_by_symbol.get(sym, deque())
            if x:
                closed_trade = x.popleft()
                trade["status"] = "closed"
                trade["pnl"] = closed_trade["pnl"]
                trade["ticks"] = closed_trade["ticks"]
                trade["r_actual"] = closed_trade["r"]
                trade["result"] = closed_trade["result"]
            else:
                trade["status"] = "cancelled" if sym in cancelled_syms else "pending"

        trade_list.append(trade)

    # Any unmatched exits (edge case)
    for symbol_exits in exits_by_symbol.values():
        for x in symbol_exits:
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
    cached = _cached("signals", SIGNALS_CACHE_TTL)
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
    symbol: str = Query("ES", min_length=1, max_length=8, pattern=r"^[A-Za-z0-9]+$"),
    tf: str = Query("5m", pattern=r"^(1m|5m|15m|1h)$"),
    hours: int = Query(24, ge=1, le=168),
):
    """Real-time candles from bridge with EMA overlays."""
    symbol = symbol.upper()
    tf_secs = TF_SECONDS[tf]
    cache_key = f"candles:{symbol}:{tf}:{hours}"
    cached = _cached(cache_key, CANDLES_CACHE_TTL)
    if cached:
        return cached

    data = await bridge_get(f"/candles?symbol={symbol}&tf={tf_secs}&hours={hours}")
    if not data or "candles" not in data:
        stale = _cached_stale(cache_key)
        if stale:
            logger.warning("Serving stale candles data for %s %s %s", symbol, tf, hours)
            return stale
        return {"candles": [], "ema20": [], "ema50": [], "ema200": []}

    agg = data["candles"]
    if not isinstance(agg, list):
        logger.warning("Unexpected candles payload type: %s", type(agg).__name__)
        return _set(cache_key, {"candles": [], "ema20": [], "ema50": [], "ema200": []})
    if not agg:
        return _set(cache_key, {"candles": [], "ema20": [], "ema50": [], "ema200": []})

    normalized = []
    closes = []
    for candle in agg:
        if not isinstance(candle, dict):
            continue
        if "time" not in candle or "close" not in candle:
            continue
        close = _safe_float(str(candle["close"]))
        if close is None:
            continue
        normalized.append(candle)
        closes.append(close)

    if len(normalized) > MAX_CANDLES:
        normalized = normalized[-MAX_CANDLES:]
        closes = closes[-MAX_CANDLES:]

    if not normalized:
        return _set(cache_key, {"candles": [], "ema20": [], "ema50": [], "ema200": []})

    ema20_vals = calc_ema(closes, 20)
    ema50_vals = calc_ema(closes, 50)
    ema200_vals = calc_ema(closes, 200)

    ema20 = [{"time": normalized[i]["time"], "value": round(v, 4)} for i, v in enumerate(ema20_vals) if v is not None]
    ema50 = [{"time": normalized[i]["time"], "value": round(v, 4)} for i, v in enumerate(ema50_vals) if v is not None]
    ema200 = [{"time": normalized[i]["time"], "value": round(v, 4)} for i, v in enumerate(ema200_vals) if v is not None]

    return _set(cache_key, {"candles": normalized, "ema20": ema20, "ema50": ema50, "ema200": ema200})


@app.get("/api/swing-points")
async def swing_points(symbol: str | None = Query(None, min_length=1, max_length=8, pattern=r"^[A-Za-z0-9]+$")):
    """Swing points from bridge (persisted by trader)."""
    symbol = symbol.upper() if symbol else None
    cached = _cached(f"swings:{symbol}", SWINGS_CACHE_TTL)
    if cached:
        return cached
    url = "/swing-points"
    if symbol:
        url += f"?symbol={symbol}"
    data = await bridge_get(url)
    if data:
        return _set(f"swings:{symbol}", data)
    stale = _cached_stale(f"swings:{symbol}")
    if stale:
        logger.warning("Serving stale swing points for %s", symbol)
        return stale
    return _set(f"swings:{symbol}", {"swingPoints": []})


@app.websocket("/api/ws")
async def websocket_proxy(websocket: WebSocket):
    """WebSocket proxy to NT8 bridge (for HTTPS compatibility)."""
    await websocket.accept()
    bridge_ws_url = f"ws://{WIN_HOST}:9998"
    
    try:
        async with websockets.connect(
            bridge_ws_url,
            open_timeout=5,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
            max_size=MAX_BRIDGE_BYTES,
        ) as bridge_ws:
            async def forward_to_client():
                async for msg in bridge_ws:
                    try:
                        await asyncio.wait_for(websocket.send_text(msg), timeout=1.0)
                    except asyncio.TimeoutError:
                        logger.warning("Dropping websocket message for slow client")
                        continue

            async def forward_to_bridge():
                while True:
                    try:
                        data = await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
                    except asyncio.TimeoutError:
                        await bridge_ws.ping()
                        continue
                    except WebSocketDisconnect:
                        break
                    await bridge_ws.send(data)

            to_client = asyncio.create_task(forward_to_client())
            to_bridge = asyncio.create_task(forward_to_bridge())
            done, pending = await asyncio.wait({to_client, to_bridge}, return_when=asyncio.FIRST_EXCEPTION)
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception()
                if exc:
                    raise exc
    except Exception:
        logger.exception("websocket_proxy failed")
    finally:
        await websocket.close()


# --- Static files ---
# Serve static files without catching API routes

STATIC_DIR = Path(__file__).parent / "static"
INDEX_FILE = STATIC_DIR / "index.html"
INDEX_HTML = INDEX_FILE.read_text(encoding="utf-8") if INDEX_FILE.exists() else None

@app.exception_handler(404)
async def custom_404_handler(request, exc):
    """Serve static files for non-API routes when no API endpoint matches."""
    path = request.url.path
    
    # Never intercept /api/* routes - return 404 as-is
    if path.startswith("/api/"):
        raise exc
    
    # Serve static files
    if path == "/":
        if INDEX_HTML:
            return HTMLResponse(INDEX_HTML)
    else:
        # Remove leading slash for file lookup
        file_path = STATIC_DIR / path.lstrip("/")
        if file_path.exists() and file_path.is_file():
            return FileResponse(file_path)
    
    # If no static file found, try index.html (SPA fallback)
    if INDEX_HTML:
        return HTMLResponse(INDEX_HTML)
    
    raise exc

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3004)
