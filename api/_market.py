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
from urllib.parse import parse_qs, quote
from urllib.request import Request, urlopen

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.9+ on the target machine has this.
    ZoneInfo = None  # type: ignore[assignment]


CACHE_TTL_SECONDS = 600
YAHOO_TIMEOUT_SECONDS = 4
YAHOO_CHART_URLS = (
    "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
)
USER_AGENT = "Mozilla/5.0"
DISTRIBUTION_DROP_THRESHOLD_PCT = -0.2

# IBD 카운팅 규칙 (docs/memory.md 기준)
DISTRIBUTION_RECOVERY_PCT = 5.0   # 종가 대비 +5% 이상 상승 시 개별 분산일 소멸
DISTRIBUTION_WINDOW_DAYS = 25     # 롤링 25거래일 창(이 밖은 기간 경과로 소멸)
CLUSTER_WINDOW_DAYS = 15          # 단기 클러스터 판정 창(약 3주)
CLUSTER_THRESHOLD = 4             # 창 내 유효 분산일이 이 개수 이상이면 경고
PHASE_PRESSURE_MIN = 3            # 유효 압박일 3~4개: 상승추세 압박
PHASE_CORRECTION_MIN = 5         # 유효 압박일 5개 이상: 조정 국면 경고

# 정체일(Stalling Day): 거래량 증가 + 상승 마감이나 상승폭 미미 + 종가가 일중 저가권
STALLING_MAX_GAIN_PCT = 0.6       # 상승폭이 이 값 미만이어야 '막힌 상승'
STALLING_CLOSE_POS_MAX = 0.35     # (종가-저가)/(고가-저가)가 이 값 이하면 저가권

# 매집일(Accumulation Day): 거래량 증가 동반 상승일(분산일의 반대)
ACCUMULATION_RISE_THRESHOLD_PCT = 0.2

# Follow-Through Day: 저점 후 4거래일째 이상 + 거래량 증가 + 큰 폭 상승 = 재진입 신호
FTD_MIN_GAIN_PCT = 1.25
FTD_MIN_DAYS_AFTER_LOW = 3        # 저점(day1, 오프셋0) 기준 오프셋 3 = 4거래일째
FTD_RALLY_LOW_LOOKBACK = 13       # 최근 저점 탐색 창
FTD_CORRECTION_MIN_PCT = 3.0      # 저점이 직전 고점 대비 이 % 이상 하락해야 '조정 후'로 인정
FTD_CORRECTION_PEAK_LOOKBACK = 30

# 파생상품 만기일(위칭데이) — 거래량 왜곡일. 시장별 규칙.
#   한국: 매월 둘째 목요일(옵션 만기), 3·6·9·12월은 네 마녀의 날(선물+옵션)
#   미국: 3·6·9·12월 셋째 금요일(분기 위칭)
WITCHING_QUARTER_MONTHS = (3, 6, 9, 12)

# 시장 폭(breadth): 전종목 등락/거래량 집계. 최근 이 거래일수만 수집(비용 제한).
BREADTH_WINDOW_DAYS = 25

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

# KRX 오픈API 일별시세를 공식 소스로 쓰는 지수들(그 외는 Yahoo). 인증키 미구독 시 자동 폴백.
KRX_INDEX_CONFIG: Dict[str, Dict[str, str]] = {
    "kospi": {
        "url": "https://data-dbg.krx.co.kr/svc/apis/idx/kospi_dd_trd",
        "idxName": "코스피",
        "source": "KRX 정보데이터시스템 오픈API (KOSPI 일별시세)",
    },
    "kosdaq": {
        "url": "https://data-dbg.krx.co.kr/svc/apis/idx/kosdaq_dd_trd",
        "idxName": "코스닥",
        "source": "KRX 정보데이터시스템 오픈API (KOSDAQ 일별시세)",
    },
}
_API_DIR = Path(__file__).resolve().parent


def krx_snapshot_path(index_id: str) -> Path:
    return _API_DIR / f"_krx_snapshot_{index_id}.json"


