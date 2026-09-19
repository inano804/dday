import copy
import concurrent.futures
import subprocess
import sys
import threading
import unittest
from contextlib import ExitStack
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))
import _market as market


def sample_rows(last_day="2026-09-18"):
    return [
        {"date": "2026-09-17", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000},
        {"date": last_day, "open": 101, "high": 102, "low": 98, "close": 100, "volume": 1200},
    ]


class LoadingTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(market, "_CACHE", {}))
        stack.enter_context(patch.object(market, "_KRX_DISK_CACHE", {}))
        stack.enter_context(patch.object(market, "_BREADTH_DISK_CACHE", {}))
        stack.enter_context(patch.object(market, "seoul_today", return_value=date(2026, 9, 19)))
        self.snapshot = stack.enter_context(patch.object(
            market, "load_krx_cache", return_value={"rows": {}, "empty": {}}
        ))
        self.breadth = stack.enter_context(patch.object(
            market, "load_breadth_cache", return_value={"rows": {}, "empty": {}}
        ))
        self.yahoo = stack.enter_context(patch.object(market, "request_yahoo_chart", return_value={}))
        self.parse = stack.enter_context(patch.object(market, "parse_chart_rows", side_effect=lambda *_: sample_rows()))
        for name in ("build_krx_rows", "build_breadth_summaries", "request_krx_daily", "request_krx_breadth"):
            stack.enter_context(patch.object(market, name, side_effect=AssertionError("KRX on request path")))
        stack.enter_context(patch.object(market.time, "sleep", side_effect=AssertionError("Request must not sleep")))

    def seed_snapshot(self, rows):
        self.snapshot.return_value = {
            "rows": {row["date"].replace("-", ""): row for row in rows}, "empty": {}
        }

    def old_snapshot(self):
        rows = sample_rows()
        rows[0]["date"], rows[1]["date"] = "2026-07-29", "2026-07-30"
        self.seed_snapshot(rows)

    def test_stale_snapshot_uses_bulk_yahoo_without_krx_backfill(self):
        self.old_snapshot()
        for index_id in ("kospi", "kosdaq"):
            status, payload = market.data_response(index_id, False)
            self.assertEqual(status, 200)
            self.assertEqual(payload["asOf"], "2026-09-18")
            self.assertFalse(payload["stale"])
            self.assertEqual(payload["source"], "Yahoo Finance chart API")

    def test_current_friday_snapshot_is_usable_on_weekend(self):
        self.seed_snapshot(sample_rows())
        payload = market.market_payload("kospi")
        self.yahoo.assert_not_called()
        self.assertIn("KRX", payload["source"])

    def test_refresh_bypasses_memory_and_current_snapshot(self):
        self.seed_snapshot(sample_rows())
        market.market_payload("kospi")
        market.market_payload("kospi", force_refresh=True)
        self.yahoo.assert_called_once_with("^KS11")
        self.assertIn("Yahoo", market._CACHE["kospi"]["payload"]["source"])

    def test_warm_request_reuses_successful_response(self):
        first = market.market_payload("nasdaq")
        self.assertIs(first, market.market_payload("nasdaq"))
        self.yahoo.assert_called_once()

    def test_upstream_failure_returns_dated_snapshot_without_caching_it(self):
        self.old_snapshot()
        self.yahoo.side_effect = RuntimeError("HTTP 429")
        status, payload = market.data_response("kospi", False)
        self.assertEqual(status, 200)
        self.assertTrue(payload["stale"])
        self.assertEqual(payload["asOf"], "2026-07-30")
        self.assertTrue(payload["warning"])
        self.assertNotIn("kospi", market._CACHE)

    def test_upstream_failure_keeps_newer_memory_payload(self):
        self.old_snapshot()
        first = copy.deepcopy(market.market_payload("kospi"))
        self.yahoo.side_effect = RuntimeError("timeout")
        result = market.market_payload("kospi", force_refresh=True)
        self.assertTrue(result["stale"])
        self.assertEqual(result["asOf"], first["asOf"])
        self.assertEqual(result["generatedAt"], first["generatedAt"])
        self.assertFalse(market._CACHE["kospi"]["payload"]["stale"])

    def test_failure_without_saved_data_returns_error(self):
        self.yahoo.side_effect = RuntimeError("timeout")
        status, payload = market.data_response("dow", False)
        self.assertEqual(status, 502)
        self.assertNotIn("timeout", payload["error"])

    def test_insufficient_yahoo_data_falls_back(self):
        self.old_snapshot()
        self.parse.side_effect = None
        self.parse.return_value = []
        self.assertTrue(market.market_payload("kospi")["stale"])

    def test_breadth_only_attaches_matching_cached_dates(self):
        self.breadth.return_value["rows"]["2026-09-18"] = {
            "advancers": 10, "decliners": 5, "unchanged": 3, "upVolume": 200, "downVolume": 100
        }
        payload = market.market_payload("kospi")
        self.assertEqual(payload["latest"]["breadth"]["adRatio"], 2)
        self.assertNotIn("breadth", payload["series"][0])

    def test_invalid_index_has_no_source_request(self):
        self.assertEqual(market.data_response("missing", False)[0], 404)
        self.yahoo.assert_not_called()

    def test_only_normal_success_is_cdn_cacheable(self):
        self.assertIn("s-maxage=300", market.data_cache_headers(200, {}, False)["Vercel-CDN-Cache-Control"])
        for status, payload, refresh in ((200, {}, True), (502, {}, False), (200, {"stale": True}, False)):
            headers = market.data_cache_headers(status, payload, refresh)
            self.assertEqual(headers["Vercel-CDN-Cache-Control"], "no-store")
            self.assertEqual(headers["Cache-Control"], "no-store")

    def test_simultaneous_requests_share_one_source_fetch(self):
        class ConcurrentLock:
            def __init__(self):
                self.barrier = threading.Barrier(5)
                self.lock = threading.Lock()

            def acquire(self, timeout):
                self.barrier.wait(timeout=3)
                return self.lock.acquire(timeout=timeout)

            def release(self):
                self.lock.release()

        for force_refresh in (False, True):
            with self.subTest(force_refresh=force_refresh):
                market._CACHE.clear()
                self.yahoo.reset_mock()
                with patch.dict(market._MARKET_LOCKS, {"kospi": ConcurrentLock()}):
                    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
                        futures = [pool.submit(market.market_payload, "kospi", force_refresh) for _ in range(5)]
                        results = [future.result(timeout=5) for future in futures]
                self.yahoo.assert_called_once()
                self.assertTrue(all(result is results[0] for result in results))

    def test_separate_refreshes_still_fetch_new_data(self):
        market.market_payload("kospi", force_refresh=True)
        market.market_payload("kospi", force_refresh=True)
        self.assertEqual(self.yahoo.call_count, 2)

    def test_source_failure_does_not_leak_details_or_hold_lock(self):
        self.yahoo.side_effect = RuntimeError("private/internal/file and upstream diagnostics")
        status, payload = market.data_response("dow", True)
        self.assertEqual(status, 502)
        self.assertNotIn("private", payload["error"])
        self.yahoo.side_effect = None
        self.assertEqual(market.data_response("dow", True)[0], 200)


