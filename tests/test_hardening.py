import unittest

from fastapi.testclient import TestClient

import server


class CacheTests(unittest.TestCase):
    def setUp(self):
        server._cache.clear()
        server._rate_buckets.clear()
        server._bridge_state["consecutive_failures"] = 0
        server._bridge_state["last_success_ts"] = None
        server._bridge_state["last_failure_ts"] = None

    def test_cache_has_max_size(self):
        for i in range(server.CACHE_MAX_ITEMS + 50):
            server._set(f"key-{i}", i)
        self.assertLessEqual(len(server._cache), server.CACHE_MAX_ITEMS)

    def test_stale_fallback_works(self):
        server._set("account", {"balance": 99999})
        ts, val = server._cache["account"]
        server._cache["account"] = (ts - server.CACHE_TTL - 1, val)
        fresh = server._cached("account")
        self.assertIsNone(fresh)
        stale = server._cached_stale("account")
        self.assertEqual(stale, {"balance": 99999})


class NormalizationTests(unittest.TestCase):
    def test_normalize_symbol_strips_contract(self):
        self.assertEqual(server._normalize_symbol("ES 03-26"), "ES")
        self.assertEqual(server._normalize_symbol("NQ 06-26"), "NQ")
        self.assertEqual(server._normalize_symbol("CL"), "CL")
        self.assertEqual(server._normalize_symbol(""), "")

    def test_normalize_quotes_dict_keyed_by_symbol(self):
        raw = {"ES": {"last": 5000}, "NQ": {"last": 20000}}
        result = server._normalize_quotes(raw)
        self.assertIn("ES", result)
        self.assertEqual(result["ES"]["last"], 5000)

    def test_normalize_quotes_array_of_objects(self):
        raw = [{"symbol": "ES", "last": 5000}, {"symbol": "NQ", "last": 20000}]
        result = server._normalize_quotes(raw)
        self.assertIn("ES", result)
        self.assertIn("NQ", result)

    def test_normalize_quotes_wrapped_in_quotes_key(self):
        raw = {"quotes": [{"symbol": "ES", "last": 5000}]}
        result = server._normalize_quotes(raw)
        self.assertIn("ES", result)

    def test_normalize_quotes_none(self):
        self.assertEqual(server._normalize_quotes(None), {})

    def test_normalize_quotes_strips_contract_from_keys(self):
        raw = {"ES 03-26": {"last": 5000}}
        result = server._normalize_quotes(raw)
        self.assertIn("ES", result)

    def test_normalize_candle_lowercase(self):
        raw = {"time": 100, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}
        result = server._normalize_candle(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result["time"], 100)
        self.assertEqual(result["close"], 1.5)

    def test_normalize_candle_pascalcase(self):
        raw = {"Time": 100, "Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5}
        result = server._normalize_candle(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result["time"], 100)
        self.assertEqual(result["close"], 1.5)

    def test_normalize_candle_missing_time(self):
        raw = {"open": 1.0, "close": 1.5}
        self.assertIsNone(server._normalize_candle(raw))

    def test_normalize_candle_missing_close(self):
        raw = {"time": 100, "open": 1.0}
        self.assertIsNone(server._normalize_candle(raw))

    def test_safe_float_rejects_nan_inf(self):
        self.assertIsNone(server._safe_float("nan"))
        self.assertIsNone(server._safe_float("inf"))
        self.assertIsNone(server._safe_float("-inf"))
        self.assertIsNone(server._safe_float("abc"))
        self.assertEqual(server._safe_float("123.45"), 123.45)
        self.assertEqual(server._safe_float(99), 99.0)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        server._cache.clear()
        server._rate_buckets.clear()
        server._bridge_state["consecutive_failures"] = 0
        server._bridge_state["last_success_ts"] = None
        server._bridge_state["last_failure_ts"] = None
        self.client = TestClient(server.app)

    def test_candles_hours_is_bounded(self):
        r = self.client.get("/api/candles?hours=999999")
        self.assertEqual(r.status_code, 422)

    def test_candles_rejects_invalid_timeframe(self):
        r = self.client.get("/api/candles?tf=30s")
        self.assertEqual(r.status_code, 422)

    def test_candles_filters_bad_candle_rows(self):
        orig = server.bridge_get

        async def fake(path):
            return {
                "candles": [
                    {"time": 1, "open": 1, "high": 2, "low": 0.5, "close": 1.5},
                    {"time": 2, "open": 1, "high": 2, "low": 0.5, "close": "bad"},
                    {"open": 1, "close": 1.2},
                ]
            }

        server.bridge_get = fake
        try:
            r = self.client.get("/api/candles")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(len(r.json()["candles"]), 1)
            self.assertEqual(r.json()["candles"][0]["time"], 1)
        finally:
            server.bridge_get = orig

    def test_candles_normalizes_pascalcase_fields(self):
        orig = server.bridge_get

        async def fake(path):
            return {
                "candles": [
                    {"Time": 1, "Open": 100, "High": 110, "Low": 90, "Close": 105},
                ]
            }

        server.bridge_get = fake
        try:
            r = self.client.get("/api/candles")
            self.assertEqual(r.status_code, 200)
            candles = r.json()["candles"]
            self.assertEqual(len(candles), 1)
            self.assertEqual(candles[0]["time"], 1)
            self.assertEqual(candles[0]["close"], 105.0)
        finally:
            server.bridge_get = orig

    def test_levels_skips_malformed_numeric_values(self):
        orig_ssh = server.ssh_grep
        orig_bridge = server.bridge_get

        async def fake_ssh(pattern, last=30):
            return (
                "ES LSR SCAN: PDH=$5000.5 PDL=$4980.25 PDC=$4995.0\n"
                "NQ LSR SCAN: PDH=$. PDL=$20000.0 PDC=$19900.0\n"
            )

        async def fake_bridge(path):
            return None

        server.ssh_grep = fake_ssh
        server.bridge_get = fake_bridge
        try:
            r = self.client.get("/api/levels")
            self.assertEqual(r.status_code, 200)
            instruments = r.json().get("instruments", [])
            self.assertEqual(len(instruments), 1)
            self.assertEqual(instruments[0]["symbol"], "ES")
        finally:
            server.ssh_grep = orig_ssh
            server.bridge_get = orig_bridge

    def test_quotes_endpoint_normalizes_response(self):
        orig = server.bridge_get

        async def fake(path):
            return [{"symbol": "ES", "last": 5000}, {"symbol": "NQ", "last": 20000}]

        server.bridge_get = fake
        try:
            r = self.client.get("/api/quotes")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertIn("quotes", body)
            self.assertIn("ES", body["quotes"])
            self.assertEqual(body["quotes"]["ES"]["last"], 5000)
        finally:
            server.bridge_get = orig

    def test_account_serves_stale_when_bridge_down(self):
        orig = server.bridge_get

        async def fake(path):
            return None

        server._set("account", {"balance": 12345})
        import time
        ts, val = server._cache["account"]
        server._cache["account"] = (ts - server.CACHE_TTL - 1, val)

        server.bridge_get = fake
        try:
            r = self.client.get("/api/account")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["balance"], 12345)
        finally:
            server.bridge_get = orig

    def test_health_endpoint(self):
        server._bridge_state["consecutive_failures"] = 5
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "offline")

    def test_rate_limit_returns_429(self):
        old_limit = server.RATE_LIMIT_MAX_REQUESTS
        server.RATE_LIMIT_MAX_REQUESTS = 1
        server._rate_buckets.clear()
        try:
            self.client.get("/api/status")
            r = self.client.get("/api/status")
            self.assertEqual(r.status_code, 429)
        finally:
            server.RATE_LIMIT_MAX_REQUESTS = old_limit
            server._rate_buckets.clear()


if __name__ == "__main__":
    unittest.main()