def krx_runtime_cache_path(index_id: str) -> Path:
    name = f"krx_cache_{index_id}.json"
    return Path("/tmp") / name if IS_SERVERLESS else BASE_DIR / name


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
        "market": "kr",
    },
    "kosdaq": {
        "name": "KOSDAQ",
        "symbol": "^KQ11",
        "currency": "KRW",
        "timezone": "Asia/Seoul",
        "market": "kr",
    },
    "nasdaq": {
        "name": "NASDAQ",
        "symbol": "^IXIC",
        "currency": "USD",
        "timezone": "America/New_York",
        "market": "us",
    },
    "sp500": {
        "name": "S&P 500",
        "symbol": "^GSPC",
        "currency": "USD",
        "timezone": "America/New_York",
        "market": "us",
    },
    "dow": {
        "name": "Dow Jones",
        "symbol": "^DJI",
        "currency": "USD",
        "timezone": "America/New_York",
        "market": "us",
    },
}

# 시장별 특성(Tier 3-C 지수 파라미터 프로파일). 코스닥은 신호 품질 경고 대상.
MARKET_PROFILE: Dict[str, Dict[str, Any]] = {
    "kr": {"witching": "kr"},
    "us": {"witching": "us"},
}
# 개인 비중이 높아 거래량 변동성이 큰 시장 — 신호 신뢰도 경고를 표시한다(docs/memory.md).
LOW_QUALITY_INDEXES = {"kosdaq"}

# 시장 폭 전종목 엔드포인트(지수 → 주식 일별매매 API)
KRX_STOCK_ENDPOINT: Dict[str, str] = {
    "kospi": "https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd",
    "kosdaq": "https://data-dbg.krx.co.kr/svc/apis/sto/ksq_bydd_trd",
}

_CACHE: Dict[str, Dict[str, Any]] = {}
_MARKET_LOCKS = {index_id: threading.Lock() for index_id in INDEXES}
_KRX_CACHE_LOCK = threading.Lock()
_KRX_BUILD_LOCKS: Dict[str, threading.Lock] = {}
_KRX_DISK_CACHE: Dict[str, Dict[str, Dict[str, Any]]] = {}


def krx_build_lock(index_id: str) -> threading.Lock:
    """지수별 빌드 락(코스피 403 백오프가 코스닥 요청을 막지 않도록 분리)."""
    with _KRX_CACHE_LOCK:
        lock = _KRX_BUILD_LOCKS.get(index_id)
        if lock is None:
            lock = threading.Lock()
            _KRX_BUILD_LOCKS[index_id] = lock
        return lock


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
            return fetch_json_url(url, timeout=YAHOO_TIMEOUT_SECONDS)
        except RuntimeError as exc:
            last_error = str(exc)
            continue

    raise RuntimeError(f"Yahoo Finance 응답 실패: {last_error}")


def fetch_json_url(
    url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 25
) -> Dict[str, Any]:
    curl_path = shutil.which("curl")
    if curl_path:
        return fetch_json_with_curl(curl_path, url, headers, timeout)
    return fetch_json_with_urllib(url, headers, timeout)


def fetch_json_with_curl(
    curl_path: str, url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 25
) -> Dict[str, Any]:
    command = [
        curl_path,
        "-fsSL",
        "--compressed",
        "--max-time",
        str(timeout),
        "--connect-timeout",
        str(min(timeout, 3)),
        "-A",
        USER_AGENT,
        "-H",
        "Accept: application/json",
    ]
    for name, value in (headers or {}).items():
        command += ["-H", f"{name}: {value}"]
    command.append(url)

    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout + 1,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("데이터 조회 시간이 초과되었습니다.") from exc
    if result.returncode != 0:
        message = result.stderr.strip() or f"curl 종료 코드 {result.returncode}"
        raise RuntimeError(message)

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("JSON 해석 실패") from exc


def fetch_json_with_urllib(
    url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 25
) -> Dict[str, Any]:
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
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"연결 오류: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise RuntimeError("데이터 연결 실패 또는 시간 초과") from exc
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


def request_krx_daily(index_id: str, bas_dd: str) -> Optional[Dict[str, Any]]:
    """기준일 하루치를 조회해 해당 지수 행을 돌려준다. 휴장/미공표면 None."""
    config = KRX_INDEX_CONFIG[index_id]
    url = f"{config['url']}?basDd={bas_dd}"
    idx_name = config["idxName"]
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
            if str(item.get("IDX_NM", "")).strip() != idx_name:
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


