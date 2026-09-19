"""Vercel 서버리스 함수: GET /api/data?index=kospi[&refresh=1]"""

import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _market


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        force_refresh = False
        try:
            index_id, force_refresh = _market.parse_data_query(urlparse(self.path).query)
        except ValueError:
            status, payload = 400, {"error": "잘못된 데이터 요청입니다."}
        else:
            status, payload = _market.data_response(index_id, force_refresh)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        for name, value in _market.data_cache_headers(status, payload, force_refresh).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
