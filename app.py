#!/usr/bin/env python3
"""Local dashboard for market distribution days.

핵심 로직은 api/_market.py에 있고, Vercel 서버리스 함수(api/*.py)와 공유한다.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent / "api"))
import _market

HOST = "127.0.0.1"
PORT = 8000
INDEX_HTML_PATH = Path(__file__).resolve().parent / "public" / "index.html"


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "DistributionDayDashboard/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self.send_html(INDEX_HTML_PATH.read_text(encoding="utf-8"))
            return

        if parsed.path == "/api/indexes":
            self.send_json({"indexes": _market.index_payloads()})
            return

        if parsed.path == "/api/data":
            query = parse_qs(parsed.query)
            index_id = (query.get("index") or ["kospi"])[0]
            refresh_value = (query.get("refresh") or ["0"])[0].lower()
            force_refresh = refresh_value in ("1", "true", "yes", "y")
            status, payload = _market.data_response(index_id, force_refresh)
            self.send_json(payload, status=status)
            return

        if parsed.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        self.send_json({"error": "찾을 수 없습니다."}, status=404)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), fmt % args))

    def send_html(self, html: str) -> None:
        encoded = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)
    print(f"분산일 대시보드 실행 중: http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
