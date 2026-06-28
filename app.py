#!/usr/bin/env python3
"""Local dashboard for market distribution days."""

from __future__ import annotations

import json
import shutil
import sys
import time
import subprocess
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.9+ on the target machine has this.
    ZoneInfo = None  # type: ignore[assignment]


HOST = "127.0.0.1"
PORT = 8000
CACHE_TTL_SECONDS = 600
YAHOO_CHART_URLS = (
    "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
)
USER_AGENT = "Mozilla/5.0"
DISTRIBUTION_DROP_THRESHOLD_PCT = -0.2

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


HTML = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>분산일 대시보드</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f7f8;
      --surface: #ffffff;
      --line: #d9e0e4;
      --text: #182127;
      --muted: #66737c;
      --price: #146c8c;
      --volume: #8aa4b1;
      --danger: #c83e32;
      --good: #24795a;
      --focus: #243b53;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 15px;
      line-height: 1.45;
    }

    header {
      background: var(--surface);
      border-bottom: 1px solid var(--line);
    }

    main,
    .header-inner {
      width: min(1440px, calc(100% - 32px));
      margin: 0 auto;
    }

    .header-inner {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 24px;
      min-height: 72px;
    }

    h1 {
      margin: 0;
      font-size: 22px;
      font-weight: 760;
      letter-spacing: 0;
    }

    .as-of {
      color: var(--muted);
      font-size: 13px;
      text-align: right;
      white-space: nowrap;
    }

    main {
      padding: 22px 0 40px;
    }

    .toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 18px;
    }

    .index-buttons {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }

    .toolbar-actions {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 10px;
    }

    button {
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      color: var(--text);
      padding: 0 14px;
      font: inherit;
      font-weight: 680;
      cursor: pointer;
    }

    button:hover {
      border-color: #9cb3bd;
      background: #f9fbfc;
    }

    button:focus-visible {
      outline: 3px solid rgba(36, 59, 83, 0.22);
      outline-offset: 2px;
    }

    button.active {
      border-color: var(--focus);
      background: var(--focus);
      color: #ffffff;
    }

    button:disabled {
      cursor: wait;
      opacity: 0.62;
    }

    .refresh-button {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      white-space: nowrap;
    }

    .status {
      color: var(--muted);
      font-size: 13px;
      text-align: right;
      min-width: 140px;
    }

    .summary-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }

    .metric,
    .chart-panel,
    .table-panel {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
    }

    .metric {
      min-height: 96px;
      padding: 16px;
    }

    .metric-label {
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 8px;
    }

    .metric-value {
      font-size: 28px;
      font-weight: 780;
      letter-spacing: 0;
    }

    .metric-detail {
      margin-top: 6px;
      color: var(--muted);
      font-size: 13px;
    }

    .chart-panel {
      padding: 16px;
      margin-bottom: 16px;
    }

    .chart-head {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 12px;
    }

    .chart-title {
      font-size: 17px;
      font-weight: 740;
    }

    .legend {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 12px;
      color: var(--muted);
      font-size: 13px;
    }

    .legend span {
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }

    .swatch {
      width: 10px;
      height: 10px;
      border-radius: 999px;
      display: inline-block;
    }

    .chart-wrap {
      position: relative;
      height: 520px;
      min-height: 360px;
    }

    .tables {
      display: grid;
      grid-template-columns: minmax(0, 0.9fr) minmax(0, 1.1fr);
      gap: 16px;
    }

    .table-panel {
      overflow: hidden;
    }

    .table-title {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      font-weight: 740;
    }

    .table-count {
      color: var(--muted);
      font-weight: 620;
    }

    .table-scroll {
      max-height: 390px;
      overflow: auto;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }

    th,
    td {
      padding: 10px 12px;
      border-bottom: 1px solid #edf1f3;
      text-align: right;
      white-space: nowrap;
    }

    th:first-child,
    td:first-child {
      text-align: left;
    }

    th {
      position: sticky;
      top: 0;
      background: #fbfcfd;
      z-index: 1;
      color: var(--muted);
      font-weight: 720;
    }

    tr:last-child td {
      border-bottom: 0;
    }

    .negative {
      color: var(--danger);
      font-weight: 720;
    }

    .positive {
      color: var(--good);
      font-weight: 720;
    }

    .empty {
      color: var(--muted);
      padding: 18px 16px;
    }

    .error {
      display: none;
      margin-bottom: 16px;
      padding: 14px 16px;
      border: 1px solid #e2a29b;
      border-radius: 8px;
      background: #fff7f5;
      color: #8f271f;
      font-weight: 650;
    }

    footer {
      margin-top: 18px;
      color: var(--muted);
      font-size: 12px;
    }

    @media (max-width: 980px) {
      .summary-grid,
      .tables {
        grid-template-columns: 1fr 1fr;
      }
    }

    @media (max-width: 720px) {
      main,
      .header-inner {
        width: min(100% - 24px, 1440px);
      }

      .header-inner,
      .toolbar,
      .chart-head {
        align-items: flex-start;
        flex-direction: column;
      }

      .toolbar-actions {
        align-items: flex-start;
        flex-direction: column-reverse;
      }

      .as-of,
      .status {
        text-align: left;
        white-space: normal;
      }

      .summary-grid,
      .tables {
        grid-template-columns: 1fr;
      }

      .chart-wrap {
        height: 430px;
      }

      button {
        padding: 0 12px;
      }
    }
  </style>