def load_krx_cache(index_id: str) -> Dict[str, Dict[str, Any]]:
    """배포에 번들된 스냅샷 위에 런타임 캐시를 덮어서 합친다(런타임이 더 최신)."""
    cache = _KRX_DISK_CACHE.get(index_id)
    if cache is None:
        snapshot = read_krx_cache_file(krx_snapshot_path(index_id))
        runtime = read_krx_cache_file(krx_runtime_cache_path(index_id))
        cache = {
            "rows": {**snapshot["rows"], **runtime["rows"]},
            "empty": {**snapshot["empty"], **runtime["empty"]},
        }
        _KRX_DISK_CACHE[index_id] = cache
    return cache


def save_krx_cache(index_id: str, cache: Dict[str, Dict[str, Any]]) -> None:
    path = krx_runtime_cache_path(index_id)
    try:
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(path)
    except OSError as exc:
        sys.stderr.write(f"KRX 캐시 저장 실패(무시하고 진행): {exc}\n")


def seoul_today() -> date:
    if ZoneInfo:
        return datetime.now(ZoneInfo("Asia/Seoul")).date()
    return datetime.now().date()


def build_krx_rows(index_id: str) -> List[Dict[str, Any]]:
    label = KRX_INDEX_CONFIG[index_id]["idxName"]
    with krx_build_lock(index_id):
        today = seoul_today()
        start = today - timedelta(days=KRX_HISTORY_DAYS)

        with _KRX_CACHE_LOCK:
            cache = load_krx_cache(index_id)
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
                f"KRX {label} 일별시세 {len(pending)}건 수집 시작(1초 간격, 약 {len(pending)}초 예상)\n"
            )

        fetched: Dict[str, Optional[Dict[str, Any]]] = {}
        error: Optional[str] = None
        for position, bas_dd in enumerate(pending):
            if position:
                time.sleep(KRX_REQUEST_INTERVAL_SECONDS)
            try:
                fetched[bas_dd] = request_krx_daily(index_id, bas_dd)
            except RuntimeError as exc:
                error = f"{bas_dd}: {exc}"
                break
            if position and position % 50 == 0:
                sys.stderr.write(f"KRX {label} 수집 진행 {position}/{len(pending)}\n")

        with _KRX_CACHE_LOCK:
            cache = load_krx_cache(index_id)
            today_iso = today.isoformat()
            for bas_dd, row in fetched.items():
                if row is None:
                    cache["empty"][bas_dd] = today_iso
                else:
                    cache["rows"][bas_dd] = row
                    cache["empty"].pop(bas_dd, None)
            if fetched:
                save_krx_cache(index_id, cache)
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


# ---------------------------------------------------------------------------
# 시장 폭(Market Breadth): 전종목 등락/거래량 집계 (KOSPI·KOSDAQ 한정)
# ---------------------------------------------------------------------------
_BREADTH_DISK_CACHE: Dict[str, Dict[str, Dict[str, Any]]] = {}


def breadth_snapshot_path(index_id: str) -> Path:
    return _API_DIR / f"_krx_breadth_{index_id}.json"


def breadth_runtime_cache_path(index_id: str) -> Path:
    name = f"krx_breadth_{index_id}.json"
    return Path("/tmp") / name if IS_SERVERLESS else BASE_DIR / name


def load_breadth_cache(index_id: str) -> Dict[str, Dict[str, Any]]:
    cache = _BREADTH_DISK_CACHE.get(index_id)
    if cache is None:
        snapshot = read_krx_cache_file(breadth_snapshot_path(index_id))
        runtime = read_krx_cache_file(breadth_runtime_cache_path(index_id))
        cache = {
            "rows": {**snapshot["rows"], **runtime["rows"]},
            "empty": {**snapshot["empty"], **runtime["empty"]},
        }
        _BREADTH_DISK_CACHE[index_id] = cache
    return cache


def save_breadth_cache(index_id: str, cache: Dict[str, Dict[str, Any]]) -> None:
    path = breadth_runtime_cache_path(index_id)
    try:
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(path)
    except OSError as exc:
        sys.stderr.write(f"브레드스 캐시 저장 실패(무시): {exc}\n")


