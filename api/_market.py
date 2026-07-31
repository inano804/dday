"""분산일 대시보드 공용 로직 (로컬 서버와 Vercel 서버리스 함수가 함께 사용)."""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.9+ on the target machine has this.
    ZoneInfo = None  # type: ignore[assignment]


CACHE_TTL_SECONDS = 600
YAHOO_CHART_URLS = (
    "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
)
USER_AGENT = "Mozilla/5.0"
DISTRIBUTION_DROP_THRESHOLD_PCT = -0.2

BASE_DIR = Path(__file__).resolve().parent.parent
IS_SERVERLESS = bool(os.environ.get("VERCEL"))


def load_env_file(path: Path) -> None:
    """`.env`의 KEY=VALUE를 os.environ에 채운다. 이미 설정된 환경변수가 우선한다."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(BASE_DIR / ".env")

KRX_KOSPI_DAILY_URL = "https://data-dbg.krx.co.kr/svc/apis/idx/kospi_dd_trd"
KRX_INDEX_NAME = "코스피"
KRX_SOURCE_NAME = "KRX 정보데이터시스템 오픈API (KOSPI 일별시세)"
KRX_SNAPSHOT_PATH = Path(__file__).resolve().parent / "_krx_snapshot.json"
KRX_RUNTIME_CACHE_PATH = (
    Path("/tmp/krx_cache.json") if IS_SERVERLESS else BASE_DIR / "krx_cache.json"
)
KRX_HISTORY_DAYS = 396
# 빈 응답(휴장 또는 미공표)은 기준일로부터 이 일수가 지난 뒤 확인됐을 때만 휴장으로 확정한다.
KRX_EMPTY_RECHECK_DAYS = 5
# KRX는 짧은 간격의 연속 호출을 403으로 차단한다(실측: 1초 간격은 허용, 차단은 약 1분 뒤 해제).
KRX_REQUEST_INTERVAL_SECONDS = 1.0
KRX_RETRY_ATTEMPTS = 3
KRX_BLOCK_BACKOFF_SECONDS = 65
# 서버리스는 함수 실행 시간 한도가 있어 미수집 일수가 많으면 KRX 대신 Yahoo로 응답한다.
KRX_MAX_PENDING: Optional[int] = 30 if IS_SERVERLESS else None

INDEXES: Dict[str, Dict[str, str]] = {
    "kospi": {
        "name": "KOSPI",
        "symbol": "^KS11",
        "currency": "KRW",
        "timezone": "Asia/Seoul",
    },
    "kosdaq": {
        "name": "KOSDAQ",
        "symbol": "^KQ11",
        "currency": "KRW",
        "timezone": "Asia/Seoul",
    },
    "nasdaq": {
        "name": "NASDAQ",
        "symbol": "^IXIC",
        "currency": "USD",
        "timezone": "America/New_York",
    },
    "sp500": {
        "name": "S&P 500",
        "symbol": "^GSPC",
        "currency": "USD",
        "timezone": "America/New_York",
    },
    "dow": {
        "name": "Dow Jones",
        "symbol": "^DJI",
        "currency": "USD",
        "timezone": "America/New_York",
    },
}

_CACHE: Dict[str, Dict[str, Any]] = {}
_KRX_CACHE_LOCK = threading.Lock()
_KRX_BUILD_LOCK = threading.Lock()
_KRX_DISK_CACHE: Optional[Dict[str, Dict[str, Any]]] = None


def index_payloads() -> List[Dict[str, str]]:
    return [
        {
            "id": key,
            "name": value["name"],
            "symbol": value["symbol"],
            "currency": value["currency"],
            "timezone": value["timezone"],
        }
        for key, value in INDEXES.items()
    ]


def local_date_from_timestamp(timestamp: int, timezone_name: str) -> str:
    tz = ZoneInfo(timezone_name) if ZoneInfo else timezone.utc
    return datetime.fromtimestamp(timestamp, tz=tz).date().isoformat()


def request_yahoo_chart(symbol: str) -> Dict[str, Any]:
    encoded_symbol = quote(symbol, safe="")
    last_error = "알 수 없는 오류"

    for base_url in YAHOO_CHART_URLS:
        url = (
            base_url.format(symbol=encoded_symbol)
            + "?range=13mo&interval=1d&events=history&includePrePost=false"
        )
        try:
            return fetch_json_url(url)
        except RuntimeError as exc:
            last_error = str(exc)
            if "429" in last_error:
                time.sleep(0.8)
            continue

    raise RuntimeError(f"Yahoo Finance 응답 실패: {last_error}")


def fetch_json_url(url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    curl_path = shutil.which("curl")
    if curl_path:
        return fetch_json_with_curl(curl_path, url, headers)
    return fetch_json_with_urllib(url, headers)


def fetch_json_with_curl(
    curl_path: str, url: str, headers: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    command = [
        curl_path,
        "-fsSL",
        "--compressed",
        "--max-time",
        "25",
        "-A",
        USER_AGENT,
        "-H",
        "Accept: application/json",
    ]
    for name, value in (headers or {}).items():
        command += ["-H", f"{name}: {value}"]
    command.append(url)

    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or f"curl 종료 코드 {result.returncode}"
        raise RuntimeError(message)

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("JSON 해석 실패") from exc


def fetch_json_with_urllib(url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Connection": "close",
            "User-Agent": USER_AGENT,
            **(headers or {}),
        },
    )

    try:
        with urlopen(request, timeout=25) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"연결 오류: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("JSON 해석 실패") from exc


def krx_auth_key() -> str:
    return os.environ.get("KRX_AUTH_KEY", "").strip()


def parse_krx_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if text in ("", "-"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def request_krx_daily(bas_dd: str) -> Optional[Dict[str, Any]]:
    """기준일 하루치를 조회해 코스피 지수 행을 돌려준다. 휴장/미공표면 None."""
    url = f"{KRX_KOSPI_DAILY_URL}?basDd={bas_dd}"
    last_error = "알 수 없는 오류"

    for attempt in range(KRX_RETRY_ATTEMPTS):
        if attempt:
            blocked = "403" in last_error
            time.sleep(KRX_BLOCK_BACKOFF_SECONDS if blocked else 2.0)
        try:
            raw = fetch_json_url(url, headers={"AUTH_KEY": krx_auth_key()})
        except RuntimeError as exc:
            last_error = str(exc)
            continue
        if "OutBlock_1" not in raw:
            snippet = json.dumps(raw, ensure_ascii=False)[:200]
            last_error = f"KRX 응답 형식 오류: {snippet}"
            continue

        for item in raw["OutBlock_1"]:
            if str(item.get("IDX_NM", "")).strip() != KRX_INDEX_NAME:
                continue
            close = parse_krx_number(item.get("CLSPRC_IDX"))
            volume = parse_krx_number(item.get("ACC_TRDVOL"))
            if close is None or volume is None:
                return None
            return {
                "date": f"{bas_dd[:4]}-{bas_dd[4:6]}-{bas_dd[6:]}",
                "open": parse_krx_number(item.get("OPNPRC_IDX")),
                "high": parse_krx_number(item.get("HGPRC_IDX")),
                "low": parse_krx_number(item.get("LWPRC_IDX")),
                "close": close,
                "volume": volume,
            }
        return None

    raise RuntimeError(last_error)


def read_krx_cache_file(path: Path) -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        data = {}
    return {
        "rows": dict(data.get("rows") or {}),
        "empty": dict(data.get("empty") or {}),
    }


def load_krx_cache() -> Dict[str, Dict[str, Any]]:
    """배포에 번들된 스냅샷 위에 런타임 캐시를 덮어서 합친다(런타임이 더 최신)."""
    global _KRX_DISK_CACHE
    if _KRX_DISK_CACHE is None:
        snapshot = read_krx_cache_file(KRX_SNAPSHOT_PATH)
        runtime = read_krx_cache_file(KRX_RUNTIME_CACHE_PATH)
        _KRX_DISK_CACHE = {
            "rows": {**snapshot["rows"], **runtime["rows"]},
            "empty": {**snapshot["empty"], **runtime["empty"]},
        }
    return _KRX_DISK_CACHE


def save_krx_cache(cache: Dict[str, Dict[str, Any]]) -> None:
    try:
        tmp_path = KRX_RUNTIME_CACHE_PATH.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(KRX_RUNTIME_CACHE_PATH)
    except OSError as exc:
        sys.stderr.write(f"KRX 캐시 저장 실패(무시하고 진행): {exc}\n")


def seoul_today() -> date:
    if ZoneInfo:
        return datetime.now(ZoneInfo("Asia/Seoul")).date()
    return datetime.now().date()


def build_krx_kospi_rows() -> List[Dict[str, Any]]:
    with _KRX_BUILD_LOCK:
        today = seoul_today()
        start = today - timedelta(days=KRX_HISTORY_DAYS)

        with _KRX_CACHE_LOCK:
            cache = load_krx_cache()
            pending: List[str] = []
            current = start
            while current <= today:
                if current.weekday() < 5:
                    bas_dd = current.strftime("%Y%m%d")
                    if bas_dd not in cache["rows"]:
                        checked_on = cache["empty"].get(bas_dd)
                        settled = (
                            checked_on is not None
                            and (date.fromisoformat(checked_on) - current).days
                            >= KRX_EMPTY_RECHECK_DAYS
                        )
                        if not settled:
                            pending.append(bas_dd)
                current += timedelta(days=1)

        if KRX_MAX_PENDING is not None and len(pending) > KRX_MAX_PENDING:
            raise RuntimeError(
                f"KRX 미수집 {len(pending)}건 — 실행 시간 한도를 넘어 이번 요청은 다른 소스를 사용"
            )

        if len(pending) > 5:
            sys.stderr.write(
                f"KRX 일별시세 {len(pending)}건 수집 시작(1초 간격, 약 {len(pending)}초 예상)\n"
            )

        fetched: Dict[str, Optional[Dict[str, Any]]] = {}
        error: Optional[str] = None
        for position, bas_dd in enumerate(pending):
            if position:
                time.sleep(KRX_REQUEST_INTERVAL_SECONDS)
            try:
                fetched[bas_dd] = request_krx_daily(bas_dd)
            except RuntimeError as exc:
                error = f"{bas_dd}: {exc}"
                break
            if position and position % 50 == 0:
                sys.stderr.write(f"KRX 수집 진행 {position}/{len(pending)}\n")

        with _KRX_CACHE_LOCK:
            cache = load_krx_cache()
            today_iso = today.isoformat()
            for bas_dd, row in fetched.items():
                if row is None:
                    cache["empty"][bas_dd] = today_iso
                else:
                    cache["rows"][bas_dd] = row
                    cache["empty"].pop(bas_dd, None)
            if fetched:
                save_krx_cache(cache)
            if error:
                raise RuntimeError(
                    f"KRX 일별시세 조회 실패({error}), 이후 날짜는 다음 새로고침에 이어서 수집"
                )
            start_iso = start.isoformat()
            rows = [
                row
                for row in cache["rows"].values()
                if start_iso <= row["date"] <= today_iso
            ]

    rows.sort(key=lambda row: row["date"])
    return rows


def parse_chart_rows(index_config: Dict[str, str], raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    chart = raw.get("chart", {})
    if chart.get("error"):
        description = chart["error"].get("description", "알 수 없는 오류")
        raise RuntimeError(f"Yahoo Finance 오류: {description}")

    results = chart.get("result") or []
    if not results:
        raise RuntimeError("Yahoo Finance 응답에 차트 데이터가 없습니다.")

    result = results[0]
    timestamps = result.get("timestamp") or []
    quote_data = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote_data.get("close") or []
    volumes = quote_data.get("volume") or []
    opens = quote_data.get("open") or []
    highs = quote_data.get("high") or []
    lows = quote_data.get("low") or []
    timezone_name = result.get("meta", {}).get("exchangeTimezoneName") or index_config["timezone"]

    rows: List[Dict[str, Any]] = []
    for position, timestamp in enumerate(timestamps):
        close = value_at(closes, position)
        volume = value_at(volumes, position)
        if close is None or volume is None:
            continue

        rows.append(
            {
                "date": local_date_from_timestamp(timestamp, timezone_name),
                "open": value_at(opens, position),
                "high": value_at(highs, position),
                "low": value_at(lows, position),
                "close": close,
                "volume": volume,
            }
        )

    rows.sort(key=lambda row: row["date"])
    return rows


def value_at(values: List[Any], position: int) -> Optional[float]:
    if position >= len(values):
        return None
    value = values[position]
    if value is None:
        return None
    return float(value)


def enrich_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    enriched: List[Dict[str, Any]] = []
    previous: Optional[Dict[str, Any]] = None

    for row in rows:
        close_change = None
        close_change_pct = None
        volume_change = None
        volume_change_pct = None
        distribution = False

        if previous:
            close_change = row["close"] - previous["close"]
            close_change_pct = safe_pct(close_change, previous["close"])
            volume_change = row["volume"] - previous["volume"]
            volume_change_pct = safe_pct(volume_change, previous["volume"])
            distribution = (
                close_change_pct is not None
                and close_change_pct <= DISTRIBUTION_DROP_THRESHOLD_PCT
                and volume_change is not None
                and volume_change > 0
            )

        enriched.append(
            {
                **row,
                "closeChange": close_change,
                "closeChangePct": close_change_pct,
                "volumeChange": volume_change,
                "volumeChangePct": volume_change_pct,
                "distribution": distribution,
            }
        )
        previous = row

    return enriched


def safe_pct(change: Optional[float], base: Optional[float]) -> Optional[float]:
    if change is None or base in (None, 0):
        return None
    return (change / base) * 100


def market_payload(index_id: str, force_refresh: bool = False) -> Dict[str, Any]:
    cached = _CACHE.get(index_id)
    now = time.time()
    if not force_refresh and cached and now - cached["created_at"] < CACHE_TTL_SECONDS:
        return cached["payload"]

    index_config = INDEXES[index_id]
    rows: Optional[List[Dict[str, Any]]] = None
    source = "Yahoo Finance chart API"

    if index_id == "kospi" and krx_auth_key():
        try:
            krx_rows = build_krx_kospi_rows()
            if len(krx_rows) < 2:
                raise RuntimeError("KRX 데이터가 충분하지 않습니다.")
            rows = enrich_rows(krx_rows)
            source = KRX_SOURCE_NAME
        except RuntimeError as exc:
            sys.stderr.write(f"KRX 수집 실패, Yahoo Finance로 대체합니다: {exc}\n")
            source = "Yahoo Finance chart API (KRX 오류로 대체)"

    if rows is None:
        raw = request_yahoo_chart(index_config["symbol"])
        rows = enrich_rows(parse_chart_rows(index_config, raw))
    if len(rows) < 2:
        raise RuntimeError("분산일을 계산할 만큼 데이터가 충분하지 않습니다.")

    latest_date = datetime.fromisoformat(rows[-1]["date"]).date()
    cutoff = latest_date - timedelta(days=365)
    one_year_rows = [
        row for row in rows if datetime.fromisoformat(row["date"]).date() >= cutoff
    ]
    if not one_year_rows:
        raise RuntimeError("최근 1년 데이터가 없습니다.")

    recent25_rows = one_year_rows[-25:]
    recent25_distribution = [row for row in recent25_rows if row["distribution"]]
    one_year_distribution = [row for row in one_year_rows if row["distribution"]]
    latest = one_year_rows[-1]

    payload = {
        "index": {
            "id": index_id,
            "name": index_config["name"],
            "symbol": index_config["symbol"],
            "currency": index_config["currency"],
            "timezone": index_config["timezone"],
        },
        "source": source,
        "asOf": latest["date"],
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "recent25TradingDays": len(recent25_rows),
            "recent25DistributionCount": len(recent25_distribution),
            "oneYearTradingDays": len(one_year_rows),
            "oneYearDistributionCount": len(one_year_distribution),
        },
        "latest": latest,
        "series": one_year_rows,
        "recent25DistributionDays": recent25_distribution,
        "oneYearDistributionDays": one_year_distribution,
    }
    _CACHE[index_id] = {"created_at": now, "payload": payload}
    return payload


def data_response(index_id: str, force_refresh: bool) -> Tuple[int, Dict[str, Any]]:
    """(HTTP 상태코드, JSON 본문) 튜플을 돌려준다."""
    if index_id not in INDEXES:
        return 404, {"error": "지원하지 않는 지수입니다."}
    try:
        return 200, market_payload(index_id, force_refresh=force_refresh)
    except RuntimeError as exc:
        return 502, {"error": str(exc)}
