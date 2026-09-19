const elements = {
  asOf: document.getElementById("asOf"),
  status: document.getElementById("status"),
  refreshButton: document.getElementById("refreshButton"),
  errorBox: document.getElementById("errorBox"),
  indexButtons: document.getElementById("indexButtons"),
  phaseBanner: document.getElementById("phaseBanner"),
  phaseLabel: document.getElementById("phaseLabel"),
  phaseAction: document.getElementById("phaseAction"),
  phaseCount: document.getElementById("phaseCount"),
  phaseCluster: document.getElementById("phaseCluster"),
  witchingToggle: document.getElementById("witchingToggle"),
  qualityNote: document.getElementById("qualityNote"),
  recent25Count: document.getElementById("recent25Count"),
  recent25Detail: document.getElementById("recent25Detail"),
  netPressure: document.getElementById("netPressure"),
  netPressureDetail: document.getElementById("netPressureDetail"),
  chipStalling: document.getElementById("chipStalling"),
  chipFtd: document.getElementById("chipFtd"),
  chipBreadthAd: document.getElementById("chipBreadthAd"),
  chipBreadthVol: document.getElementById("chipBreadthVol"),
  chipOneYear: document.getElementById("chipOneYear"),
  latestClose: document.getElementById("latestClose"),
  latestChange: document.getElementById("latestChange"),
  latestVolume: document.getElementById("latestVolume"),
  latestVolumeChange: document.getElementById("latestVolumeChange"),
  chartTitle: document.getElementById("chartTitle"),
  chartRange: document.getElementById("chartRange"),
  chartZoomIn: document.getElementById("chartZoomIn"),
  chartZoomOut: document.getElementById("chartZoomOut"),
  chartReset: document.getElementById("chartReset"),
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
let chartIndexId = null;
let chartPayload = null;
const minChartRange = 4;
let isLoading = false;
let lastPayload = null;
let excludeWitching = false;
let dataController = null;
let requestId = 0;
const payloadCache = new Map();
const payloadCacheTtl = 10 * 60 * 1000;

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
}

function setError(message) {
  elements.errorBox.textContent = message;
  elements.errorBox.style.display = message ? "block" : "none";
}

async function fetchJson(url, signal, forceRefresh = false) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal?.aborted) controller.abort();
  signal?.addEventListener("abort", abort, { once: true });
  const timeout = setTimeout(abort, 15000);
  try {
    const response = await fetch(url, {
      cache: forceRefresh ? "no-store" : "default",
      signal: controller.signal,
      headers: { "Accept": "application/json" },
    });
    const body = await response.json();
    if (!response.ok) {
      throw new Error(body.error || `요청 실패: ${response.status}`);
    }
    return body;
  } catch (error) {
    if (controller.signal.aborted && !signal?.aborted) {
      throw new Error("응답이 지연되고 있습니다. 잠시 후 다시 새로고침해 주세요.");
    }
    throw error;
  } finally {
    clearTimeout(timeout);
    signal?.removeEventListener("abort", abort);
  }
}

function validPayload(payload, indexId) {
  const validRow = row => row && /^\d{4}-\d{2}-\d{2}$/.test(row.date) &&
    Number.isFinite(row.close) && Number.isFinite(row.volume);
  const validRows = rows => Array.isArray(rows) && rows.length <= 400 && rows.every(validRow);
  return payload?.index?.id === indexId && typeof payload.index.name === "string" &&
    /^\d{4}-\d{2}-\d{2}$/.test(payload.asOf) && Number.isFinite(Date.parse(payload.generatedAt)) &&
    validRows(payload.series) && payload.series.length >= 2 && validRow(payload.latest) &&
    validRows(payload.activeDistributionDays) && validRows(payload.oneYearDistributionDays) &&
    payload.summary && ["activeDistributionCount", "recent25DistributionCount", "oneYearDistributionCount",
      "netPressure", "accumulationRecentCount", "stallingCount"].every(key => Number.isFinite(payload.summary[key]));
}

function cachedPayload(indexId) {
  if (payloadCache.has(indexId)) return payloadCache.get(indexId);
  try {
    const cached = JSON.parse(sessionStorage.getItem(`market-v2:${indexId}`));
    if (validPayload(cached?.payload, indexId) && Number.isFinite(cached.fetchedAt) &&
        cached.fetchedAt <= Date.now() + 60000) {
      payloadCache.set(indexId, cached);
      return cached;
    }
    sessionStorage.removeItem(`market-v2:${indexId}`);
  } catch (_) { /* Storage can be unavailable in private browsing. */ }
  return null;
}

