#!/usr/bin/env python3
"""LSR Trading Dashboard — FastAPI backend."""

import asyncio
import re
import time
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

app = FastAPI(title="LSR Dashboard")

# --- Config ---
WIN_HOST = "100.66.60.10"
SSH_TARGET = f"ryans@{WIN_HOST}"
BRIDGE = f"http://{WIN_HOST}:8080"
LOG_DIR = r"C:\Users\ryans\clawd\agents\trader\futures"
LOG_FILE = f"{LOG_DIR}\\trader-error.log"  # All output goes here
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


async def ssh_read(remote_path: str, tail: int | None = None) -> str:
    cmd = f"type \"{remote_path}\""
    if tail:
        cmd = f"powershell -Command \"Get-Content '{remote_path}' -Tail {tail}\""
    proc = await asyncio.create_subprocess_exec(
        "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
        SSH_TARGET, cmd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    return stdout.decode("utf-8", errors="replace")


async def ssh_grep(remote_path: str, pattern: str, last: int = 30) -> str:
    """Use Select-String on Windows for efficient log searching."""
    cmd = (
        f"powershell -Command \"Select-String -Path '{remote_path}' "
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
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "ConnectTimeout=3", "-o", "StrictHostKeyChecking=no",
            SSH_TARGET,
            'powershell -Command "Get-Process -Name NinjaTrader* -ErrorAction SilentlyContinue | Select-Object -First 1 | ForEach-Object { $_.ProcessName }"',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
        running = bool(stdout.strip())
        result = {"status": "running" if running else "stopped", "ts": datetime.utcnow().isoformat()}
    except Exception:
        result = {"status": "unknown", "ts": datetime.utcnow().isoformat()}
    return _set("status", result)


@app.get("/api/account")
async def account():
    cached = _cached("account")
    if cached:
        return cached
    data = await bridge_get("/account")
    if data is None:
        return {"error": "bridge unreachable"}
    return _set("account", data)


@app.get("/api/positions")
async def positions():
    cached = _cached("positions")
    if cached:
        return cached
    data = await bridge_get("/positions")
    if data is None:
        return {"error": "bridge unreachable"}
    return _set("positions", data)


@app.get("/api/quotes")
async def quotes():
    cached = _cached("quotes")
    if cached:
        return cached
    data = await bridge_get("/quotes")
    if data is None:
        return _set("quotes", {})
    return _set("quotes", data)


@app.get("/api/levels")
async def levels():
    cached = _cached("levels")
    if cached:
        return cached
    try:
        # Format: "NQ LSR SCAN: PDH=$25360.5 PDL=$25100.0 PDC=$25278.8"
        text = await ssh_grep(LOG_FILE, "LSR SCAN:", last=50)
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

    # Merge live quotes
    quote_data = await bridge_get("/quotes")
    if quote_data:
        for inst in instruments.values():
            q = quote_data.get(inst["symbol"])
            if q:
                inst["last"] = q.get("last")
                inst["bid"] = q.get("bid")
                inst["ask"] = q.get("ask")

    return _set("levels", {"instruments": list(instruments.values())})


TICK_VALUES = {
    "ES": (0.25, 12.50), "NQ": (0.25, 5.00), "CL": (0.01, 10.00),
    "GC": (0.10, 10.00), "SI": (0.005, 25.00),
}


@app.get("/api/trades")
async def trades():
    cached = _cached("trades")
    if cached:
        return cached

    # Get real P&L from "Trade logged" lines
    try:
        exit_text = await ssh_grep(LOG_FILE, "Trade logged", last=50)
    except Exception:
        exit_text = ""

    # Parse exits: "✅ [NQ] Trade logged (NT8 executed): +142.0 ticks | $+1775.00 | +14.20R"
    exit_pat = re.compile(
        r"\[(\w+)\]\s+Trade logged.*?([+-]?[\d.]+)\s*ticks\s*\|\s*\$([+-]?[\d.]+)\s*\|\s*([+-]?[\d.]+)R"
    )
    exits = []
    for line in exit_text.splitlines():
        m = exit_pat.search(line)
        if m:
            pnl_dollars = float(m.group(3))
            exits.append({
                "symbol": m.group(1),
                "ticks": float(m.group(2)),
                "pnl": pnl_dollars,
                "r": float(m.group(4)),
                "result": "win" if pnl_dollars > 0 else "loss",
            })

    # Get entries for context
    try:
        entry_text = await ssh_grep(
            LOG_FILE,
            "PLACING LIMIT|Current Price|Canceling LIMIT",
            last=60,
        )
    except Exception:
        entry_text = ""

    # Get open positions
    pos_data = await bridge_get("/positions")
    pos_map = {}
    if pos_data and "positions" in pos_data:
        for p in pos_data["positions"]:
            pos_map[p.get("symbol", "")] = p

    place_pat = re.compile(
        r"\[(\w+)\]\s+PLACING LIMIT ORDER:\s+(BUY|SELL)\s+\w+\s+@\s+([\d.]+)"
    )
    detail_pat = re.compile(
        r"Current Price:\s+([\d.]+)\s*\|\s*Entry:\s+([\d.]+)\s*\|\s*Stop:\s+([\d.]+)\s*\|\s*Target:\s+([\d.]+)"
    )
    cancel_pat = re.compile(r"\[(\w+)\]\s+Canceling LIMIT order.*expired")

    entries = []
    for line in entry_text.splitlines():
        m = place_pat.search(line)
        if m:
            entries.append({
                "symbol": m.group(1),
                "side": m.group(2),
                "price": float(m.group(3)),
            })
            continue
        m = detail_pat.search(line)
        if m and entries and "stop" not in entries[-1]:
            entries[-1]["stop"] = float(m.group(3))
            entries[-1]["target"] = float(m.group(4))
            continue
        m = cancel_pat.search(line)
        if m:
            entries.append({"type": "cancel", "symbol": m.group(1), "reason": "expired"})

    # Tag open entries
    for e in entries:
        if e.get("type") == "cancel":
            continue
        sym = e.get("symbol", "")
        pos = pos_map.get(sym)
        if pos and abs(pos.get("avgPrice", 0) - e.get("price", 0)) < 1:
            e["status"] = "open"
            e["pnl"] = pos.get("unrealizedPnl", 0)

    # Build final trade list: real exits first, then recent entries
    trade_list = []

    for ex in exits[-20:]:
        trade_list.append({
            "type": "exit",
            "symbol": ex["symbol"],
            "ticks": ex["ticks"],
            "pnl": ex["pnl"],
            "r": ex["r"],
            "result": ex["result"],
        })

    for e in entries[-15:]:
        if e.get("type") == "cancel":
            trade_list.append(e)
        elif e.get("status") == "open":
            trade_list.append({
                "type": "open",
                "symbol": e["symbol"],
                "side": e["side"],
                "price": e["price"],
                "stop": e.get("stop"),
                "target": e.get("target"),
                "pnl": e.get("pnl", 0),
            })

    return _set("trades", {"trades": trade_list})


@app.get("/api/signals")
async def signals():
    cached = _cached("signals")
    if cached:
        return cached
    try:
        # Get meaningful signals: sweeps, entries, rejections, cancels — not candle loading spam
        text = await ssh_grep(
            LOG_FILE,
            "SWEEP|PLACING|RECLAIM|ENTRY|ORDER|Cancel|expired|SIGNAL|reject|LSR SCAN|fill",
            last=30,
        )
    except Exception:
        return _set("signals", {"signals": []})

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    # Deduplicate consecutive identical lines
    deduped = []
    for line in lines:
        if not deduped or line != deduped[-1]:
            deduped.append(line)
    return _set("signals", {"signals": deduped[-30:]})


# --- Static files ---
app.mount("/", StaticFiles(directory=str(Path(__file__).parent / "static"), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3004)
