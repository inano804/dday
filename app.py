#!/usr/bin/env python3
"""Local dashboard for market distribution days.

핵심 로직은 api/_market.py에 있고, Vercel 서버리스 함수(api/*.py)와 공유한다.
"""

from __future__ import annotations

import json
import mimetypes
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent / "api"))
import _market

HOST = "127.0.0.1"
PORT = 8000
INDEX_HTML_PATH = Path(__file__).resolve().parent / "public" / "index.html"
PUBLIC_DIR = INDEX_HTML_PATH.parent
SECURITY_HEADERS = json.loads(
    (PUBLIC_DIR.parent / "vercel.json").read_text(encoding="utf-8")
)["routes"][0]["headers"]


class DashboardServer(ThreadingHTTPServer):
    request_queue_size = 32


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "DistributionDayDashboard/1.0"

    def end_headers(self) -> None:
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        super().end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self.send_html(INDEX_HTML_PATH.read_text(encoding="utf-8"))
            return

        if parsed.path.startswith("/vendor/") or parsed.path in ("/dashboard.js", "/dashboard.css"):
            path = (PUBLIC_DIR / unquote(parsed.path).lstrip("/")).resolve()
            if not path.is_relative_to(PUBLIC_DIR.resolve()) or not path.is_file():
                self.send_json({"error": "찾을 수 없습니다."}, status=404)
                return
            encoded = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return

        if parsed.path == "/api/indexes":
            self.send_json({"indexes": _market.index_payloads()})
            return

        if parsed.path == "/api/data":
            try:
                index_id, force_refresh = _market.parse_data_query(parsed.query)
            except ValueError:
                self.send_json({"error": "잘못된 데이터 요청입니다."}, status=400)
                return
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
    server = DashboardServer((HOST, PORT), DashboardHandler)
    print(f"분산일 대시보드 실행 중: http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