function rememberPayload(indexId, payload) {
  // Use the source generation time so layered caches cannot extend freshness.
  const generatedAt = Date.parse(payload.generatedAt);
  const cached = { payload, fetchedAt: Number.isFinite(generatedAt) ? generatedAt : 0 };
  payloadCache.set(indexId, cached);
  try {
    sessionStorage.setItem(`market-v2:${indexId}`, JSON.stringify(cached));
  } catch (_) { /* The in-memory cache still works if storage is full. */ }
}

async function loadIndexes() {
  const payload = await fetchJson("/api/indexes");
  indexList = payload.indexes;
  elements.indexButtons.replaceChildren();

  for (const item of indexList) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = item.name;
    button.dataset.index = item.id;
    button.addEventListener("click", () => {
      loadData(item.id);
    });
    elements.indexButtons.appendChild(button);
  }
  updateButtons();
}

function updateButtons() {
  for (const button of elements.indexButtons.querySelectorAll("button")) {
    button.classList.toggle("active", button.dataset.index === activeIndex);
  }
}

async function loadData(indexId, { forceRefresh = false } = {}) {
  const currentRequest = ++requestId;
  dataController?.abort();
  dataController = new AbortController();
  const controller = dataController;
  activeIndex = indexId;
  updateButtons();
  setError("");
  const cached = cachedPayload(indexId);
  if (cached) {
    render(cached.payload);
    if (!forceRefresh && !cached.payload.stale &&
        Date.now() - cached.fetchedAt < payloadCacheTtl) {
      setStatus("완료");
      setLoading(false);
      return;
    }
  }
  setLoading(true);
  setStatus(forceRefresh ? "새로고침 중" : cached ? "저장된 데이터 · 업데이트 중" : "데이터 불러오는 중");

  try {
    let url = `/api/data?index=${encodeURIComponent(indexId)}`;
    if (forceRefresh) url += `&refresh=1&_=${Date.now()}`;
    let payload = await fetchJson(url, controller.signal, forceRefresh);
    if (currentRequest !== requestId) return;
    if (!validPayload(payload, indexId)) {
      throw new Error("데이터 응답을 확인하지 못했습니다.");
    }
    if (payload.stale && cached && cached.payload.asOf > payload.asOf) {
      payload = { ...cached.payload, stale: true, warning: payload.warning };
    }
    rememberPayload(indexId, payload);
    render(payload);
    setStatus(payload.stale ? "저장된 데이터" : "완료");
    setError(payload.warning || "");
  } catch (error) {
    if (currentRequest !== requestId) return;
    setStatus(cached ? "저장된 데이터" : "오류");
    setError(error.message || "데이터를 불러오지 못했습니다.");
  } finally {
    if (currentRequest === requestId) setLoading(false);
  }
}

async function refreshData() {
  if (isLoading) return;
  await loadData(activeIndex || "kospi", { forceRefresh: true });
}

function textElement(tag, text, className = "") {
  const element = document.createElement(tag);
  element.textContent = text;
  element.className = className;
  return element;
}

function typeBadge(row) {
  const cell = textElement("td", "", "type-cell");
  if (row.stalling) cell.append(textElement("span", "정체", "badge stalling"));
  else if (row.distribution) cell.append(textElement("span", "분산", "badge dist"));
  if (row.witching) cell.append(" ", textElement("span", "만기", "badge witch"));
  if (!cell.childNodes.length) cell.textContent = "-";
  return cell;
}