def request_krx_breadth(index_id: str, bas_dd: str) -> Optional[Dict[str, Any]]:
    """전종목 일별매매를 조회해 등락 종목수·상승/하락 거래량 요약을 만든다. 휴장이면 None."""
    endpoint = KRX_STOCK_ENDPOINT[index_id]
    url = f"{endpoint}?basDd={bas_dd}"
    last_error = "알 수 없는 오류"

    for attempt in range(KRX_RETRY_ATTEMPTS):
        if attempt:
            time.sleep(KRX_BLOCK_BACKOFF_SECONDS if "403" in last_error else 2.0)
        try:
            raw = fetch_json_url(url, headers={"AUTH_KEY": krx_auth_key()})
        except RuntimeError as exc:
            last_error = str(exc)
            continue
        if "OutBlock_1" not in raw:
            last_error = f"KRX 응답 형식 오류: {json.dumps(raw, ensure_ascii=False)[:160]}"
            continue

        items = raw["OutBlock_1"]
        if not items:
            return None  # 휴장/미공표

        adv = dec = unch = 0
        up_vol = down_vol = 0.0
        for item in items:
            rate = parse_krx_number(item.get("FLUC_RT"))
            volume = parse_krx_number(item.get("ACC_TRDVOL")) or 0.0
            if rate is None:
                continue
            if rate > 0:
                adv += 1
                up_vol += volume
            elif rate < 0:
                dec += 1
                down_vol += volume
            else:
                unch += 1
        return {
            "date": f"{bas_dd[:4]}-{bas_dd[4:6]}-{bas_dd[6:]}",
            "advancers": adv,
            "decliners": dec,
            "unchanged": unch,
            "upVolume": up_vol,
            "downVolume": down_vol,
        }

    raise RuntimeError(last_error)


def build_breadth_summaries(index_id: str, iso_dates: List[str]) -> Dict[str, Dict[str, Any]]:
    """주어진 거래일들의 시장 폭 요약을 돌려준다(캐시에 없는 것만 수집)."""
    if index_id not in KRX_STOCK_ENDPOINT or not krx_auth_key():
        return {}

    with krx_build_lock(f"breadth:{index_id}"):
        today_iso = seoul_today().isoformat()
        with _KRX_CACHE_LOCK:
            cache = load_breadth_cache(index_id)
            pending = []
            for iso in iso_dates:
                bas_dd = iso.replace("-", "")
                if iso in cache["rows"]:
                    continue
                checked_on = cache["empty"].get(bas_dd)
                settled = (
                    checked_on is not None
                    and (date.fromisoformat(checked_on) - date.fromisoformat(iso)).days
                    >= KRX_EMPTY_RECHECK_DAYS
                )
                if not settled:
                    pending.append((iso, bas_dd))

        if KRX_MAX_PENDING is not None and len(pending) > KRX_MAX_PENDING:
            # 서버리스에서 미수집이 많으면 시장 폭은 이번 요청에서 생략(가진 것만 반환).
            sys.stderr.write(f"브레드스 미수집 {len(pending)}건 — 이번 요청은 수집 생략\n")
            pending = []

        fetched: Dict[str, Optional[Dict[str, Any]]] = {}
        for position, (iso, bas_dd) in enumerate(pending):
            if position:
                time.sleep(KRX_REQUEST_INTERVAL_SECONDS)
            try:
                fetched[bas_dd] = request_krx_breadth(index_id, bas_dd)
            except RuntimeError as exc:
                sys.stderr.write(f"브레드스 조회 실패 {bas_dd}: {exc}\n")
                break

        with _KRX_CACHE_LOCK:
            cache = load_breadth_cache(index_id)
            for bas_dd, summary in fetched.items():
                iso = f"{bas_dd[:4]}-{bas_dd[4:6]}-{bas_dd[6:]}"
                if summary is None:
                    cache["empty"][bas_dd] = today_iso
                else:
                    cache["rows"][iso] = summary
                    cache["empty"].pop(bas_dd, None)
            if fetched:
                save_breadth_cache(index_id, cache)
            return {iso: cache["rows"][iso] for iso in iso_dates if iso in cache["rows"]}


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
        stalling = False
        accumulation = False

        if previous:
            close_change = row["close"] - previous["close"]
            close_change_pct = safe_pct(close_change, previous["close"])
            volume_change = row["volume"] - previous["volume"]
            volume_change_pct = safe_pct(volume_change, previous["volume"])
            volume_up = volume_change is not None and volume_change > 0

            if close_change_pct is not None and volume_up:
                distribution = close_change_pct <= DISTRIBUTION_DROP_THRESHOLD_PCT
                # 정체일: 거래량 증가 + 상승/보합이나 상승폭 미미 + 종가가 일중 저가권
                stalling = (
                    not distribution
                    and 0 <= close_change_pct < STALLING_MAX_GAIN_PCT
                    and close_in_lower_range(row, STALLING_CLOSE_POS_MAX)
                )
                # 매집일: 거래량 증가 동반 상승(정체일 제외)
                accumulation = (
                    not stalling
                    and close_change_pct >= ACCUMULATION_RISE_THRESHOLD_PCT
                )

        enriched.append(
            {
                **row,
                "closeChange": close_change,
                "closeChangePct": close_change_pct,
                "volumeChange": volume_change,
                "volumeChangePct": volume_change_pct,
                "distribution": distribution,
                "stalling": stalling,
                "accumulation": accumulation,
            }
        )
        previous = row

    return enriched