class QueryTests(unittest.TestCase):
    def test_supported_queries_keep_refresh_semantics(self):
        self.assertEqual(market.parse_data_query(""), ("kospi", False))
        self.assertEqual(market.parse_data_query("index=kosdaq&refresh=1&_=1789816000000"), ("kosdaq", True))
        self.assertEqual(market.parse_data_query("index=nasdaq&refresh=TRUE"), ("nasdaq", True))

    def test_rejects_oversized_duplicate_and_unexpected_parameters(self):
        for query in ("index=" + "x" * 2050, "index=kospi&index=kosdaq", "url=https://example.com",
                      "refresh=maybe", "index=", "_=abc", "&".join("_=1" for _ in range(9))):
            with self.subTest(query=query[:50]), self.assertRaises(ValueError):
                market.parse_data_query(query)


class TransportTests(unittest.TestCase):
    def test_yahoo_retries_other_host_with_short_timeouts_and_no_sleep(self):
        with patch.object(market, "fetch_json_url", side_effect=[RuntimeError("429"), {"ok": True}]) as fetch:
            with patch.object(market.time, "sleep", side_effect=AssertionError("Unexpected sleep")):
                self.assertEqual(market.request_yahoo_chart("^KS11"), {"ok": True})
        self.assertEqual(fetch.call_count, 2)
        self.assertIn("query1", fetch.call_args.args[0])
        self.assertEqual(fetch.call_args.kwargs["timeout"], 4)

    def test_curl_timeout_is_a_handled_source_error(self):
        with patch.object(market.subprocess, "run", side_effect=subprocess.TimeoutExpired("curl", 5)):
            with self.assertRaises(RuntimeError):
                market.fetch_json_with_curl("curl", "https://example.com", timeout=4)

    def test_urllib_timeout_is_a_handled_source_error(self):
        with patch.object(market, "urlopen", side_effect=TimeoutError()):
            with self.assertRaises(RuntimeError):
                market.fetch_json_with_urllib("https://example.com", timeout=4)


if __name__ == "__main__":
    unittest.main()