function render(payload) {
  lastPayload = payload;
  const summary = payload.summary;
  const latest = payload.latest;
  const index = payload.index;
  const cluster = payload.cluster || {};

  // 만기일 제외 토글에 따라 국면·카운트 전환
  const useEx = excludeWitching;
  const phase = (useEx ? payload.phaseExWitching : payload.phase) || {};
  const activeCount = useEx
    ? (summary.activeDistributionExWitchingCount ?? summary.activeDistributionCount)
    : summary.activeDistributionCount;

  elements.asOf.textContent = `${index.name} 기준일 ${payload.asOf}`;
  elements.phaseBanner.className = `phase-banner ${phase.level || ""}`;
  elements.phaseLabel.textContent = phase.label || "-";
  elements.phaseAction.textContent = phase.action || "";
  elements.phaseCount.textContent = `유효 압박일 ${activeCount}일${useEx ? " (만기 제외)" : ""}`;
  elements.phaseCluster.textContent = cluster.warning
    ? `⚠ 최근 ${cluster.windowDays}거래일에 ${cluster.count}일 집중 — 조정 임박 신호`
    : "";

  elements.qualityNote.textContent = index.lowQuality
    ? "⚠ 코스닥은 개인 비중이 높아 거래량 변동성이 커 신호 품질이 낮습니다. 참고 지표로만 활용하세요."
    : "";

  elements.recent25Count.textContent = `${activeCount}일`;
  elements.recent25Detail.textContent = `창 내 발생 ${summary.recent25DistributionCount}일 중 (5% 회복·기간경과 제외)`;

  const net = summary.netPressure;
  elements.netPressure.textContent = net > 0 ? `+${net}` : `${net}`;
  elements.netPressure.className = `metric-value ${net > 0 ? "negative" : net < 0 ? "positive" : ""}`;
  elements.netPressureDetail.textContent = `압박 ${summary.activeDistributionCount} − 매집 ${summary.accumulationRecentCount} (음수일수록 강세)`;

  elements.latestClose.textContent = formatNumber(latest.close);
  elements.latestChange.textContent = formatPct(latest.closeChangePct);
  elements.latestChange.className = `metric-detail ${pctClass(latest.closeChangePct)}`;
  elements.latestVolume.textContent = formatVolume(latest.volume);
  elements.latestVolumeChange.textContent = formatPct(latest.volumeChangePct);
  elements.latestVolumeChange.className = `metric-detail ${pctClass(latest.volumeChangePct)}`;

  renderSignalStrip(payload);

  elements.chartTitle.textContent = `${index.name} 최근 1년 지수와 거래량`;
  elements.sourceNote.textContent = `출처: ${payload.source}. 분산일 = 전 거래일 대비 종가 0.2%↓ + 거래량 증가. 정체일(거래량↑·상승폭 미미·종가 저가권)도 압박일로 합산. 유효 압박일 = 25거래일 창 내 미소멸(종가 대비 5%↑ 상승 시 소멸). FTD = 조정 후 재진입 신호. 후행 지표로, 확인 신호이지 예측 신호가 아닙니다.`;

  renderChart(payload);
  renderRows(elements.recent25Rows, elements.recent25Empty, payload.activeDistributionDays);
  renderRows(elements.oneYearRows, elements.oneYearEmpty, payload.oneYearDistributionDays);
  elements.recent25TableCount.textContent = `${payload.activeDistributionDays.length}일`;
  elements.oneYearTableCount.textContent = `${payload.oneYearDistributionDays.length}일`;
}

function renderSignalStrip(payload) {
  const summary = payload.summary;
  const ftd = payload.followThrough || {};
  const latestBreadth = [...payload.series].reverse().find((row) => row.breadth);

  elements.chipStalling.replaceChildren();
  if (summary.stallingCount > 0) {
    elements.chipStalling.append("정체일 ", textElement("b", summary.stallingCount), "일 (유효 압박에 포함)");
  }
  elements.chipFtd.replaceChildren();
  if (ftd.latest) {
    const signal = textElement("span", "", "good");
    signal.append(textElement("b", "▲ 최근 FTD"), ` ${ftd.latest}`);
    elements.chipFtd.append(signal, " 재진입 신호");
  }

  if (latestBreadth && latestBreadth.breadth) {
    const b = latestBreadth.breadth;
    const adCls = b.adRatio != null && b.adRatio < 1 ? "bad" : "good";
    const volCls = b.upDownVolRatio != null && b.upDownVolRatio < 1 ? "bad" : "good";
    elements.chipBreadthAd.className = `chip ${adCls}`;
    elements.chipBreadthAd.replaceChildren("시장 폭 등락 ",
      textElement("b", `${b.advancers}↑ / ${b.decliners}↓`), b.adRatio != null ? ` (A/D ${b.adRatio})` : "");
    elements.chipBreadthVol.className = `chip ${volCls}`;
    elements.chipBreadthVol.replaceChildren();
    if (b.upDownVolRatio != null) elements.chipBreadthVol.append("상승/하락 거래량 ", textElement("b", b.upDownVolRatio));
  } else {
    elements.chipBreadthAd.className = "chip";
    elements.chipBreadthAd.replaceChildren();
    elements.chipBreadthVol.className = "chip";
    elements.chipBreadthVol.replaceChildren();
  }

  elements.chipOneYear.replaceChildren("최근 1년 분산일 ", textElement("b", summary.oneYearDistributionCount), "일 (참고)");
}

