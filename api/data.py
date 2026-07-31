"""Vercel 서버리스 함수: GET /api/data?index=kospi[&refresh=1]"""

import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _market


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        index_id = (query.get("index") or ["kospi"])[0]
        refresh_value = (query.get("refresh") or ["0"])[0].lower()
        force_refresh = refresh_value in ("1", "true", "yes", "y")

        status, payload = _market.data_response(index_id, force_refresh)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