def close_in_lower_range(row: Dict[str, Any], max_pos: float) -> bool:
    """종가가 일중 범위의 하위 max_pos 이내(저가권)이면 True. OHLC 없으면 False."""
    high = row.get("high")
    low = row.get("low")
    close = row.get("close")
    if high is None or low is None or close is None or high <= low:
        return False
    position = (close - low) / (high - low)
    return position <= max_pos


def safe_pct(change: Optional[float], base: Optional[float]) -> Optional[float]:
    if change is None or base in (None, 0):
        return None
    return (change / base) * 100


def annotate_distribution_lifecycle(one_year_rows: List[Dict[str, Any]]) -> None:
    """압박일(분산일+정체일)에 소멸 여부(expired)와 유효 여부(active)를 표시한다(행 직접 수정).

    IBD 규칙: 압박일은 롤링 25거래일 창 안에서만 세고, 그 창 안이라도 지수가
    해당일 종가 대비 +5% 이상 상승하면 강세가 매도물량을 흡수했다고 보아 소멸시킨다.
    정체일(Stalling Day)도 분산일과 동일하게 카운트한다.
    """
    n = len(one_year_rows)
    window_start = n - DISTRIBUTION_WINDOW_DAYS  # 이 인덱스 미만은 기간 경과로 소멸

    for i, row in enumerate(one_year_rows):
        expired = False
        reason: Optional[str] = None
        active = False
        is_pressure = bool(row.get("distribution") or row.get("stalling"))

        if is_pressure:
            if i < window_start:
                expired = True
                reason = "25거래일 경과"
            else:
                threshold = row["close"] * (1 + DISTRIBUTION_RECOVERY_PCT / 100)
                recovered = any(
                    one_year_rows[j]["close"] >= threshold for j in range(i + 1, n)
                )
                if recovered:
                    expired = True
                    reason = "5% 회복"
            active = not expired

        row["pressure"] = is_pressure
        row["pressureType"] = (
            "distribution" if row.get("distribution")
            else "stalling" if row.get("stalling")
            else None
        )
        row["expired"] = expired
        row["expiredReason"] = reason
        row["active"] = active