function renderRows(tbody, emptyElement, rows) {
  const fragment = document.createDocumentFragment();
  emptyElement.style.display = rows.length ? "none" : "block";
  emptyElement.textContent = "해당 신호 없음";

  for (const row of [...rows].sort((a, b) => b.date.localeCompare(a.date))) {
    const tr = document.createElement("tr");
    tr.append(textElement("td", row.date), typeBadge(row), textElement("td", formatNumber(row.close)),
      textElement("td", formatPct(row.closeChangePct), pctClass(row.closeChangePct)),
      textElement("td", formatPct(row.volumeChangePct), pctClass(row.volumeChangePct)));
    fragment.appendChild(tr);
  }
  tbody.replaceChildren(fragment);
}

function updateChartControls({ chart: currentChart } = { chart }) {
  if (!currentChart) return;
  const labels = currentChart.data.labels;
  const min = Math.max(0, Math.ceil(currentChart.scales.x.min));
  const max = Math.min(labels.length - 1, Math.floor(currentChart.scales.x.max));
  const zoomed = min > 0 || max < labels.length - 1;
  elements.chartRange.textContent = `${labels[min]} ~ ${labels[max]} · ${max - min + 1}거래일`;
  elements.chartZoomIn.disabled = max - min <= minChartRange;
  elements.chartZoomOut.disabled = !zoomed;
  elements.chartReset.disabled = !zoomed;
  currentChart.canvas.style.cursor = zoomed ? "grab" : "crosshair";
}

function zoomChart(factor) {
  if (!chart) return;
  chart.zoom({ x: factor });
  updateChartControls();
}

function resetChartZoom() {
  if (!chart) return;
  chart.resetZoom();
  updateChartControls();
}