</head>
<body>
  <header>
    <div class="header-inner">
      <h1>분산일 대시보드</h1>
      <div class="as-of" id="asOf">데이터 대기 중</div>
    </div>
  </header>

  <main>
    <div class="toolbar">
      <div class="index-buttons" id="indexButtons"></div>
      <div class="toolbar-actions">
        <button class="refresh-button" id="refreshButton" type="button" aria-label="데이터 새로고침">
          <span aria-hidden="true">↻</span>
          <span>새로고침</span>
        </button>
        <div class="status" id="status">초기화 중</div>
      </div>
    </div>

    <div class="error" id="errorBox"></div>

    <section class="summary-grid" aria-label="요약">
      <div class="metric">
        <div class="metric-label">최근 25거래일 분산일</div>
        <div class="metric-value" id="recent25Count">-</div>
        <div class="metric-detail" id="recent25Detail">-</div>
      </div>
      <div class="metric">
        <div class="metric-label">최근 1년 분산일</div>
        <div class="metric-value" id="oneYearCount">-</div>
        <div class="metric-detail" id="oneYearDetail">-</div>
      </div>
      <div class="metric">
        <div class="metric-label">최신 종가</div>
        <div class="metric-value" id="latestClose">-</div>
        <div class="metric-detail" id="latestChange">-</div>
      </div>
      <div class="metric">
        <div class="metric-label">최신 거래량</div>
        <div class="metric-value" id="latestVolume">-</div>
        <div class="metric-detail" id="latestVolumeChange">-</div>
      </div>
    </section>

    <section class="chart-panel">
      <div class="chart-head">
        <div>
          <div class="chart-title" id="chartTitle">최근 1년 지수와 거래량</div>
        </div>
        <div class="legend" aria-label="범례">
          <span><i class="swatch" style="background: var(--price)"></i>종가</span>
          <span><i class="swatch" style="background: var(--volume)"></i>거래량</span>
          <span><i class="swatch" style="background: var(--danger)"></i>분산일</span>
        </div>
      </div>
      <div class="chart-wrap">
        <canvas id="marketChart"></canvas>
      </div>
    </section>

    <section class="tables" aria-label="분산일 목록">
      <div class="table-panel">
        <div class="table-title">
          <span>최근 25거래일</span>
          <span class="table-count" id="recent25TableCount">-</span>
        </div>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>날짜</th>
                <th>종가</th>
                <th>등락률</th>
                <th>거래량 증감률</th>
              </tr>
            </thead>
            <tbody id="recent25Rows"></tbody>
          </table>
          <div class="empty" id="recent25Empty">-</div>
        </div>
      </div>

      <div class="table-panel">
        <div class="table-title">
          <span>최근 1년</span>
          <span class="table-count" id="oneYearTableCount">-</span>
        </div>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>날짜</th>
                <th>종가</th>
                <th>등락률</th>
                <th>거래량 증감률</th>
              </tr>
            </thead>
            <tbody id="oneYearRows"></tbody>
          </table>
          <div class="empty" id="oneYearEmpty">-</div>
        </div>
      </div>
    </section>

    <footer id="sourceNote">분산일 기준: 전 거래일 대비 종가 0.2% 이상 하락 및 거래량 증가.</footer>
  </main>

  <script>
    const elements = {
      asOf: document.getElementById("asOf"),
      status: document.getElementById("status"),
      refreshButton: document.getElementById("refreshButton"),
      errorBox: document.getElementById("errorBox"),
      indexButtons: document.getElementById("indexButtons"),
      recent25Count: document.getElementById("recent25Count"),
      recent25Detail: document.getElementById("recent25Detail"),
      oneYearCount: document.getElementById("oneYearCount"),
      oneYearDetail: document.getElementById("oneYearDetail"),
      latestClose: document.getElementById("latestClose"),
      latestChange: document.getElementById("latestChange"),
      latestVolume: document.getElementById("latestVolume"),
      latestVolumeChange: document.getElementById("latestVolumeChange"),
      chartTitle: document.getElementById("chartTitle"),
      recent25Rows: document.getElementById("recent25Rows"),
      recent25Empty: document.getElementById("recent25Empty"),
      oneYearRows: document.getElementById("oneYearRows"),
      oneYearEmpty: document.getElementById("oneYearEmpty"),
      recent25TableCount: document.getElementById("recent25TableCount"),
      oneYearTableCount: document.getElementById("oneYearTableCount"),
      sourceNote: document.getElementById("sourceNote"),
    };

    const numberFormat = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 2 });
    const integerFormat = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 0 });
    const compactFormat = new Intl.NumberFormat("ko-KR", {
      notation: "compact",
      maximumFractionDigits: 1,
    });

    let activeIndex = "kospi";
    let indexList = [];
    let chart = null;
    let isLoading = false;

    function formatNumber(value) {
      if (value === null || value === undefined || Number.isNaN(value)) return "-";
      return numberFormat.format(value);
    }

    function formatVolume(value) {
      if (value === null || value === undefined || Number.isNaN(value)) return "-";
      return value >= 1000000 ? compactFormat.format(value) : integerFormat.format(value);
    }

    function formatPct(value) {
      if (value === null || value === undefined || Number.isNaN(value)) return "-";
      const sign = value > 0 ? "+" : "";
      return `${sign}${numberFormat.format(value)}%`;
    }

    function pctClass(value) {
      if (value < 0) return "negative";
      if (value > 0) return "positive";
      return "";
    }

    function setStatus(message) {
      elements.status.textContent = message;
    }

    function setLoading(loading) {
      isLoading = loading;
      elements.refreshButton.disabled = loading;
      for (const button of elements.indexButtons.querySelectorAll("button")) {
        button.disabled = loading;
      }
    }

    function setError(message) {
      elements.errorBox.textContent = message;
      elements.errorBox.style.display = message ? "block" : "none";
    }

    async function fetchJson(url) {
      const response = await fetch(url, {
        cache: "no-store",
        headers: { "Accept": "application/json" },
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.error || `요청 실패: ${response.status}`);
      }
      return body;
    }

    async function loadIndexes() {
      const payload = await fetchJson("/api/indexes");
      indexList = payload.indexes;
      elements.indexButtons.innerHTML = "";

      for (const item of indexList) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = item.name;
        button.dataset.index = item.id;
        button.addEventListener("click", () => {
          if (isLoading) return;
          activeIndex = item.id;
          loadData(item.id);
        });
        elements.indexButtons.appendChild(button);
      }
    }

    function updateButtons() {
      for (const button of elements.indexButtons.querySelectorAll("button")) {
        button.classList.toggle("active", button.dataset.index === activeIndex);
      }
    }

    async function loadData(indexId) {
      activeIndex = indexId;
      updateButtons();
      setError("");
      setLoading(true);
      setStatus("데이터 불러오는 중");

      try {
        const payload = await fetchJson(`/api/data?index=${encodeURIComponent(indexId)}`);
        render(payload);
        setStatus("완료");
      } catch (error) {
        setStatus("오류");
        setError(error.message || "데이터를 불러오지 못했습니다.");
      } finally {
        setLoading(false);
      }
    }

    async function refreshData() {
      if (isLoading) return;
      activeIndex = activeIndex || "kospi";
      updateButtons();
      setError("");
      setLoading(true);
      setStatus("새로고침 중");

      try {
        const url = `/api/data?index=${encodeURIComponent(activeIndex)}&refresh=1&_=${Date.now()}`;
        const payload = await fetchJson(url);
        render(payload);
        setStatus("완료");
      } catch (error) {
        setStatus("오류");
        setError(error.message || "데이터를 새로고침하지 못했습니다.");
      } finally {
        setLoading(false);
      }
    }

    function render(payload) {
      const summary = payload.summary;
      const latest = payload.latest;
      const index = payload.index;

      elements.asOf.textContent = `${index.name} 기준일 ${payload.asOf}`;
      elements.recent25Count.textContent = `${summary.recent25DistributionCount}일`;
      elements.recent25Detail.textContent = `${summary.recent25TradingDays}거래일 중`;
      elements.oneYearCount.textContent = `${summary.oneYearDistributionCount}일`;
      elements.oneYearDetail.textContent = `${summary.oneYearTradingDays}거래일 중`;
      elements.latestClose.textContent = formatNumber(latest.close);
      elements.latestChange.textContent = formatPct(latest.closeChangePct);
      elements.latestChange.className = `metric-detail ${pctClass(latest.closeChangePct)}`;
      elements.latestVolume.textContent = formatVolume(latest.volume);
      elements.latestVolumeChange.textContent = formatPct(latest.volumeChangePct);
      elements.latestVolumeChange.className = `metric-detail ${pctClass(latest.volumeChangePct)}`;
      elements.chartTitle.textContent = `${index.name} 최근 1년 지수와 거래량`;
      elements.sourceNote.textContent = `출처: ${payload.source}. 분산일 기준: 전 거래일 대비 종가 0.2% 이상 하락 및 거래량 증가.`;

      renderChart(payload);
      renderRows(elements.recent25Rows, elements.recent25Empty, payload.recent25DistributionDays);
      renderRows(elements.oneYearRows, elements.oneYearEmpty, payload.oneYearDistributionDays);
      elements.recent25TableCount.textContent = `${payload.recent25DistributionDays.length}일`;
      elements.oneYearTableCount.textContent = `${payload.oneYearDistributionDays.length}일`;
    }

    function renderRows(tbody, emptyElement, rows) {
      tbody.innerHTML = "";
      emptyElement.style.display = rows.length ? "none" : "block";
      emptyElement.textContent = "분산일 없음";

      for (const row of rows) {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${row.date}</td>
          <td>${formatNumber(row.close)}</td>
          <td class="${pctClass(row.closeChangePct)}">${formatPct(row.closeChangePct)}</td>
          <td class="${pctClass(row.volumeChangePct)}">${formatPct(row.volumeChangePct)}</td>
        `;
        tbody.appendChild(tr);
      }
    }

    function renderChart(payload) {
      const labels = payload.series.map((row) => row.date);
      const closeData = payload.series.map((row) => row.close);
      const volumeData = payload.series.map((row) => row.volume);
      const distributionData = payload.series.map((row) => row.distribution ? row.close : null);

      const context = document.getElementById("marketChart").getContext("2d");
      if (chart) chart.destroy();

      chart = new Chart(context, {
        data: {
          labels,
          datasets: [
            {
              type: "bar",
              label: "거래량",
              data: volumeData,
              yAxisID: "volume",
              backgroundColor: "rgba(138, 164, 177, 0.28)",
              borderColor: "rgba(138, 164, 177, 0.5)",
              borderWidth: 1,
              barPercentage: 0.9,
              categoryPercentage: 0.9,
              order: 3,
            },
            {
              type: "line",
              label: "종가",
              data: closeData,
              yAxisID: "price",
              borderColor: "#146c8c",
              backgroundColor: "rgba(20, 108, 140, 0.08)",
              borderWidth: 2,
              pointRadius: 0,
              tension: 0.18,
              order: 1,
            },
            {
              type: "line",
              label: "분산일",
              data: distributionData,
              yAxisID: "price",
              showLine: false,
              pointRadius: 4.5,
              pointHoverRadius: 7,
              pointBackgroundColor: "#c83e32",
              pointBorderColor: "#ffffff",
              pointBorderWidth: 1.5,
              order: 0,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: {
            mode: "index",
            intersect: false,
          },
          plugins: {
            legend: {
              display: false,
            },
            tooltip: {
              callbacks: {
                afterBody(items) {
                  const row = payload.series[items[0].dataIndex];
                  if (!row) return "";
                  const lines = [
                    `등락률: ${formatPct(row.closeChangePct)}`,
                    `거래량 증감: ${formatPct(row.volumeChangePct)}`,
                  ];
                  if (row.distribution) lines.push("분산일");
                  return lines;
                },
              },
            },
          },
          scales: {
            x: {
              grid: { display: false },
              ticks: {
                maxTicksLimit: 10,
                maxRotation: 0,
              },
            },
            price: {
              position: "left",
              grid: { color: "rgba(24, 33, 39, 0.08)" },
              ticks: {
                callback: (value) => formatNumber(value),
              },
            },
            volume: {
              position: "right",
              grid: { display: false },
              beginAtZero: true,
              ticks: {
                callback: (value) => formatVolume(value),
              },
            },
          },
        },
      });
    }

    async function boot() {
      try {
        elements.refreshButton.addEventListener("click", refreshData);
        await loadIndexes();
        await loadData(activeIndex);
      } catch (error) {
        setStatus("오류");
        setError(error.message || "초기화 실패");
      }
    }

    boot();
  </script>
</body>
</html>
"""


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


def fetch_json_url(url: str) -> Dict[str, Any]:
    curl_path = shutil.which("curl")
    if curl_path:
        return fetch_json_with_curl(curl_path, url)
    return fetch_json_with_urllib(url)


def fetch_json_with_curl(curl_path: str, url: str) -> Dict[str, Any]:
    result = subprocess.run(
        [
            curl_path,
            "-fsSL",
            "--compressed",
            "--max-time",
            "25",
            "-A",
            USER_AGENT,
            "-H",
            "Accept: application/json",
            url,
        ],
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


def fetch_json_with_urllib(url: str) -> Dict[str, Any]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Connection": "close",
            "User-Agent": USER_AGENT,
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
        "source": "Yahoo Finance chart API",
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


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "DistributionDayDashboard/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self.send_html(HTML)
            return

        if parsed.path == "/api/indexes":
            self.send_json({"indexes": index_payloads()})
            return

        if parsed.path == "/api/data":
            query = parse_qs(parsed.query)
            index_id = (query.get("index") or ["kospi"])[0]
            refresh_value = (query.get("refresh") or ["0"])[0].lower()
            force_refresh = refresh_value in ("1", "true", "yes", "y")
            if index_id not in INDEXES:
                self.send_json({"error": "지원하지 않는 지수입니다."}, status=404)
                return
            try:
                self.send_json(market_payload(index_id, force_refresh=force_refresh))
            except RuntimeError as exc:
                self.send_json({"error": str(exc)}, status=502)
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