def detect_follow_through_days(one_year_rows: List[Dict[str, Any]]) -> None:
    """조정 후 재진입 신호(Follow-Through Day)를 표시한다(행 직접 수정).

    랠리 시도 저점(1일차) 이후 4거래일째 이상에서 거래량 증가 + 1.25% 이상 상승 시 1회 발화.
    그 저점이 직전 고점 대비 3% 이상 하락한 '조정 후'일 때만 인정하고, 새 저점이
    직전 저점을 깨야 다음 랠리 시도로 보아 다시 발화한다(랠리당 최초 1일만 표시).
    """
    for row in one_year_rows:
        row["followThrough"] = False

    n = len(one_year_rows)
    for i in range(n):
        row = one_year_rows[i]
        gain = row.get("closeChangePct")
        vol_change = row.get("volumeChange")
        if gain is None or gain < FTD_MIN_GAIN_PCT:
            continue
        if vol_change is None or vol_change <= 0:
            continue

        low_from = max(0, i - FTD_RALLY_LOW_LOOKBACK)
        if low_from >= i:
            continue
        rally_low = min(range(low_from, i), key=lambda j: one_year_rows[j]["close"])
        if (i - rally_low) < FTD_MIN_DAYS_AFTER_LOW:
            continue

        peak_from = max(0, rally_low - FTD_CORRECTION_PEAK_LOOKBACK)
        peak = max(one_year_rows[j]["close"] for j in range(peak_from, rally_low + 1))
        drawdown = safe_pct(one_year_rows[rally_low]["close"] - peak, peak)
        if drawdown is None or drawdown > -FTD_CORRECTION_MIN_PCT:
            continue  # 저점이 직전 고점 대비 3% 이상 하락한 '조정 후'가 아님

        # 같은 랠리에서 중복 발화 방지: 최근 lookback 내 이미 FTD가 있으면 건너뜀
        if any(one_year_rows[j]["followThrough"] for j in range(low_from, i)):
            continue

        row["followThrough"] = True


def nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    """해당 월의 n번째 특정 요일 날짜(weekday: 월=0 … 일=6)."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + (n - 1) * 7)


def witching_info(d: date, market: str) -> Optional[str]:
    """만기일이면 유형 문자열, 아니면 None.

    한국: 매월 둘째 목요일(월물 옵션), 3·6·9·12월은 네 마녀의 날(선물+옵션).
    미국: 3·6·9·12월 셋째 금요일(분기 위칭).
    """
    is_quarter = d.month in WITCHING_QUARTER_MONTHS
    if market == "kr":
        if d == nth_weekday_of_month(d.year, d.month, 3, 2):  # 둘째 목요일
            return "네 마녀의 날" if is_quarter else "옵션 만기"
    elif market == "us":
        if is_quarter and d == nth_weekday_of_month(d.year, d.month, 4, 3):  # 셋째 금요일
            return "분기 위칭"
    return None


def annotate_witching(rows: List[Dict[str, Any]], market: str) -> None:
    for row in rows:
        info = witching_info(date.fromisoformat(row["date"]), market)
        row["witching"] = info is not None
        row["witchingType"] = info


def market_phase(active_count: int) -> Dict[str, Any]:
    """유효 압박일 수로 IBD Market Pulse 3단계 국면을 판정한다."""
    if active_count >= PHASE_CORRECTION_MIN:
        return {
            "level": "correction",
            "label": "조정 국면 진입 경고",
            "action": "현금 비중 확대, 신규 진입 중단",
        }
    if active_count >= PHASE_PRESSURE_MIN:
        return {
            "level": "pressure",
            "label": "상승추세 압박",
            "action": "신규 매수 축소, 손절 엄격 적용",
        }
    return {
        "level": "confirmed",
        "label": "확인된 상승추세",
        "action": "정상 비중 운용",
    }


def market_payload(index_id: str, force_refresh: bool = False) -> Dict[str, Any]:
    started = time.monotonic()
    cached = _CACHE.get(index_id)
    if not force_refresh and cached and time.time() - cached["created_at"] < CACHE_TTL_SECONDS:
        return cached["payload"]

    lock = _MARKET_LOCKS[index_id]
    if not lock.acquire(timeout=YAHOO_TIMEOUT_SECONDS * len(YAHOO_CHART_URLS) + 3):
        raise RuntimeError("다른 데이터 조회가 진행 중입니다.")
    try:
        cached = _CACHE.get(index_id)
        if cached:
            fresh = time.time() - cached["created_at"] < CACHE_TTL_SECONDS
            fetched_during_request = (
                cached.get("source_refetched", False)
                and cached.get("completed_at", 0) >= started
            )
            if (not force_refresh and fresh) or fetched_during_request:
                return cached["payload"]
        return build_market_payload(index_id, force_refresh)
    finally:
        lock.release()


def build_market_payload(index_id: str, force_refresh: bool = False) -> Dict[str, Any]:
    cached = _CACHE.get(index_id)
    index_config = INDEXES[index_id]
    rows: Optional[List[Dict[str, Any]]] = None
    source = "Yahoo Finance chart API"
    stale = False
    source_refetched = False
    warning = None
    snapshot_rows: List[Dict[str, Any]] = []

    # KRX backfill and retry sleeps belong to the offline snapshot updater.
    if index_id in KRX_INDEX_CONFIG:
        with _KRX_CACHE_LOCK:
            snapshot_rows = sorted(
                load_krx_cache(index_id)["rows"].values(), key=lambda row: row["date"]
            )
        latest_weekday = seoul_today()
        while latest_weekday.weekday() >= 5:
            latest_weekday -= timedelta(days=1)
        if (not force_refresh and len(snapshot_rows) >= 2
                and snapshot_rows[-1]["date"] >= latest_weekday.isoformat()):
            rows = enrich_rows(snapshot_rows)
            source = KRX_INDEX_CONFIG[index_id]["source"]

    if rows is None:
        try:
            raw = request_yahoo_chart(index_config["symbol"])
            rows = enrich_rows(parse_chart_rows(index_config, raw))
            if len(rows) < 2:
                raise RuntimeError("조회한 데이터가 충분하지 않습니다.")
            source_refetched = True
        except RuntimeError as exc:
            sys.stderr.write(f"최신 데이터 조회 실패({index_id}): {exc}\n")
            warning = "최신 데이터를 가져오지 못해 저장된 데이터를 표시합니다. 기준일을 확인해 주세요."
            if cached and (len(snapshot_rows) < 2 or
                           cached["payload"]["asOf"] >= snapshot_rows[-1]["date"]):
                return {**cached["payload"], "stale": True, "warning": warning}
            if len(snapshot_rows) < 2:
                raise
            rows = enrich_rows(snapshot_rows)
            source = KRX_INDEX_CONFIG[index_id]["source"] + " (저장본)"
            stale = True
    if len(rows) < 2:
        raise RuntimeError("분산일을 계산할 만큼 데이터가 충분하지 않습니다.")

    latest_date = datetime.fromisoformat(rows[-1]["date"]).date()
    cutoff = latest_date - timedelta(days=365)
    one_year_rows = [
        row for row in rows if datetime.fromisoformat(row["date"]).date() >= cutoff
    ]
    if not one_year_rows:
        raise RuntimeError("최근 1년 데이터가 없습니다.")

    market = index_config.get("market", "us")
    annotate_distribution_lifecycle(one_year_rows)
    detect_follow_through_days(one_year_rows)
    annotate_witching(one_year_rows, market)
    attach_breadth(index_id, one_year_rows)

    recent25_rows = one_year_rows[-DISTRIBUTION_WINDOW_DAYS:]
    recent25_distribution = [row for row in recent25_rows if row["distribution"]]
    active_pressure = [row for row in one_year_rows if row["active"]]
    active_count = len(active_pressure)
    active_ex_witching = [row for row in active_pressure if not row.get("witching")]
    active_ex_count = len(active_ex_witching)
    stalling_count = sum(1 for row in active_pressure if row.get("stalling"))
    one_year_distribution = [row for row in one_year_rows if row["distribution"]]
    latest = one_year_rows[-1]

    # 순압력: 최근 25거래일 유효 압박일(분산+정체) vs 매집일
    accumulation_recent = sum(1 for row in recent25_rows if row.get("accumulation"))
    active_recent = sum(1 for row in recent25_rows if row.get("active"))
    net_pressure = active_recent - accumulation_recent

    follow_through_days = [row for row in one_year_rows if row.get("followThrough")]
    latest_ftd = follow_through_days[-1]["date"] if follow_through_days else None

    cluster_slice = one_year_rows[-CLUSTER_WINDOW_DAYS:]
    cluster_count = sum(1 for row in cluster_slice if row["active"])
    cluster_warning = cluster_count >= CLUSTER_THRESHOLD

    payload = {
        "index": {
            "id": index_id,
            "name": index_config["name"],
            "symbol": index_config["symbol"],
            "currency": index_config["currency"],
            "timezone": index_config["timezone"],
            "market": market,
            "lowQuality": index_id in LOW_QUALITY_INDEXES,
        },
        "source": source,
        "stale": stale,
        "warning": warning,
        "asOf": latest["date"],
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "recent25TradingDays": len(recent25_rows),
            "recent25DistributionCount": len(recent25_distribution),
            "activeDistributionCount": active_count,
            "activeDistributionExWitchingCount": active_ex_count,
            "stallingCount": stalling_count,
            "accumulationRecentCount": accumulation_recent,
            "netPressure": net_pressure,
            "oneYearTradingDays": len(one_year_rows),
            "oneYearDistributionCount": len(one_year_distribution),
        },
        "phase": market_phase(active_count),
        "phaseExWitching": market_phase(active_ex_count),
        "cluster": {
            "warning": cluster_warning,
            "count": cluster_count,
            "windowDays": CLUSTER_WINDOW_DAYS,
            "threshold": CLUSTER_THRESHOLD,
        },
        "followThrough": {
            "count": len(follow_through_days),
            "latest": latest_ftd,
        },
        "latest": latest,
        "series": one_year_rows,
        "recent25DistributionDays": recent25_distribution,
        "activeDistributionDays": active_pressure,
        "oneYearDistributionDays": one_year_distribution,
    }
    if not stale:
        _CACHE[index_id] = {
            "created_at": time.time(), "completed_at": time.monotonic(),
            "source_refetched": source_refetched, "payload": payload,
        }
    return payload


def attach_breadth(index_id: str, one_year_rows: List[Dict[str, Any]]) -> None:
    """저장된 시장 폭만 붙인다. 누락된 날짜 수집은 별도 갱신 작업에서 실행한다."""
    if index_id not in KRX_STOCK_ENDPOINT:
        return
    recent = one_year_rows[-BREADTH_WINDOW_DAYS:]
    iso_dates = [row["date"] for row in recent]
    with _KRX_CACHE_LOCK:
        cache = load_breadth_cache(index_id)
        summaries = {iso: cache["rows"][iso] for iso in iso_dates if iso in cache["rows"]}
    for row in recent:
        summary = summaries.get(row["date"])
        if not summary:
            continue
        adv = summary["advancers"]
        dec = summary["decliners"]
        up_vol = summary["upVolume"]
        down_vol = summary["downVolume"]
        row["breadth"] = {
            "advancers": adv,
            "decliners": dec,
            "unchanged": summary["unchanged"],
            "adRatio": round(adv / dec, 2) if dec else None,
            "upDownVolRatio": round(up_vol / down_vol, 2) if down_vol else None,
        }


def data_cache_headers(status: int, payload: Dict[str, Any], force_refresh: bool) -> Dict[str, str]:
    cacheable = status == 200 and not force_refresh and not payload.get("stale")
    return {
        "Cache-Control": "no-store",
        "Vercel-CDN-Cache-Control": (
            "public, s-maxage=300, stale-while-revalidate=600" if cacheable else "no-store"
        ),
    }


def parse_data_query(query_string: str) -> Tuple[str, bool]:
    if len(query_string) > 2048:
        raise ValueError("Query too long")
    query = parse_qs(query_string, keep_blank_values=True, max_num_fields=8)
    if set(query) - {"index", "refresh", "_"} or any(len(values) != 1 for values in query.values()):
        raise ValueError("Unsupported or duplicate parameter")
    index_id = query.get("index", ["kospi"])[0]
    refresh = query.get("refresh", ["0"])[0].lower()
    if not index_id or len(index_id) > 16 or refresh not in {"0", "1", "false", "true", "no", "yes", "n", "y"}:
        raise ValueError("Invalid parameter")
    timestamp = query.get("_", ["0"])[0]
    if not timestamp.isascii() or not timestamp.isdecimal() or len(timestamp) > 20:
        raise ValueError("Invalid timestamp")
    return index_id, refresh in {"1", "true", "yes", "y"}


def data_response(index_id: str, force_refresh: bool) -> Tuple[int, Dict[str, Any]]:
    """(HTTP 상태코드, JSON 본문) 튜플을 돌려준다."""
    if index_id not in INDEXES:
        return 404, {"error": "지원하지 않는 지수입니다."}
    try:
        return 200, market_payload(index_id, force_refresh=force_refresh)
    except (RuntimeError, ValueError, TypeError, KeyError, OSError) as exc:
        sys.stderr.write(f"데이터 응답 실패({index_id}): {type(exc).__name__}\n")
        return 502, {"error": "데이터를 가져오지 못했습니다. 잠시 후 다시 시도해 주세요."}