function renderChart(payload) {
  if (chart && chartPayload === payload) return;
  const savedRange = chart && chartIndexId === payload.index.id && chart.isZoomedOrPanned()
    ? { min: chart.data.labels[chart.scales.x.min], max: chart.data.labels[chart.scales.x.max] }
    : null;
  const s = payload.series;
  const labels = s.map((row) => row.date);
  const volumeData = s.map((row) => row.volume);
  const closeData = s.map((row) => row.close);
  const activeDistData = s.map((row) => (row.active && row.distribution) ? row.close : null);
  const activeStallData = s.map((row) => (row.active && row.stalling) ? row.close : null);
  const expiredData = s.map((row) => (row.pressure && !row.active) ? row.close : null);
  const ftdData = s.map((row) => row.followThrough ? row.close : null);
  const witchData = s.map((row) => (row.witching && row.pressure) ? row.close : null);

  const context = document.getElementById("marketChart").getContext("2d");
  if (chart) chart.destroy();

  chart = new Chart(context, {
    plugins: [{
      id: "visibleRangeAxes",
      afterDataLimits(currentChart, { scale }) {
        if (scale.id !== "price" && scale.id !== "volume") return;
        const scales = currentChart.options.scales;
        const min = Math.max(0, Math.ceil(scales.x.min ?? 0));
        const max = Math.min(s.length - 1, Math.floor(scales.x.max ?? s.length - 1));
        if (min === 0 && max === s.length - 1) return;
        const visible = s.slice(min, max + 1);
        if (scale.id === "volume") {
          scale.min = 0;
          scale.max = Math.max(1, ...visible.map(row => row.volume)) * 1.1;
          return;
        }
        const low = Math.min(...visible.map(row => row.close));
        const high = Math.max(...visible.map(row => row.close));
        const padding = Math.max((high - low) * 0.08, Math.abs(high) * 0.002, 0.01);
        scale.min = low - padding;
        scale.max = high + padding;
      },
    }],
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
          order: 5,
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
          order: 4,
        },
        {
          type: "line",
          label: "소멸 압박일",
          data: expiredData,
          yAxisID: "price",
          showLine: false,
          pointRadius: 3.5,
          pointHoverRadius: 6,
          pointBackgroundColor: "rgba(255,255,255,0)",
          pointBorderColor: "#9aa7ae",
          pointBorderWidth: 1.5,
          order: 3,
        },
        {
          type: "line",
          label: "만기일",
          data: witchData,
          yAxisID: "price",
          showLine: false,
          pointStyle: "rectRot",
          pointRadius: 7,
          pointHoverRadius: 9,
          pointBackgroundColor: "rgba(255,255,255,0)",
          pointBorderColor: "#8a97a0",
          pointBorderWidth: 1.5,
          order: 2,
        },
        {
          type: "line",
          label: "유효 분산일",
          data: activeDistData,
          yAxisID: "price",
          showLine: false,
          pointRadius: 4.5,
          pointHoverRadius: 7,
          pointBackgroundColor: "#c83e32",
          pointBorderColor: "#ffffff",
          pointBorderWidth: 1.5,
          order: 0,
        },
        {
          type: "line",
          label: "유효 정체일",
          data: activeStallData,
          yAxisID: "price",
          showLine: false,
          pointRadius: 4.5,
          pointHoverRadius: 7,
          pointBackgroundColor: "#d98324",
          pointBorderColor: "#ffffff",
          pointBorderWidth: 1.5,
          order: 0,
        },
        {
          type: "line",
          label: "Follow-Through Day",
          data: ftdData,
          yAxisID: "price",
          showLine: false,
          pointStyle: "triangle",
          pointRadius: 6.5,
          pointHoverRadius: 9,
          pointBackgroundColor: "#24795a",
          pointBorderColor: "#ffffff",
          pointBorderWidth: 1.5,
          order: 1,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: {
        mode: "index",
        intersect: false,
      },
      plugins: {
        zoom: {
          limits: { x: { min: 0, max: labels.length - 1, minRange: minChartRange } },
          pan: { enabled: true, mode: "x", onPan: updateChartControls },
          zoom: {
            mode: "x",
            wheel: { enabled: true, speed: 0.15 },
            pinch: { enabled: true },
            onZoom: updateChartControls,
          },
        },
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
              if (row.stalling) {
                lines.push(row.active ? "유효 정체일" : `소멸 정체일 (${row.expiredReason || "소멸"})`);
              } else if (row.distribution) {
                lines.push(row.active ? "유효 분산일" : `소멸 분산일 (${row.expiredReason || "소멸"})`);
              } else if (row.accumulation) {
                lines.push("매집일 (거래량↑ 상승)");
              }
              if (row.followThrough) lines.push("▲ Follow-Through Day (재진입 신호)");
              if (row.witching) lines.push(`만기일: ${row.witchingType}`);
              if (row.breadth) {
                lines.push(`시장 폭: ${row.breadth.advancers}↑ / ${row.breadth.decliners}↓`
                  + (row.breadth.upDownVolRatio != null ? ` · 상승/하락 거래량 ${row.breadth.upDownVolRatio}` : ""));
              }
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
  chartIndexId = payload.index.id;
  chartPayload = payload;
  if (savedRange) {
    const min = labels.findIndex(label => label >= savedRange.min);
    const last = labels.findIndex(label => label >= savedRange.max);
    const max = last < 0 ? labels.length - 1 : last;
    if (min >= 0 && max > min) chart.zoomScale("x", { min, max });
  }
  updateChartControls();
}

async function boot() {
  try {
    elements.refreshButton.addEventListener("click", refreshData);
    elements.chartZoomIn.addEventListener("click", () => zoomChart(1.3));
    elements.chartZoomOut.addEventListener("click", () => zoomChart(0.7));
    elements.chartReset.addEventListener("click", resetChartZoom);
    elements.witchingToggle.addEventListener("change", () => {
      excludeWitching = elements.witchingToggle.checked;
      if (lastPayload) render(lastPayload);
    });
    await Promise.all([loadIndexes(), loadData(activeIndex)]);
  } catch (error) {
    setStatus("오류");
    setError(error.message || "초기화 실패");
  }
}

document.addEventListener("DOMContentLoaded", boot);
