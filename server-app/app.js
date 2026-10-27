const $ = (selector) => document.querySelector(selector);
const titles = { recommendations: "추천종목", data: "데이터 수집·갱신", orders: "종목 매매", dashboard: "작업 대시보드", watchlist: "관심종목", holdings: "보유종목", analysis: "종목분석", reports: "분석 리포트", logs: "시스템 로그", settings: "설정", users: "관리자 설정" };
const state = { user: null, settings: null, view: "dashboard", analysisTab: "detail", holdingsTab: "positions", returnPeriod: "year", selectedInstrument: null, chartStartByCode: new Map(), chartEndByCode: new Map(), runPage: 1, logPage: 1, statuses: new Map(), selected: null, submitting: false, requestKey: null };
let stockPollTimer = null;
let stockViewGeneration = 0;
const vectorFactorGroups = [
  ["sector", "섹터·테마", ["반도체", "반도체 장비·소재", "방산·항공우주", "필수소비재·생필품", "재생에너지", "태양광", "풍력", "2차전지", "배터리 소재", "전기차", "로봇·자동화", "AI·소프트웨어", "클라우드·데이터센터", "전력기기·전력망", "원전", "바이오·헬스케어", "은행", "보험", "증권", "자동차", "조선", "해운", "철강", "화학", "정유", "건설", "통신", "유틸리티", "유통", "음식료", "게임·콘텐츠", "인터넷", "리츠", "반도체 소부장 기업", "반도체 전공정 기업", "반도체 패키징·후공정 기업", "가전·백색가전", "전자부품·IT하드웨어", "태양광·ESS", "수소·연료전지", "디스플레이·OLED", "공조·냉난방·냉각", "비철금속·제련", "화장품·생활용품"]],
  ["commodity", "원자재", ["금", "은", "백금", "팔라듐", "구리", "아연", "알루미늄", "니켈", "납", "주석", "철광석 IMF · 월평균", "열연강판", "리튬", "코발트", "흑연", "희토류·NdPr", "WTI", "Brent", "Dubai", "천연가스 Henry Hub", "천연가스 TTF", "LNG JKM", "석탄", "우라늄", "옥수수", "소맥", "대두", "대두유", "팜유 · 2026 이후 월평균", "설탕", "커피 Arabica · 월평균", "면화", "고무 RSS3 · 월평균", "목재", "운임 BDI", "컨테이너 운임 SCFI", "철강 생산자물가지수 · 미국 · 월간"]],
  ["market", "국가·시장", ["KOSPI", "KOSPI 200", "KOSDAQ", "KOSDAQ 150", "Nasdaq Composite", "Nasdaq-100", "S&P 500", "Dow Jones Industrial Average", "Russell 2000", "S&P/ASX 200 (호주)", "Nikkei 225", "TOPIX", "CSI 300", "Hang Seng", "TAIEX", "STOXX Europe 600", "DAX", "FTSE 100", "NIFTY 50", "MSCI World", "MSCI Emerging Markets"]],
  ["control", "환율·금리·위험", ["USD/KRW", "USD/JPY", "EUR/USD", "달러 지수 DXY", "한국 국채 3년 금리", "한국 국채 10년 금리", "미국 국채 2년 금리", "미국 국채 10년 금리", "미국 장단기 금리차", "VIX", "VKOSPI", "신용 스프레드"]],
];
const vectorDraft = { start: "2026-01-01", end: "", endMode:"rolling_yesterday", horizons: [1, 5, 21], window: "60", method: "pearson", scale: "5", selected: new Set() };
let vectorSaveQueue=Promise.resolve(), vectorStateVersion=0;
const dimensionDraft = {family: "commodity", selected: "commodity-17", start: "2026-01-01", end: "", alignment: "source", display: "raw", onlyCollected: false};
function stopStockPolling() { if (stockPollTimer) clearInterval(stockPollTimer); stockPollTimer = null; stockViewGeneration += 1; }
function requestIdentifier() {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  // LAN HTTP lacks randomUUID's secure-context API; getRandomValues remains available.
  const bytes = crypto.getRandomValues(new Uint8Array(16)); bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
  const hex = [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
}
let events;
async function restoreRememberedEmail() {
  const original = $("#loginEmail").value;
  const originallyChecked = $("#rememberEmail").checked;
  try {
    const { email } = await api("/auth/remembered-email");
    if ($("#loginEmail").value !== original || $("#rememberEmail").checked !== originallyChecked) return;
    $("#loginEmail").value = email; $("#rememberEmail").checked = Boolean(email);
    try { if (email) localStorage.setItem("nastack-trading.login-email.v1", email); else localStorage.removeItem("nastack-trading.login-email.v1"); } catch { /* Optional browser cache. */ }
  } catch {
    // The browser cache restores only an email, never a credential or authenticated state.
    try { const email = localStorage.getItem("nastack-trading.login-email.v1") || ""; if (!$("#loginEmail").value) { $("#loginEmail").value = email; $("#rememberEmail").checked = Boolean(email); } } catch { /* Optional browser cache. */ }
  }
}
// Email cache is only a device convenience; account/session/remember records persist in DB.
function cacheLoginEmail() {
  try { if ($("#rememberEmail").checked) localStorage.setItem("nastack-trading.login-email.v1", $("#loginEmail").value); else localStorage.removeItem("nastack-trading.login-email.v1"); } catch { /* DB-backed remembering still works. */ }
}
$("#rememberEmail").addEventListener("change", async () => {
  if (!$("#rememberEmail").checked) try { await api("/auth/remembered-email", { method: "DELETE" }); cacheLoginEmail(); }
  catch { $("#rememberEmail").checked = true; $("#rememberEmailMessage").textContent = "삭제하지 못했습니다. 서버 연결을 확인하고 다시 해제하세요."; }
});
restoreRememberedEmail();
async function loadSessionOptions() {
  try {
    state.sessionOptions = await api("/auth/options");
  } catch { $("#loginMessage").textContent = "세션 설정을 확인하지 못했습니다. 서버 연결을 확인하세요."; }
}

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function showToast(message) {
  $("#toast").textContent = message;
  $("#toast").classList.add("visible");
  clearTimeout(showToast.timeout);
  showToast.timeout = setTimeout(() => $("#toast").classList.remove("visible"), 3500);
}

function loggedOut() {
  stopStockPolling(); events?.close(); state.sessionEpoch=(state.sessionEpoch||0)+1;state.user = null; state.statuses.clear();
  clearTimeout(screenStateTimer);screenStateSignature="";
  state.reportFilters=null;
  state.realizedCode="";
  state.selectedInstrument=null;state.newsCode="";state.symbol="";state.dailyNews=null;state.newsModelCatalog=null;state.newsModelCatalogAt=0;state.chartStartByCode.clear();state.chartEndByCode.clear();state.analysisTab="detail";state.holdingsTab="positions";state.returnPeriod="year";
  vectorDraft.selected.clear(); vectorDraft.start = "2026-01-01"; vectorDraft.end = ""; vectorDraft.horizons = [1, 5, 21]; vectorDraft.window = "60"; vectorDraft.method = "pearson"; vectorDraft.scale = "5";
  dimensionDraft.family = "commodity"; dimensionDraft.selected = "commodity-17"; dimensionDraft.start = "2026-01-01"; dimensionDraft.end = ""; dimensionDraft.alignment = "source"; dimensionDraft.display = "raw";
  dimensionDraft.onlyCollected = false;
  $("#appShell").hidden = true; $("#liveContent").replaceChildren(); $("#liveInspector").replaceChildren();
  $("#loginGate").hidden = false; $("#loginCode").value = "";
  resetCodeStep();
  restoreRememberedEmail();
}

async function api(path, options = {}) {
  const {onUsage,...requestOptions}=options;
  const response = await fetch(`/api${path}`, { credentials: "same-origin", ...requestOptions,
    headers: { "Content-Type": "application/json", ...options.headers } });
  if (response.status === 204) return null;
  let payload; try { payload = await response.json(); } catch { payload = {}; }
  let requestUsage=null;try{requestUsage=JSON.parse(response.headers.get("X-LLM-Usage")||"null");}catch{}
  if(requestUsage){if(payload&&typeof payload==="object")payload.request_usage=requestUsage;onUsage?.(requestUsage);}
  if (!response.ok) {
    if (response.status === 401 && path !== "/auth/login") loggedOut();
    const error = new Error(typeof payload.detail === "string" ? payload.detail : `요청을 처리하지 못했습니다 (${response.status}).`); error.usage=requestUsage;error.status = response.status; error.code = response.headers.get("X-Trading-Error"); error.retryAfter=Number(response.headers.get("Retry-After"))||null; throw error;
  }
  return payload;
}

const statusLabels = { queued: "대기", running: "처리 중", completed: "완료", failed: "실패" };
function panel(title) { const section = node("section", undefined, "panel live-panel"); section.append(node("h2", title)); return section; }
function reportError(error) { showToast(error.message || "연결을 확인하세요."); }

function inspector(run) {
  state.selected = run?.id || null;
  const area = $("#liveInspector");
  area.replaceChildren(node("p", "DETAILS", "eyebrow"), node("h2", run ? "작업 상세" : "분석 안내"));
  if (!run) { area.append(node("p", "위의 화면별 가이드를 참고하세요. 항목을 선택하면 해당 결과·출처 정보를 이곳에 표시합니다.", "helper")); return; }
  area.append(node("p", `${run.instrument_snapshot?.name || run.symbol} · ${run.symbol} · ${statusLabels[run.status]}`), node("p", run.message, "helper"),
    node("p", `설정 버전 ${run.settings_version} · 시도 ${run.attempts}회`, "helper"), node("p", `요청: ${new Date(run.created_at).toLocaleString("ko-KR", { timeZone: state.user.timezone })}`, "helper"));
  if (run.result) {
    area.append(node("p", run.result.summary), node("p", run.result.simulation ? "판정: 보류 · 실제 분석 미연결" : "연구 보고서 · 매매 판단/주문 미연결", "warning"),
      node("p", `등록 출처 ${run.result.source_count}개 · 실제 수집 근거 ${run.result.evidence.length}개`, "helper"));
    if (run.result.outlook) area.append(node("p", `동향: ${{positive: "긍정", neutral: "중립", negative: "부정", uncertain: "불확실"}[run.result.outlook] || "불확실"}`));
    for (const risk of run.result.risks || []) area.append(node("p", `주의: ${risk}`, "warning"));
    const sources = node("details"); sources.open = true; sources.append(node("summary", "수집 근거·출처"));
    for (const evidence of run.result.evidence) {
      const item = node("details"); item.append(node("summary", `${evidence.id} · ${evidence.title} · ${evidence.disposition === "use" ? "사용" : "참고만"}`));
      const link = node("a", "원문 보기");
      try { const url = new URL(evidence.url); if (["https:", "http:"].includes(url.protocol)) { link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer"; } } catch { /* Invalid URL is not linked. */ }
      item.append(link, node("p", evidence.snippet), node("p", `수집: ${evidence.retrieved_at} · 발행: ${evidence.published_at || "미확인"}`, "helper")); sources.append(item);
    }
    area.append(sources);
    const detail = node("details"); detail.append(node("summary", "결과 데이터"), node("pre", JSON.stringify(run.result, null, 2), "result-json")); area.append(detail);
  }
  if (run.error_code) {
    if (run.error_code === "INSTRUMENT_CHANGED") area.append(node("p", "종목 정보가 변경되었습니다. 확인 후 새 분석을 요청하세요.", "warning"));
    if (run.error_code === "SOURCE_CATALOG_CHANGED") area.append(node("p", "분석 출처가 변경되었습니다. 설정을 확인하고 새 작업을 요청하세요.", "warning"));
    const errors = { RESEARCH_DISABLED: "서버에서 연구 연결을 활성화하세요.", TAVILY_NOT_CONFIGURED: "서버에 Tavily API 키가 필요합니다.", CLOUD_NOT_ALLOWED: "클라우드 사용이 허용되지 않았습니다.", MODEL_NOT_ALLOWED: "관리자가 허용한 클라우드 모델을 선택하세요.", OPENAI_NOT_CONFIGURED: "서버에 OpenAI API 키가 필요합니다.", VLLM_ENDPOINT_NOT_ALLOWED: "vLLM 주소가 서버의 허용 주소와 일치하지 않습니다.", MODEL_NOT_CONFIGURED: "모델 ID를 입력하세요.", SOURCE_POLICY_REQUIRED: "설정에서 분석 출처를 등록하세요.", PROVIDER_TIMEOUT: "외부 서비스 응답 시간이 초과됐습니다. 자동 클라우드 전환은 하지 않습니다.", PROVIDER_HTTP_ERROR: "외부 서비스 응답에 오류가 있습니다. 서버 연결 설정을 확인하세요.", MODEL_INVALID_REPORT: "모델 응답 형식 검증에 실패했습니다.", MODEL_INVALID_EVIDENCE: "모델이 허용되지 않은 근거를 인용했습니다.", EXTERNAL_CALL_UNKNOWN: "외부 호출 중 작업이 중단됐습니다. 중복 비용을 피하기 위해 자동 재호출하지 않습니다." };
    area.append(node("p", errors[run.error_code] || run.message, "negative"), node("p", run.error_code, "helper"));
  }
}

function pagination(area, data, key) {
  const controls = node("div", undefined, "pagination"); const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  for (const [label, delta] of [["‹ 이전", -1], ["다음 ›", 1]]) {
    const button = node("button", label); button.disabled = delta < 0 ? data.page <= 1 : data.page >= pages;
    button.addEventListener("click", () => { state[key] += delta; render().catch(reportError); }); controls.append(button);
    if (delta < 0) controls.append(node("span", `${data.page} / ${pages} · ${data.total}건`, "helper"));
  }
  area.append(controls);
}

async function renderRuns() {
  if(state.view==="reports")return renderReports();
  const data = state.view === "reports" ? await api(`/runs?page=${state.runPage}&page_size=${state.settings.value.page_size}`) : null;
  if (!["dashboard", "reports"].includes(state.view) || !state.user) return;
  const area = $("#liveContent"); area.replaceChildren();
  if (state.view === "dashboard") {
    const summary = node("div", undefined, "summary-grid"); area.append(summary);
    const holdings = panel("보유종목"); const watch = panel("관심종목"); summary.append(holdings, watch);
    const [portfolio, watchlist, kiwoom] = await Promise.all([api("/portfolio/holdings"), api("/watchlist?page=1&page_size=10"),api("/portfolio/kiwoom/holdings").catch(()=>({snapshot:null}))]);
    const extraPages=await Promise.all(Array.from({length:Math.max(0,Math.ceil(watchlist.total/10)-1)},(_,i)=>api(`/watchlist?page=${i+2}&page_size=10`)));for(const page of extraPages)watchlist.items.push(...page.items);
    if (state.view !== "dashboard") return;
    const snapshot = portfolio.snapshot;
    holdings.append(node("p", snapshot ? `${snapshot.environment === "mock" ? "모의" : "실전"} · 계좌 끝자리 ${snapshot.account_last4} · ${snapshot.total}종목` : "조회된 보유종목이 없습니다.", "helper"));
    if (snapshot) holdings.append(node("p", `마지막 조회 ${formatTime(snapshot.collected_at)}${snapshot.partial ? " · 일부만 조회됨" : ""}`, snapshot.partial ? "warning" : "helper"));
    function stockCloud(items,label){const cloud=node("div",undefined,"stock-icon-cloud");cloud.setAttribute("aria-label",label);for(const item of items){const button=node("button",undefined,"stock-orb"),shortName=item.name.replace(/^(KODEX|TIGER|ACE|SOL)\s*/,"");button.type="button";button.setAttribute("aria-label",`${item.name} · ${item.code}`);button.title=`${item.name} · ${item.code}`;button.style.setProperty("--orb-hue",String(Number(item.code)%360));const monogram=node("span",shortName.slice(0,3),"stock-orb-monogram"),tooltip=node("span",`${item.name} · ${item.code}`,"stock-orb-tooltip");tooltip.setAttribute("aria-hidden","true");button.append(monogram,tooltip);button.addEventListener("click",()=>openInstrument(item));cloud.append(button);}return cloud;}
    const held=new Map();for(const source of [snapshot,kiwoom.snapshot])for(const item of source?.items||[])held.set(item.code,{...item,market:"KRX",asset_type:"stock"});
    holdings.append(node("p",`나무 ${snapshot?.items?.length||0}종목 · 영웅문 ${kiwoom.snapshot?.items?.length||0}종목 · 중복 종목은 하나의 아이콘으로 표시`,"helper"),stockCloud([...held.values()],"보유종목 아이콘"));
    const goHoldings = node("button", "보유종목 보기", "secondary"); goHoldings.addEventListener("click", () => openView("holdings")); holdings.append(goHoldings);
    watch.append(node("p", `${watchlist.total}종목 · 내 관심 목록`, "helper"));
    watch.append(stockCloud(watchlist.items.map(entry=>entry.instrument),"관심종목 아이콘"));
    const goWatch = node("button", "관심종목 관리", "secondary"); goWatch.addEventListener("click", () => openView("watchlist")); watch.append(goWatch);
    await renderTradeMonitor(area);
    $("#liveInspector").replaceChildren(node("h2","매매 모니터링"),node("p","저장된 설정·주문 상태를 조회합니다. 주문을 실행하거나 분석을 재계산하지 않습니다.","helper"));
    return;
  }
  const section = panel(state.view === "reports" ? "작업 결과·이력" : "분석 작업");
  if (!data.items.length) section.append(node("p", "저장된 작업이 없습니다.", "helper"));
  const table = node("table"); const head = node("thead"); const tr = node("tr");
  ["종목", "상태", "진행", "요청 시각", "상세"].forEach((label) => tr.append(node("th", label))); head.append(tr); table.append(head);
  const body = node("tbody");
  for (const run of data.items) {
    const row = node("tr"); const security = node("td", run.instrument_snapshot?.name || run.symbol); if (run.instrument_snapshot?.name) security.append(node("small", run.symbol)); row.append(security);
    const status = node("td", statusLabels[run.status], run.status === "failed" ? "negative" : "");
    if (run.status === "running") status.prepend(node("span", undefined, "spinner inline-spinner")); row.append(status);
    const progress = node("td"); progress.append(node("span", `${run.progress}%`)); const bar = node("progress"); bar.value = run.progress; bar.max = 100; bar.setAttribute("aria-label", `${run.symbol} 진행률`); progress.append(bar); row.append(progress);
    row.append(node("td", new Date(run.created_at).toLocaleString("ko-KR", { timeZone: state.user.timezone })));
    const cell = node("td"); const button = node("button", "보기", "secondary"); button.addEventListener("click", () => { inspector(run); $("#rightRail").classList.remove("collapsed"); }); cell.append(button); row.append(cell); body.append(row);
    if (state.selected === run.id) inspector(run);
  }
  table.append(body); const wrap = node("div", undefined, "table-wrap"); wrap.append(table); section.append(wrap); pagination(section, data, "runPage"); area.append(section);
}

async function renderLogs() {
  const data = await api(`/logs?page=${state.logPage}&page_size=${state.settings.value.page_size}`);
  if (state.view !== "logs" || !state.user) return;
  const area = $("#liveContent"); const section = panel("운영 로그"); area.replaceChildren(section);
  await renderTradeJournal(area);
  for (const item of data.items) { const entry = node("article", undefined, "log-entry"); entry.append(node("strong", `${item.level} · ${item.message}`), node("small", item.created_at, "helper")); section.append(entry); }
  if (!data.total) section.append(node("p", "로그가 없습니다.", "helper")); pagination(section, data, "logPage");
  if (state.user.role === "admin") {
    const prune = node("button", "30일 이전 내 운영 로그 정리", "secondary");
    prune.addEventListener("click", async () => { if (!confirm("30일 이전의 내 운영 로그를 삭제할까요? 감사 기록과 작업 결과는 보존됩니다.")) return;
      try { const result = await api("/logs?older_than_days=30", { method: "DELETE" }); showToast(`${result.deleted}건 삭제`); await renderLogs(); } catch (error) { reportError(error); } }); section.append(prune);
    const auditSection = panel("감사 기록 · 최근 10건"); const audits = await api("/audit");
    if (state.view !== "logs") return;
    for (const item of audits) auditSection.append(node("p", `${item.created_at} · ${item.action}`, "helper")); area.append(auditSection);
  }
}

async function renderSettings() {
  state.settings = await api("/settings"); if (state.view !== "settings" || !state.user) return;
  const section = panel("사용자 설정"); $("#liveContent").replaceChildren(section);
  // Static markup only; server/user data is assigned via textContent/value.
  section.innerHTML = `<h2>사용자 설정</h2><form id="settingsForm" class="settings-form">
    <details open><summary>화면</summary><label>테마<select name="theme"><option value="midnight">Midnight</option><option value="slate">Slate</option></select></label>
    <label>목록 수<select name="page_size"><option value="5">5개</option><option value="10">10개</option></select></label></details>
    <details open><summary>분석 모델</summary><p class="helper">모델 선택과 실행 허용은 별도입니다. 뉴스 평가·기본 속성 생성은 각 화면의 정책을 따릅니다. 개인 OpenAI 키는 아래 ‘내 외부 서비스 연결’에 등록하세요. 개인 설정이 없을 때는 공용 서버 연결을 사용하며, 개인 연결 사용을 끄고 저장하면 해당 서비스는 사용하지 않습니다. vLLM 서버 주소는 서버가 허용한 주소만 사용할 수 있습니다.</p>
    <label>공급자<select name="provider"><option value="vllm">Win11 vLLM</option><option value="openai">OpenAI</option></select></label>
    <label>모델 ID<select name="model_id"><option value="">모델 선택</option></select></label>
    <label>서버 주소<input name="base_url" maxlength="500" placeholder="http://Win11서버:포트/v1" /></label>
    <label>클라우드 사용<select name="cloud_enabled"><option value="false">사용 안 함</option><option value="true">허용</option></select></label>
    <label>클라우드 모델<select name="cloud_model"><option value="">모델 선택</option></select></label><button type="button" id="refreshSettingsModels" class="secondary">모델 목록 새로고침</button><p id="settingsModelsMessage" class="helper" role="status"></p>
    <label id="legacyResearchTimeout">웹검색 연구 LLM 응답 대기(초)<input name="timeout_seconds" type="number" min="5" max="300" aria-label="제한 시간 (초)" /></label><p id="legacyResearchTimeoutHelp" class="helper">기본 90초, 5~300초로 조정 가능합니다. 웹검색 연구의 LLM 요청별 네트워크 대기 설정이며 전체 작업 시간이 아닙니다. 뉴스 평가·기본 속성 생성에는 적용되지 않습니다.</p></details>
    <details id="legacyResearchSources"><summary>웹검색 연구 전용 출처 정책</summary><p class="helper">기존 Tavily 웹검색 연구에만 쓰는 도메인 정책이며 뉴스 및 통계 분석의 출처 정책과는 별개입니다. ‘사용’만 LLM 판단 근거로 전달됩니다. ‘참고만’은 결과에 표시하며 ‘제외’는 제거합니다. 미등록 도메인은 참고만으로 처리합니다.</p><div id="catalogPicker"></div><div id="sourceRows"></div><button type="button" id="addSource" class="secondary">출처 추가</button></details>
    <button class="primary" type="submit">설정 저장</button><p id="settingsMessage" role="status"></p></form>`;
  const value = state.settings.value; const form = $("#settingsForm");
  state.integrations = await api("/integrations");
  if(!state.integrations.research_enabled){form.querySelector("#legacyResearchSources").remove();form.querySelector("#legacyResearchTimeout").hidden=true;form.querySelector("#legacyResearchTimeoutHelp").hidden=true;const note=node("p","뉴스·통계 출처와 사용 정책은 ‘종목분석 → 뉴스 및 통계 분석’에서 관리합니다. 비활성 웹검색 연구의 이전 설정은 표시하지 않습니다.","helper");form.querySelector('[type="submit"]').before(note);}
  const connection = node("p", `연구 ${state.integrations.research_enabled ? "활성" : "비활성"} · 공용 vLLM ${state.integrations.vllm_configured ? "등록" : "미등록"} / OpenAI ${state.integrations.openai_source==='personal'?'개인 연결':'공용 서버 연결'} ${state.integrations.openai_configured ? "등록" : "미등록"}. 등록 표시는 실제 접속 성공을 뜻하지 않습니다.`, "notice");
  section.prepend(connection);
  for (const name of ["theme", "page_size", "cloud_enabled", "cloud_model", "timeout_seconds"]) form.elements[name].value = String(value[name]);
  for (const name of ["provider", "model_id", "base_url"]) form.elements[name].value = value.analysis_model[name];
  let settingsModels=null;const modelRefresh=form.querySelector("#refreshSettingsModels"),modelNote=form.querySelector("#settingsModelsMessage");
  function optionsFor(select,items,selected){select.replaceChildren();const placeholder=node("option","모델 선택");placeholder.value="";select.append(placeholder);for(const item of items){const option=node("option",item.model);option.value=item.model;select.append(option);}if(selected&&!items.some(item=>item.model===selected)){const saved=node("option",`${selected} · 기존 설정 / 목록 확인 필요`);saved.value=selected;select.append(saved);}select.value=selected||"";}
  function drawModelChoices(selected=value.analysis_model.model_id){optionsFor(form.elements.model_id,settingsModels.items.filter(item=>item.provider===form.elements.provider.value),selected);}
  async function loadSettingsModels(refresh=false){modelRefresh.disabled=true;const selected=form.elements.model_id.value||value.analysis_model.model_id,cloud=form.elements.cloud_model.value||value.cloud_model;try{settingsModels=await api(`/models?refresh=${refresh}`);drawModelChoices(selected);optionsFor(form.elements.cloud_model,settingsModels.items.filter(item=>item.provider==="openai"),cloud);modelNote.textContent=`GPT 5.6 이상 · 계정 확인 ${settingsModels.cloud_discovered_count}개. ${settingsModels.cloud_error||settingsModels.cloud_note}${settingsModels.error?` · ${settingsModels.error}`:""} · 선택 후 ‘설정 저장’을 눌러야 적용됩니다.`;}finally{modelRefresh.disabled=state.user.role==="viewer";}}
  modelRefresh.addEventListener("click",()=>loadSettingsModels(true).catch(reportError));form.elements.provider.addEventListener("change",()=>{drawModelChoices(form.elements.provider.value==="openai"?form.elements.cloud_model.value:value.analysis_model.model_id);});form.elements.model_id.addEventListener("change",()=>{if(form.elements.provider.value==="openai")form.elements.cloud_model.value=form.elements.model_id.value;});form.elements.cloud_model.addEventListener("change",()=>{if(form.elements.provider.value==="openai")form.elements.model_id.value=form.elements.cloud_model.value;});await loadSettingsModels();
  function addSource(rule = { domain: "", disposition: "reference" }) {
    const row = node("div", undefined, "source-row"); const input = node("input"); input.value = rule.domain; input.placeholder = "example.com"; input.required = true; input.setAttribute("aria-label", "출처 도메인");
    const select = node("select"); select.setAttribute("aria-label", "출처 사용 범위");
    for (const [key, text] of [["use", "사용"], ["reference", "참고만"], ["exclude", "제외"]]) { const option = node("option", text); option.value = key; select.append(option); }
    select.value = rule.disposition; const remove = node("button", "삭제", "secondary"); remove.type = "button"; remove.addEventListener("click", () => row.remove()); row.append(input, select, remove); $("#sourceRows").append(row);
  }
  if(state.integrations.research_enabled){value.sources.forEach(addSource); $("#addSource").addEventListener("click", () => addSource());}
  let sourcePage = 1;
  async function loadCatalogChoices() {
    const area = $("#catalogPicker"); if (!area) return; area.textContent = "소스 목록 불러오는 중…";
    const data = await api(`/sources?page=${sourcePage}&page_size=5`); area.replaceChildren(node("p", "공통 소스 목록 · 추가 후 설정 저장", "helper"));
    for (const source of data.items) {
      const row = node("div", undefined, "source-row"); const description = node("div"); const link = node("a", source.name); link.href = source.url; link.target = "_blank"; link.rel = "noopener noreferrer"; description.append(link, node("p", `${source.domain} · ${source.category} · ${source.access_notes}`, "helper"));
      const policy = node("select"); policy.setAttribute("aria-label", `${source.name} 사용 범위`);
      for (const [key, text] of [["reference","참고만"],["use","사용"],["exclude","제외"]]) {const option = node("option", text); option.value = key; policy.append(option);}
      const add = node("button", "정책에 추가", "secondary"); add.type = "button"; add.disabled = source.status === "retired" || state.user.role === "viewer";
      add.addEventListener("click", () => { const existing = [...$("#sourceRows").children].find((item) => item.querySelector("input").value === source.domain); if (existing) existing.querySelector("select").value = policy.value; else addSource({domain:source.domain,disposition:policy.value}); showToast("출처 정책에 반영했습니다. 설정 저장을 눌러주세요."); });
      row.append(description, policy, add); area.append(row);
    }
    const pages = node("div", undefined, "pagination"); const previous = node("button", "‹ 이전", "secondary"); const next = node("button", "다음 ›", "secondary"); previous.type = next.type = "button"; previous.disabled = sourcePage <= 1; next.disabled = sourcePage * 5 >= data.total;
    previous.addEventListener("click", () => {sourcePage--; loadCatalogChoices().catch(reportError);}); next.addEventListener("click", () => {sourcePage++; loadCatalogChoices().catch(reportError);}); pages.append(previous,node("span",`${sourcePage} / ${Math.max(1,Math.ceil(data.total/5))} · ${data.total}건`,"helper"),next); area.append(pages);
  }
  await loadCatalogChoices();
  if (state.user.role === "viewer") for (const element of form.elements) element.disabled = true;
  form.addEventListener("submit", async (event) => {
    event.preventDefault(); const button = form.querySelector("[type=submit]"); button.disabled = true; button.textContent = "저장 중…";
    const preference = { theme: form.elements.theme.value, page_size: Number(form.elements.page_size.value), cloud_enabled: form.elements.cloud_enabled.value === "true", cloud_model: form.elements.cloud_model.value,
      news_model: value.news_model || "default", default_attributes_enabled: value.default_attributes_enabled ?? true, default_attributes_cloud_enabled: value.default_attributes_cloud_enabled ?? false, timeout_seconds: Number(form.elements.timeout_seconds.value), analysis_model: { provider: form.elements.provider.value, model_id: form.elements.model_id.value, base_url: form.elements.base_url.value },
      sources: state.integrations.research_enabled ? [...$("#sourceRows").children].map((row) => ({ domain: row.querySelector("input").value, disposition: row.querySelector("select").value })) : value.sources };
    try { state.settings = await api("/settings", { method: "PUT", body: JSON.stringify({ version: state.settings.version, value: preference }) }); document.documentElement.dataset.theme = preference.theme; $("#settingsMessage").textContent = `저장 완료 · 버전 ${state.settings.version}`; }
    catch (error) { $("#settingsMessage").textContent = error.message; } finally { button.disabled = false; button.textContent = "설정 저장"; }
  });
  await renderSchedulePlan(section);
  const brokers=panel("내 증권사 연결 · 사용자별 암호화 저장");section.append(brokers);
  await renderPersonalBrokerKeys(brokers,"namuh","나무증권 · NH PLUG");
  await renderPersonalBrokerKeys(brokers,"kiwoom","영웅문 · 키움 REST API");
  await renderPersonalConnections(section);
  const notifications=panel("내 알림·이메일 수신 목록");section.append(notifications);await renderTradingSettings(notifications);
  if(state.user.role!=="viewer"){const defaults=panel("내 기본 분석 모델·종목 속성 정책");section.append(defaults);await renderDefaultAttributesPolicy(defaults);}
  const tasks=panel("LLM 작업별 역할·프롬프트");section.append(tasks);await renderLLMTasks(tasks);
}

async function renderSchedulePlan(section) {
  const data = await api("/schedule-plan");
  const details = node("details"); details.id = "schedulePlan";
  details.append(node("summary", "정기 작업 계획"), node("p", "주기 설정은 DB에 저장됩니다. 현재 실행기는 연결되지 않아 자동 조회·분석은 시작되지 않습니다. 보유종목은 성공한 수동 조회 기록을 화면에 표시합니다.", "helper"));
  const form = node("form", undefined, "settings-form"); details.append(form); section.append(details);
  const fields = [
    ["holdings_minutes", "보유종목 조회 주기", 5, 1440],
    ["analysis_minutes", "관심종목 분석 주기", 15, 10080],
    ["news_minutes", "뉴스 확인 주기", 5, 1440],
    ["instrument_master_minutes", "공식 종목 목록 갱신 주기", 60, 10080],
  ];
  for (const [key, title, min, max] of fields) {
    const label = node("label", `${title} (분)`); const input = node("input");
    input.name = key; input.type = "number"; input.min = String(min); input.max = String(max); input.required = true; input.value = String(data.value[key]);
    label.append(input); form.append(label);
  }
  const market = node("label", "보유종목 · 정규장에만 실행"); const checkbox = node("input"); checkbox.type = "checkbox"; checkbox.name = "holdings_market_hours_only"; checkbox.checked = data.value.holdings_market_hours_only; market.append(checkbox); form.append(market);
  const save = node("button", "주기 계획 저장", "secondary"); save.type = "submit"; const message = node("p", "모든 항목: 계획만 저장 · 자동 실행 전", "helper"); message.setAttribute("role", "status"); form.append(save, message);
  if (state.user.role === "viewer") for (const element of form.elements) element.disabled = true;
  form.addEventListener("submit", async (event) => {
    event.preventDefault(); save.disabled = true; save.textContent = "저장 중…";
    const value = Object.fromEntries(fields.map(([key]) => [key, Number(form.elements[key].value)]));
    value.holdings_market_hours_only = checkbox.checked;
    try { const updated = await api("/schedule-plan", {method:"PUT", body:JSON.stringify({version:data.version, value})}); data.version = updated.version; message.textContent = `계획 저장 완료 · 버전 ${data.version} · 자동 실행 전`; }
    catch (error) { message.textContent = error.message; }
    finally { save.disabled = false; save.textContent = "주기 계획 저장"; }
  });
}

async function renderDefaultAttributesPolicy(area){
  area.classList.add("default-attributes-policy");
  const business=node("input"),businessLabel=node("label","벡터 초기화 전에 사업 설명 자동 웹조사 (Tavily·LLM 비용 및 공개정보 외부 전송)");business.type="checkbox";businessLabel.append(business);
  async function refreshSummary(attempt=0){const status=await api("/vectors/defaults-status");if(!area.isConnected)return;message.textContent=`${status.total}종목 · 기본 생성 완료 ${status.ready} · 대기 ${status.pending} · 실패·검토 필요 ${status.failed} · 사용자 속성 보존 ${status.manual_preserved}${status.failed?" · 관리자 LLM 연결 확인 후 초기화 버튼으로 재시도하세요.":""}`;if(status.pending&&attempt<20)setTimeout(()=>{if(area.isConnected)refreshSummary(attempt+1).catch(reportError);},2000);}
  area.append(node("h3","내 기본 LLM · 종목 다차원 기본 속성"),node("p","관심·보유종목 신규 등록 시 출처가 있는 사업 설명을 참고하여 기본 섹터를 제안합니다. 공식 설명이 없으면 검토 필요로 표시합니다. 클라우드 자동 호출 미허용 시 검토된 사업 주제 매핑으로 초기화합니다. 기존 사용자 저장 속성은 덮어쓰지 않습니다. 뉴스 전용 모델 선택과는 별개로 내 기본 분석 모델을 사용합니다.","helper"));const enabled=node("input"),cloud=node("input"),models=node("select"),save=node("button","기본 LLM·속성 정책 저장","primary"),refresh=node("button","기본 속성 정책 새로고침","secondary"),message=node("p","","helper");enabled.type=cloud.type="checkbox";models.setAttribute("aria-label","내 기본 LLM");const enableLabel=node("label","신규 관심·보유종목 기본 속성 자동 생성"),cloudLabel=node("label","클라우드 자동 생성 허용 (외부 전송·API 비용 발생)");enableLabel.append(enabled);cloudLabel.append(cloud);const initialize=node("button","기존 관심·보유종목 기본 속성 초기화","secondary");const verify=node("button","LLM 연결 테스트","secondary");area.append(models,enableLabel,cloudLabel,save,refresh,verify,initialize,message);verify.addEventListener("click",async()=>{const isCloud=policy?.model?.provider==="openai";if(isCloud&&!confirm("저장된 관리자 GPT 모델로 짧은 연결 테스트를 실행합니다. API 비용이 발생할 수 있습니다. 실행할까요?"))return;verify.disabled=true;message.textContent="저장된 관리자 모델 연결 테스트 중…";try{const result=await llmButtonTask(verify,"LLM 연결 테스트 중…",()=>area.isConnected,call=>call("/user/vector-defaults/verify",{method:"POST",body:JSON.stringify({cloud_confirmed:isCloud})}));message.textContent=`${result.model} · ${result.status==="verified"?"실제 응답 검증 성공":`연결 실패 · ${result.error}`}`;}catch(error){message.textContent=error.message;}finally{verify.disabled=false;}});initialize.addEventListener("click",async()=>{initialize.disabled=true;try{const result=await runDefaultInitialization(initialize,()=>area.isConnected,"/vectors/defaults-initialize");message.textContent=`${result.total}종목 확인 · 생성 대기 ${result.pending} · 기본값 유지 ${result.ready} · 사용자 속성 보존 ${result.manual_preserved}. 모델 실패 상태는 트레이딩 벡터에서 확인·재시도하세요.`;await refreshSummary();}catch(error){message.textContent=error.message;}finally{initialize.disabled=false;}});let policy;
  area.append(businessLabel,node("p","동의 시 신규 종목은 사업 설명 조사 → LLM 초기 차원 제안 순서로 처리합니다. 저장된 조사 결과는 재사용하며, 조사 실패·근거 부족 시 초기 차원을 확정하지 않습니다. 조사 실패·미완료 기록은 자동 재호출하지 않으므로 아래 사업 조사 이력을 확인하세요. 실제 상관계수·예측 성능은 별도 수치 분석으로 검증합니다.","helper"));
  async function load(){policy=await api("/user/vector-defaults");enabled.checked=policy.enabled;cloud.checked=policy.allow_cloud;business.checked=policy.research_business;models.replaceChildren();for(const model of policy.models){const option=node("option",model.label);option.value=model.id;models.append(option);}models.value=policy.model?.id||"";area.dataset.openaiKeyConfigured=String(policy.openai_key_configured);message.textContent="내 기본 모델 설정입니다. 모델 연결 실패는 종목 등록을 취소하지 않고 속성 실패 상태로 남깁니다.";save.disabled=!models.value;await refreshSummary();}
  refresh.addEventListener("click",()=>load().catch(reportError));save.addEventListener("click",async()=>{if(business.checked&&!policy.research_business&&!confirm("신규 관심·보유종목 및 명시적 초기화 대상의 공개 종목명·코드를 Tavily에 보내 검색하고, 기존 사업 설명·검색 발췌를 내 기본 LLM으로 요약한 다음 벡터 초기 차원을 제안합니다. 검색 및 모델 비용이 발생할 수 있습니다. 자동 웹조사를 허용할까요?"))return;if(cloud.checked&&!policy.allow_cloud&&!confirm("등록하는 공개 종목명·시장·유형을 내 클라우드 모델에 자동 전송하며 API 비용이 발생할 수 있습니다. 허용할까요?"))return;save.disabled=true;try{await api("/user/vector-defaults",{method:"PUT",body:JSON.stringify({version:policy.version,enabled:enabled.checked,allow_cloud:cloud.checked,research_business:business.checked,model:models.value})});await load();message.textContent="내 기본 LLM·속성 정책 저장 완료";}catch(error){message.textContent=error.message;}finally{save.disabled=!models.value;}});await load();
}

async function renderUsers() {
  await loadSessionOptions();
  const container = panel("관리자 설정"); container.id = "adminSettings"; $("#liveContent").replaceChildren(container);
  const tablist = node("div", undefined, "admin-tabs"); tablist.setAttribute("role", "tablist"); tablist.setAttribute("aria-label", "관리자 설정 영역"); container.append(tablist);
  const tabs = []; const panels = [];
  for (const [key, label] of [["users", "사용자관리"]]) {
    const tab = node("button", label, "secondary admin-tab"); tab.type = "button"; tab.id = `admin-tab-${key}`; tab.setAttribute("role", "tab"); tab.setAttribute("aria-controls", `admin-panel-${key}`);
    const content = node("div", undefined, "admin-tabpanel"); content.id = `admin-panel-${key}`; content.setAttribute("role", "tabpanel"); content.setAttribute("aria-labelledby", tab.id); content.tabIndex = 0;
    tabs.push(tab); panels.push(content); tablist.append(tab); container.append(content);
  }
  function selectTab(index, focus = false) { tabs.forEach((tab, i) => { tab.setAttribute("aria-selected", String(i === index)); tab.tabIndex = i === index ? 0 : -1; panels[i].hidden = i !== index; }); if(focus) tabs[index].focus(); }
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => selectTab(index));
    tab.addEventListener("keydown", event => { let next = index; if(event.key === "ArrowRight") next = (index + 1) % tabs.length; else if(event.key === "ArrowLeft") next = (index + tabs.length - 1) % tabs.length; else if(event.key === "Home") next = 0; else if(event.key === "End") next = tabs.length - 1; else return; event.preventDefault(); selectTab(next, true); });
  });
  selectTab(0);
  const section = panels[0];
  let policy = await api("/admin/settings");
  const defaults = node("details"); defaults.open = true;
  defaults.innerHTML = `<summary>로그인·세션 정책</summary><form id="adminPolicyForm" class="settings-form">
    <label>신규 사용자 기본 시간<input name="default_hours" aria-label="신규 사용자 기본 시간" type="number" min="1" max="168" required /></label>
    <label>허용 최대 시간<input name="max_hours" aria-label="허용 최대 시간" type="number" min="1" max="168" required /></label>
    <label>코드 유효 시간 (분)<input name="code_minutes" type="number" min="1" max="30" required /></label>
    <label>코드 최대 시도<input name="code_attempts" type="number" min="1" max="10" required /></label>
    <p class="helper">단위: 시간. 기본값 변경은 새로 추가하는 사용자에게 적용됩니다. 기존 사용자는 아래에서 개별 조정하세요. 모든 관리자 설정은 이 화면과 DB에서 관리합니다.</p>
    <button type="submit" class="primary">관리자 설정 저장</button><p id="adminPolicyMessage" role="status"></p></form>`;
  section.append(defaults);
  const policyForm = defaults.querySelector("form");
  policyForm.elements.code_minutes.closest("label").hidden=true;policyForm.elements.code_attempts.closest("label").hidden=true;
  policyForm.elements.default_hours.value = policy.default_session_hours; policyForm.elements.max_hours.value = policy.max_session_hours;
  policyForm.elements.code_minutes.value = policy.code_ttl_minutes; policyForm.elements.code_attempts.value = policy.code_max_attempts;
  policyForm.addEventListener("submit", async (event) => {
    event.preventDefault(); const button = policyForm.querySelector("button"); button.disabled = true; button.textContent = "저장 중…";
    try { policy = await api("/admin/settings", { method: "PUT", body: JSON.stringify({version: policy.version, default_session_hours: Number(policyForm.elements.default_hours.value), max_session_hours: Number(policyForm.elements.max_hours.value), code_ttl_minutes: Number(policyForm.elements.code_minutes.value), code_max_attempts: Number(policyForm.elements.code_attempts.value)}) });
      state.sessionOptions = policy; hours.value = policy.default_session_hours; hours.max = policy.max_session_hours;
      $("#adminPolicyMessage").textContent = `저장 완료 · 버전 ${policy.version}`; await list();
    } catch (error) { $("#adminPolicyMessage").textContent = error.message; }
    finally { button.disabled = false; button.textContent = "관리자 설정 저장"; }
  });
  section.append(node("h2", "사용자·권한·세션 시간"), node("p", "유효 세션은 만료되지 않은 로그인 수입니다. 기기·브라우저별로 별도 세션이며, 같은 브라우저의 여러 탭은 한 세션입니다. 브라우저를 닫아도 만료/로그아웃 전까지 유지됩니다.", "helper")); const form = node("form", undefined, "user-form");
  const email = node("input"); email.type = "email"; email.required = true; email.placeholder = "사용자 이메일"; email.setAttribute("aria-label", "추가할 이메일");
  const role = node("select"); role.setAttribute("aria-label", "추가할 사용자 권한");
  for (const [key, text] of [["trader", "일반 사용자 · 분석·매매"], ["viewer", "조회 전용"]]) { const option = node("option", text); option.value = key; role.append(option); }
  const hoursLabel = node("label", "세션 시간 (시간)", "inline-field"); const hours = node("input"); hours.type = "number"; hours.min = 1; hours.max = state.sessionOptions?.max_session_hours || 168; hours.value = state.sessionOptions?.default_session_hours || 24; hours.required = true; hours.setAttribute("aria-label", "추가할 사용자 세션 시간"); hoursLabel.append(hours);
  const submit = node("button", "사용자 추가", "primary"); submit.type = "submit"; const message = node("p", undefined, "helper"); message.setAttribute("role", "status"); form.append(email, role, hoursLabel, submit); section.append(form, message, node("p", "관리자는 세션 무제한입니다. 일반 사용자 시간 변경은 현재 유효한 세션에 저장 시점부터 즉시 적용됩니다. 만료·해지된 세션은 복원하지 않습니다.", "helper"));
  const reloadUsers = node("button", "사용자·세션 새로고침", "secondary"); reloadUsers.type = "button";
  reloadUsers.addEventListener("click", async () => {reloadUsers.disabled=true;reloadUsers.textContent="불러오는 중…";try{await list();}catch(error){reportError(error);}finally{reloadUsers.disabled=false;reloadUsers.textContent="사용자·세션 새로고침";}});
  const users = node("div"); users.id = "adminUserList"; section.append(reloadUsers, users);
  async function list() {
    const data = await api("/users"); if (state.view !== "users") return; users.replaceChildren();
    for (const user of data) {
      const row = node("div", undefined, "user-row"); row.append(node("span", `${user.email} · ${user.role} · 계정 ${user.active ? "활성" : "제한"} · 유효 세션 ${user.valid_sessions}개`));
      const durationLabel = node("label", user.role==="admin"?"세션 무제한":"세션 시간", "inline-field"); const duration = node("input"); duration.type = "number"; duration.min = 1; duration.max = state.sessionOptions?.max_session_hours || 168; duration.value = user.session_hours; duration.disabled=user.role==="admin"; duration.setAttribute("aria-label", `세션 시간 ${user.email}`); durationLabel.append(duration);
      const save = node("button", "시간 저장", "secondary");
      save.disabled=user.role==="admin";
      save.addEventListener("click", async () => {
        if (!duration.reportValidity()) return;
        save.disabled = true; save.textContent = "저장 중…";
        try { await api(`/users/${user.id}`, { method: "PATCH", body: JSON.stringify({ role: user.role, active: user.active, session_hours: Number(duration.value) }) });
          if (user.id === state.user.id) { state.user=await api("/me"); updateClock(); }
          showToast("세션 시간을 현재 유효한 세션에 바로 적용했습니다."); await list();
        } catch (error) { reportError(error); save.disabled = false; save.textContent = "시간 저장"; }
      }); row.append(durationLabel, save);
      if (user.id !== state.user.id) { const button = node("button", user.active ? "사용 제한" : "제한 해제", "secondary");
        button.addEventListener("click", async () => { button.disabled = true; try { await api(`/users/${user.id}`, { method: "PATCH", body: JSON.stringify({ role: user.role, active: !user.active }) }); await list(); } catch (error) { reportError(error); button.disabled = false; } }); row.append(button); }
      users.append(row);
    }
  }
  form.addEventListener("submit", async (event) => { event.preventDefault(); submit.disabled = true;
    try { await api("/users", { method: "POST", body: JSON.stringify({ email: email.value, role: role.value, session_hours: Number(hours.value) }) }); message.textContent = "등록 완료 · 해당 사용자는 로그인 화면에서 인증 메일을 요청할 수 있습니다."; email.value = ""; await list(); }
    catch (error) { message.textContent = error.message; } finally { submit.disabled = false; } });
  await list();
  if (state.view !== "users" || !state.user) return;
  await renderSessionHistory(section, list);
}

function formatTime(value) { return value ? new Date(value).toLocaleString("ko-KR", {timeZone: state.user.timezone}) : "기록 없음"; }
function openView(view) { stopStockPolling(); if(view==="orders"&&state.view!=="orders")$("#rightRail").classList.add("collapsed"); state.view = view; document.querySelectorAll(".nav-item[data-view]").forEach(button => button.classList.toggle("active", button.dataset.view === view)); render().catch(reportError); }
async function loadWatchMembership(){
  const owner=state.user?.id,tenant=state.user?.tenant_id;
  const valid=()=>state.user?.id===owner&&state.user?.tenant_id===tenant;
  try{
    const [watch,namuh,kiwoom]=await Promise.all([api("/watchlist?page=1&page_size=10"),api("/portfolio/holdings"),api("/portfolio/kiwoom/holdings")]);
    if(!valid())return null;
    const codes=new Set(watch.items.map(row=>row.instrument.code));
    for(let page=2;page<=Math.ceil(watch.total/10);page++){const more=await api(`/watchlist?page=${page}&page_size=10`);if(!valid())return null;for(const row of more.items)codes.add(row.instrument.code);}
    for(const portfolio of [namuh,kiwoom])for(const row of portfolio.snapshot?.items||[])if(row.quantity==null||Number(row.quantity)>0)codes.add(row.code);
    return {codes,owner,tenant,busy:new Set()};
  }catch{return null;}
}
function watchlistAddControl(item,membershipPromise,isCurrent=()=>true){
  const area=node("span",undefined,"watchlist-add-action"),owner=state.user?.id,tenant=state.user?.tenant_id;
  area.dataset.watchlistAddCode=item.code;
  const valid=()=>state.user?.id===owner&&state.user?.tenant_id===tenant&&area.isConnected&&isCurrent();
  if(state.user?.role==="viewer")return area;
  Promise.resolve(membershipPromise).then(membership=>{
    if(!valid())return;
    if(!membership){area.append(node("small","관심·보유 목록 확인 실패 · 화면을 다시 조회하세요.","helper"));return;}
    if(membership.owner!==owner||membership.tenant!==tenant||membership.codes.has(item.code))return;
    const add=node("button","관심종목 추가","secondary"),message=node("small",undefined,"helper");add.type="button";add.setAttribute("aria-label",`${item.name} 관심종목 추가`);area.append(add,message);
    add.addEventListener("click",async()=>{
      if(!valid()||membership.busy.has(item.code)||membership.codes.has(item.code)||state.user.role==="viewer")return;
      membership.busy.add(item.code);
      try{await buttonTask(add,"관심종목 저장 중…",async()=>{
        const lookup=async()=>{const result=await api(`/instruments?q=${encodeURIComponent(item.code)}&page_size=10`);if(!valid())throw new Error("화면·계정이 변경되었습니다.");return result.items.find(row=>row.code===item.code);};
        let registered=await lookup();
        if(!registered){
          const master=await api(`/instrument-search?q=${encodeURIComponent(item.code)}&page_size=10`);if(!valid())return;
          const official=master.items.find(row=>row.code===item.code);
          if(!official||!master.snapshot)throw new Error("공식 종목 목록에서 확인할 수 없습니다. 관심종목 화면에서 공식 목록을 갱신한 뒤 추가하세요.");
          try{registered=await api(`/admin/instrument-search/${item.code}?snapshot=${encodeURIComponent(master.snapshot)}`,{method:"POST"});}
          catch(error){if(error.status!==409)throw error;registered=await lookup();if(!registered)throw error;}
        }
        if(!valid())return;
        if(registered.status!=="active")throw new Error("사용 중지된 종목은 관심종목에 추가할 수 없습니다.");
        try{await api("/watchlist",{method:"POST",body:JSON.stringify({instrument_id:registered.id})});}
        catch(error){if(error.status!==409||!String(error.message).includes("이미 관심종목"))throw error;}
        if(!valid())return;
        membership.codes.add(item.code);
        for(const control of document.querySelectorAll(".watchlist-add-action"))if(control.dataset.watchlistAddCode===item.code)control.replaceChildren(node("small","관심종목에 추가됨","helper"));
      });}catch(error){if(valid())message.textContent=error.message;}
      finally{membership.busy.delete(item.code);}
    });
  });
  return area;
}
function openInstrument(item, {chartEnd = "saved"} = {}) {
  // Recommendations describe the current session, not an earlier saved chart range.
  if (chartEnd === "today") state.chartEndByCode.delete(item.code);
  state.selectedInstrument = item; state.newsCode = item.code; state.symbol = item.code;
  state.analysisTab = "detail"; openView("analysis");
  if (window.innerWidth <= 800) $("#leftRail").classList.add("collapsed");
}

function detailInspector(item, snapshot) {
  const area = $("#liveInspector"); area.replaceChildren(node("p", "DETAILS", "eyebrow"), node("h2", `${item.name} · ${item.code}`));
  area.append(node("p", `${item.market || "시장 미확인"} · ${item.asset_type === "etf" ? "ETF" : "주식"}`), node("p", item.source_note || "등록된 종목 정보", "helper"));
  area.append(node("p", snapshot ? `시세 출처: ${snapshot.source} · ${formatTime(snapshot.collected_at)}` : "시세를 조회하지 않았습니다. 표시할 가격이나 차트가 없습니다.", "helper"));
  area.append(node("p", "시세 조회는 읽기 전용이며 주문을 실행하지 않습니다.", "warning"));
}

function priceText(value, suffix = "원") { return value == null ? "—" : `${Number(value).toLocaleString("ko-KR")} ${suffix}`; }
function movementClass(value, direction) {
  if (value == null || !Number.isFinite(Number(value))) return "price-flat";
  if (Number(value) === 0 || direction === "flat") return "price-flat";
  if (direction === "up") return "price-up";
  if (direction === "down") return "price-down";
  return Number(value) > 0 ? "price-up" : "price-down";
}
function movementText(value, suffix, direction) {
  if (value == null || !Number.isFinite(Number(value))) return "—";
  const trend = movementClass(value, direction);
  const prefix = trend === "price-up" ? "+" : trend === "price-down" ? "-" : "";
  return `${prefix}${Math.abs(Number(value)).toLocaleString("ko-KR")} ${suffix}`;
}
function chart(data, code) {
  const wrap = node("div", undefined, "stock-chart");
  const available = data.filter(point => /^\d{4}-\d{2}-\d{2}$/.test(point.date) && point.close != null && point.close !== "" && Number.isFinite(Number(point.close))).sort((a,b) => a.date.localeCompare(b.date));
  const earliest = available[0]?.date || "없음", latest = available.at(-1)?.date || "없음";
  const today = new Intl.DateTimeFormat("en-CA", {timeZone: state.user?.timezone || "Asia/Seoul", year:"numeric", month:"2-digit", day:"2-digit"}).format(new Date());
  const chosenStart = state.chartStartByCode.get(code) || "2026-01-01";
  // No saved override means a rolling end date, not a frozen calendar date.
  const chosenEnd = state.chartEndByCode.get(code) || today;
  const controls = node("div", undefined, "chart-controls"); const label = node("label", "차트 시작일", "inline-field"); const start = node("input"), end = node("input");
  start.type = end.type = "date"; start.required = end.required = true; start.min = end.min = "2000-01-01"; start.max = end.max = today;
  start.value = chosenStart > today ? today : chosenStart;
  end.value = chosenEnd > today ? today : chosenEnd;
  start.setAttribute("aria-label", "차트 시작일"); label.append(start);
  const endLabel = node("label", "차트 종료일", "inline-field"); end.setAttribute("aria-label", "차트 종료일"); endLabel.append(end);
  const untilToday = node("button", "오늘까지", "secondary"); untilToday.type = "button";
  controls.append(label, endLabel, untilToday, node("span", `저장 자료 ${earliest} ~ ${latest} · ${available.length}거래일`, "helper")); wrap.append(controls);
  async function changeRange(endMode) {
    end.setCustomValidity(start.value && end.value && start.value > end.value ? "종료일은 시작일 이후로 선택하세요." : "");
    if (!start.checkValidity() || !end.checkValidity()) { (!start.checkValidity() ? start : end).reportValidity(); return; }
    state.chartStartByCode.set(code, start.value);
    if (endMode === "custom") state.chartEndByCode.set(code, end.value);
    else if (endMode === "today") state.chartEndByCode.delete(code);
    persistScreenState();
    start.disabled=end.disabled=true;const note=node("p","선택 기간의 부족한 자료를 확인하고 DB에 저장하는 중…","helper");wrap.append(note);
    let values=data;
    try{if(state.user.role!=="viewer"){const result=await api("/vectors/stock-history",{method:"POST",body:JSON.stringify({code,start:start.value,end:end.value})});values=result.chart;if(result.remaining.length)showToast(`과거 자료 미제공 ${result.remaining.length}일 · 임의 보간하지 않습니다.`);}}catch(error){showToast(error.message);}
    if(wrap.isConnected){const next=chart(values,code);if(values!==data)next.append(node("p","과거 일봉은 기존 NH 자료와 키움 ka10081 비수정 종가를 날짜별로 합쳐 표시합니다. 공급자·기업행사 보정 차이는 별도 검증이 필요합니다.","helper"));wrap.replaceWith(next);}
  }
  start.addEventListener("change", () => changeRange()); end.addEventListener("change", () => changeRange("custom"));
  untilToday.addEventListener("click", () => { end.value = today; changeRange("today"); });
  wrap.append(node("p", `선택 기간 ${start.value} ~ ${end.value}`, "helper"));
  const points = available.filter(point => point.date >= start.value && point.date <= end.value); const values = points.map(point => Number(point.close));
  if (end.value > latest) wrap.append(node("p", `종료일은 ${end.value}이며 저장된 시세는 ${latest}까지입니다. 최신 자료가 필요하면 ‘나무 시세 새로 조회’를 누르세요.`, "warning"));
  if (!points.length) { wrap.append(node("p", "선택 기간에 저장된 시세가 없습니다. 기간을 조정하거나 시세를 새로 조회하세요.", "helper")); return wrap; }
  const low = Math.min(...values), high = Math.max(...values), spread = high - low || 1;
  const step = 740 / Math.max(1, points.length - 1);
  const rangeStart = Date.parse(start.value), rangeSpan = Date.parse(end.value) - rangeStart || 86400000;
  const xAt = point => 20 + (Date.parse(point.date) - rangeStart) / rangeSpan * 740;
  const yAt = value => 135 - ((Number(value) - low) / spread) * 110;
  const coords = points.map(point => `${xAt(point)},${yAt(point.close)}`).join(" ");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"); svg.setAttribute("viewBox", "0 0 800 240"); svg.setAttribute("role", "img"); svg.setAttribute("tabindex", "0");
  svg.setAttribute("aria-label", `${points.length}거래일 종가 추이. 차트를 클릭하거나 방향키로 날짜별 종가와 거래량을 확인하세요.`);
  const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline"); line.setAttribute("points", coords); line.setAttribute("fill", "none"); line.setAttribute("stroke", "#58b4ff"); line.setAttribute("stroke-width", "3"); svg.append(line); wrap.append(svg);
  if (points.length === 1) { const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle"); dot.setAttribute("cx", String(xAt(points[0]))); dot.setAttribute("cy", String(yAt(points[0].close))); dot.setAttribute("r", "4"); dot.setAttribute("fill", "#58b4ff"); svg.append(dot); }
  const baseline = document.createElementNS("http://www.w3.org/2000/svg", "line"); for (const [key,value] of [["x1","20"],["x2","760"],["y1","170"],["y2","170"],["stroke","#658099"]]) baseline.setAttribute(key,value); svg.append(baseline);
  const differences = points.map((point, index) => point.change ?? (index ? Number(point.close) - Number(points[index - 1].close) : null));
  const maxMove = Math.max(1, ...differences.filter(value => value != null).map(value => Math.abs(Number(value))));
  const barWidth = Math.min(10, Math.max(2, step * .7));
  differences.forEach((difference, index) => { if (difference == null || !Number.isFinite(Number(difference)) || Number(difference) === 0) return;
    const height = Math.max(2, Math.abs(Number(difference)) / maxMove * 30); const bar = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    for (const [key,value] of [["x",String(xAt(points[index]) - barWidth / 2)],["y",String(Number(difference) > 0 ? 170 - height : 170)],["width",String(barWidth)],["height",String(height)],["fill",Number(difference) > 0 ? "#ff6174" : "#3ca8ff"]]) bar.setAttribute(key,value);
    svg.append(bar);
  });
  for (const [day, x, anchor] of [[start.value, 20, "start"], [end.value, 760, "end"]]) { const text = document.createElementNS("http://www.w3.org/2000/svg", "text"); for (const [key,value] of [["x",String(x)],["y","228"],["fill","#95a7be"],["font-size","12"],["text-anchor",anchor]]) text.setAttribute(key,value); text.textContent = day; svg.append(text); }
  const selection = node("p", "차트를 클릭하면 해당 날짜의 종가와 거래량을 표시합니다.", "chart-selection"); selection.setAttribute("role", "status");
  let selectedIndex = -1;
  function showPoint(index) {
    selectedIndex = Math.max(0, Math.min(points.length - 1, index)); const point = points[selectedIndex];
    svg.querySelectorAll(".chart-marker").forEach(marker => marker.remove());
    const guide = document.createElementNS("http://www.w3.org/2000/svg", "line"); guide.setAttribute("class", "chart-marker");
    for (const [key,value] of [["x1",String(xAt(point))],["x2",String(xAt(point))],["y1","18"],["y2","170"],["stroke","#a9cfff"]]) guide.setAttribute(key,value); svg.append(guide);
    const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle"); dot.setAttribute("class", "chart-marker"); dot.setAttribute("cx", String(xAt(point))); dot.setAttribute("cy", String(yAt(point.close))); dot.setAttribute("r", "5"); dot.setAttribute("fill", "#f4f8ff"); svg.append(dot);
    selection.textContent = `${point.date} · ${point.provisional ? "현재가 (장중 잠정)" : "종가"} ${priceText(point.close)} · 거래량 ${point.volume == null ? "자료 없음" : `${Number(point.volume).toLocaleString("ko-KR")}주`}`;
  }
  svg.addEventListener("click", event => { const pt = svg.createSVGPoint(); pt.x = event.clientX; pt.y = event.clientY; const local = pt.matrixTransform(svg.getScreenCTM().inverse()); let nearest = 0; for (let index = 1; index < points.length; index++) if (Math.abs(xAt(points[index]) - local.x) < Math.abs(xAt(points[nearest]) - local.x)) nearest = index; showPoint(nearest); });
  svg.addEventListener("keydown", event => { if (!["ArrowLeft", "ArrowRight", "Enter", " "].includes(event.key)) return; event.preventDefault(); showPoint(event.key === "ArrowLeft" ? (selectedIndex < 0 ? points.length - 1 : selectedIndex - 1) : event.key === "ArrowRight" ? selectedIndex + 1 : selectedIndex < 0 ? points.length - 1 : selectedIndex); });
  const totalChange = Number(points.at(-1).close) - Number(points[0].close);
  wrap.append(selection, node("p", "종가 추이(선) · 전일 대비 변화량(빨강 상승 / 파랑 하락)", "helper"), node("p", `${points[0].date} → ${points.at(-1).date} · 최저 ${priceText(low)} · 최고 ${priceText(high)} · 전체 변화 ${movementText(totalChange, "원")}`, movementClass(totalChange)));
  showPoint(points.length - 1);
  return wrap;
}

// Dimension synchronization is owned by the independent data collector.
function vectorAnalysisRequest(code,range,dimensions,options={}){
  return {code,start:range.start,end:range.end,end_mode:"latest_completed",input_policy:"lagged_available",dimensions:[...dimensions].sort(),horizons:[1,5,21],window:60,method:"pearson",levels:5,...options};
}
async function renderSavedBusiness(area,code,isCurrent=()=>true){
  const owner=state.user?.id,tenant=state.user?.tenant_id,ticket=requestIdentifier();
  area.dataset.businessRequest=ticket;
  const valid=()=>state.user?.id===owner&&state.user?.tenant_id===tenant&&area.isConnected&&area.dataset.businessRequest===ticket&&isCurrent();
  area.replaceChildren(node("h4","사업 설명"),node("p","저장된 사업설명 조회 중…","helper"));
  try{
    const research=await api(`/business-research?code=${encodeURIComponent(code)}`);
    let context=research.context;
    if(!valid())return;
    if(!context?.description){context=(await api(`/vectors/defaults/${encodeURIComponent(code)}`)).profile?.business_context;if(!valid())return;}
    area.replaceChildren(node("h4","사업 설명"),node("p",context?.description||"저장된 사업설명이 없습니다. 사업조사 완료 후 표시됩니다.","business-description-text"));
    const failure=research.items?.[0];
    if(!context?.description&&failure?.failure_message)area.append(node("p",failure.failure_message,"warning"));
    if(!context?.description&&failure?.error==="PROVIDER_HTTP_ERROR")area.append(node("p",failure.usage?.total_tokens!=null?`보고된 사용 토큰 ${failure.usage.total_tokens}`:"실패 요청의 사용량이 보고되지 않았습니다. 0토큰·무료로 간주하지 않으며 실제 청구는 제공자에서 확인하세요.","helper"));
    if(context?.note)area.append(node("p",context.note,"helper"));
    const sources=new Set([...(context?.sources||[]),...(context?.evidence||[]).map(item=>item.url)]);
    for(const url of sources){if(typeof url!=="string"||!/^https?:\/\//i.test(url))continue;const link=node("a","사업 설명 출처");link.href=url;link.target="_blank";link.rel="noopener noreferrer";area.append(link);}
  }catch(error){if(valid())area.replaceChildren(node("h4","사업 설명"),node("p",`저장된 사업설명을 조회하지 못했습니다: ${error.message}`,"helper"));}
}
function resizeBusinessDescription(field){
  field.style.height="auto";
  field.style.height=`${Math.max(100,field.scrollHeight+4)}px`;
}
function renderDetailAnalysis(box,item){
  state.newsCode=item.code;
  const owner=state.user.id,ticket=requestIdentifier();box.dataset.analysisGeneration=ticket;const valid=()=>state.user?.id===owner&&box.isConnected&&box.dataset.analysisGeneration===ticket;
  box.replaceChildren(node("h3",`${item.name} · 자동 분석 요약`));box.setAttribute("aria-live","polite");box.setAttribute("aria-busy","true");
  const businessDescription=node("section",undefined,"saved-business-description");box.append(businessDescription);
  renderSavedBusiness(businessDescription,item.code,valid);
  let initializationRetry=null;
  if(item.analysis_origin==="recommendation"&&state.user.role!=="viewer"){
    initializationRetry=node("button","추천종목 초기 분석 확인·재시도","analysis-retry-action task-busy");initializationRetry.disabled=true;initializationRetry.setAttribute("aria-busy","true");box.append(initializationRetry);
    initializationRetry.addEventListener("click",()=>{if(valid()&&confirm("완료된 사업설명·초기값은 재사용하고 이 종목의 실패·부족 단계만 다시 처리합니다. 영구 자동 실행 설정은 바꾸지 않습니다. LLM·검색 비용이 발생할 수 있습니다. 진행할까요?"))renderDetailAnalysis(box,{...item,recommendation_initialize:true,recommendation_retry:true});});
  }
  const wait=node("p","저장 결과 조회·최신성 확인 중…","detail-analysis-wait"),spin=node("span",undefined,"spinner");wait.prepend(spin);box.append(wait);
  const vector=node("div",undefined,"detail-analysis-card"),news=node("div",undefined,"detail-analysis-card");box.append(vector,news);
  const progress=(target,text)=>target.replaceChildren(node("p",text,"helper"));
  progress(vector,"트레이딩 벡터 · 저장 결과 조회 대기");progress(news,"뉴스 · 저장 평가 조회 중");
  const job={started:Date.now(),running:true};
  const initialization=item.recommendation_initialize?initializeRecommendedAnalysis(item.code,text=>{if(valid()){progress(vector,text);progress(news,"초기 차원·벡터 준비 후 뉴스·통계 수집·평가를 진행합니다.");}},valid):Promise.resolve();
  if(item.recommendation_initialize)initialization.finally(()=>{if(valid())renderSavedBusiness(businessDescription,item.code,valid);}).catch(()=>{});
  if(item.recommendation_initialize)initialization.then(profile=>{if(valid()&&profile?.execution_usage)box.append(node("p",`초기화 묶음 전체 사용량 · ${llmUsageText(profile.execution_usage)}`,"helper"));}).catch(()=>{});
  {
    const end=seoulDate(),range={start:"2026-01-01",end};
    job.vector=(async()=>{
      await initialization;if(!valid())throw new Error("화면·계정 변경");
      const timing=await api("/vectors/forecast-clock");if(!valid())throw new Error("화면·계정 변경");range.end=timing.available_cutoff;
      progress(vector,"저장 원천 최신성 확인 중…");
      const defaults=await api(`/vectors/defaults/${item.code}`),dimensions=defaults.profile?.selected_dimensions?.slice(0,20)||[];
      if(!valid())throw new Error("화면·계정 변경");
      if(!dimensions.length)throw new Error("저장된 다차원 속성이 없습니다.");
      if(item.recommendation_initialize){
        progress(vector,"키움 과거 시세의 부족 구간 확인·수집 중…");
        await api("/vectors/stock-history",{method:"POST",body:JSON.stringify({code:item.code,start:range.start,end:range.end,provider:"kiwoom"})});
        if(!valid())throw new Error("화면·계정 변경");
      }
      const request=vectorAnalysisRequest(item.code,range,dimensions);
      const saved=await api("/vectors/preview",{method:"POST",body:JSON.stringify(request)});
      if(!valid())throw new Error("화면·계정 변경");if(saved.persistence?.preview)showVectorResult(saved,true);
      const result=await api("/vectors/analyze",{method:"POST",body:JSON.stringify(request)});
      if(!valid())throw new Error("화면·계정 변경");
      result.sync_notes=result.freshness?.needs_refresh?["원천 자료 부족·갱신 지연 · 데이터 수집 화면 확인 · 저장 자료 기반 탐색 결과"]:[];
      if(item.recommendation_initialize){
        if(result.status==="completed"&&result.persistence?.id){
          await api("/vectors/attributes",{method:"POST",body:JSON.stringify({result_id:result.persistence.id,auto_initial:true})});
          if(!valid())throw new Error("화면·계정 변경");result.sync_notes.push("최초 트레이딩 벡터 할당 완료 · 기존 수동 속성은 보존");
        }else result.sync_notes.push("표본·원천 자료 부족으로 트레이딩 벡터 할당 보류 · 초기 차원은 유지");
      }
      return result;
    })();
    job.news=(async()=>{await initialization;if(!valid())throw new Error("화면·계정 변경");if(item.recommendation_initialize){await job.vector.catch(()=>{});if(!valid())throw new Error("화면·계정 변경");progress(news,"사업 기반 뉴스·공시·통계 수집·평가 중…");await refreshBusinessCollection(item.code,text=>{if(valid())progress(news,text);},Boolean(item.recommendation_retry),valid,"all");if(!valid())throw new Error("화면·계정 변경");}const workflow=await api(`/news/workflow/${item.code}`);if(!valid())throw new Error("화면·계정 변경");const result=await api(`/news/today?code=${item.code}&levels=5`);result.collection_workflow=workflow;return result;})();
    Promise.allSettled([job.vector,job.news]).then(()=>{job.finished=Date.now();job.running=false;});
  }
  const timerLabel=node("span",undefined,"detail-analysis-elapsed");timerLabel.setAttribute("aria-label","자동 분석 소요 시간");box.children[0].className="detail-analysis-heading";box.children[0].append(timerLabel);
  function updateElapsed(){const seconds=Math.max(0,Math.floor(((job.finished||Date.now())-job.started)/1000));timerLabel.textContent=`${job.finished?"소요":"경과"} ${Math.floor(seconds/60)}분 ${String(seconds%60).padStart(2,"0")}초${job.finished?"":" · 실행 중"}`;}
  updateElapsed();const elapsedTimer=setInterval(()=>{if(!box.isConnected){clearInterval(elapsedTimer);return;}updateElapsed();if(job.finished)clearInterval(elapsedTimer);},1000);
  function showVectorResult(data,pending=false){if(!valid())return;vector.replaceChildren(node("h4","예상 수익률 · Pearson"));if(pending)vector.append(node("p","저장 결과 우선 표시 · 현재 입력·최신성 확인 전입니다.","warning"));else vector.append(node("p",data.persistence?.reused?"동일 입력 · 저장 결과 재사용":"변경된 입력으로 계산 완료","helper"));const entries=[["일봉",data.horizon_results?.find(g=>g.horizon===1)?.today_comparison?.find(c=>c.method==="pearson")?.forecast],["주봉 마감",data.horizon_results?.find(g=>g.horizon===5)?.forecasts?.find(c=>c.method==="pearson")],["기간 통합",data.comparison?.find(c=>c.method==="pearson")?.forecast]];for(const [label,value] of entries){const line=node("p",`${label}: `);line.append(node("strong",value?.predicted_return==null?"추정 불가":`${(value.predicted_return*100).toFixed(3)}% · ${priceText(Math.round(value.predicted_close))}`,value?.predicted_return>0?"forecast-up":value?.predicted_return<0?"forecast-down":""));if(label==="주봉 마감"&&value)line.append(node("small",` · 기준 ${value.base_date||"미확인"} → 봉 마감 ${value.target_date||"미확인"} · 주봉 기준 수익률`));if(value?.input_note)line.append(node("small",` · 입력 벡터 ${value.input_date} · ${value.input_lag_sessions}거래일 선행 모델 · ${value.input_note}`));if(value?.reason)line.append(node("small",` · ${value.reason}`));vector.append(line);}for(const note of data.sync_notes||[])vector.append(node("small",note));if(entries.some(([,forecast])=>forecast?.predicted_return==null)){const gaps=(data.freshness?.items||[]).filter(item=>item.missing_ranges?.length);if(gaps.length)vector.append(node("p","보완할 원천 자료: "+gaps.map(item=>`${item.name||item.id} (${item.missing_ranges.map(range=>range.join(" ~ ")).join(", ")})`).join(" · "),"warning"));}vector.append(node("small",`기준 일별 종가 ${priceText(data.comparison?.[0]?.forecast?.base_close)} · ${data.comparison?.[0]?.forecast?.base_date||"미확인"} → 예측 대상 ${data.comparison?.[0]?.forecast?.target_date||"미확인"} · 탐색 추정이며 수익 보장 아님`));}
  job.vector.then(data=>showVectorResult(data)).catch(error=>{if(valid())progress(vector,`벡터 분석 상태: ${error.message}`);});
  job.news.then(data=>{if(!valid())return;state.dailyNews=data;news.replaceChildren(node("h4","뉴스 영향 평가 합산"),node("strong",data.total_sum==null?"판단 불가 / 미평가":`${data.total_sum>0?"+":""}${data.total_sum}점`,data.total_sum>0?"forecast-up":data.total_sum<0?"forecast-down":""),node("p",`5단계 · 평가 ${data.material_counts?.reviewed||0}건 · 점수 산정 ${data.material_counts?.scored||0}건 · 대기 ${Math.max(0,(data.analysis_pool_count||0)-(data.material_counts?.reviewed||0))}건`,"helper"));news.append(collectionWorkflowView(data.collection_workflow,item.code));}).catch(async error=>{if(!valid())return;progress(news,`뉴스 자동 평가 상태: ${error.message}`);try{const saved=await api(`/news/today?code=${item.code}&levels=5`);if(valid())news.append(node("p",`저장 평가 합산: ${saved.total_sum==null?"미평가":saved.total_sum+"점"}`));}catch{}});
  Promise.allSettled([job.vector,job.news]).then(()=>{clearInterval(elapsedTimer);if(valid()){updateElapsed();wait.textContent="저장 결과·최신성 확인 완료 · 부족·지연 여부는 각 결과에서 확인하세요.";box.setAttribute("aria-busy","false");if(initializationRetry){initializationRetry.disabled=false;initializationRetry.classList.remove("task-busy");initializationRetry.setAttribute("aria-busy","false");}}});
}

async function renderStockDetail(area) {
  const section = panel("종목 상세"); area.append(section);
  let item = state.selectedInstrument;
  if (!item) {
    const watch = await api("/watchlist?page=1&page_size=5");
    section.append(node("p", "관심종목에서 종목 보기를 누르면 이곳에 상세 정보와 차트가 표시됩니다.", "helper"));
    for (const row of watch.items) { const button = node("button", `${row.instrument.name} · ${row.instrument.code}`, "secondary"); button.addEventListener("click", () => openInstrument(row.instrument)); section.append(button); }
    return;
  }
  section.append(node("p", `${item.name} · ${item.code} · ${item.market || "시장 미확인"}`, "stock-heading"));
  section.append(watchlistAddControl(item,loadWatchMembership()));
  const status = node("p", "저장된 시세 확인 중…", "helper"); status.setAttribute("role", "status"); section.append(status);
  const autoState = node("p", "정규장 여부 확인 중…", "helper"); autoState.setAttribute("role", "status"); section.append(autoState);
  const output = node("div"); section.append(output);
  const analysisSummary=node("section",undefined,"detail-analysis-summary");analysisSummary.setAttribute("aria-label","선택 종목 자동 분석 진행 및 결과");
  output.append(analysisSummary);
  const refresh = node("button", "나무 시세 새로 조회", "secondary"); refresh.disabled = state.user.role === "viewer"; section.append(refresh);
  const generation = stockViewGeneration;
  const valid = () => generation === stockViewGeneration && state.view === "analysis" && state.analysisTab === "detail" && state.selectedInstrument?.code === item.code;
  let currentSnapshot = null, inflight = false, lastFailedAutoAt = 0, marketStatus = null, marketStatusCheckedAt = 0;
  function display(snapshot) {
    currentSnapshot = snapshot;
    output.replaceChildren();
    status.textContent = snapshot ? `${snapshot.environment === "mock" ? "모의" : "실전"} · NH PLUG · 마지막 조회 ${formatTime(snapshot.collected_at)} (실시간 스트림 아님)` : "조회된 시세가 없습니다. 나무 연결을 확인하고 새로 조회하세요.";
    if (snapshot) {
      const cards = node("div", undefined, "price-grid");
      const direction = snapshot.direction || (snapshot.change_rate < 0 ? "down" : snapshot.change_rate > 0 ? "up" : "flat");
      for (const [label, value, suffix, moving] of [["현재가", snapshot.price, "원", false], ["전일 대비", snapshot.change, "원", true], ["등락률", snapshot.change_rate, "%", true]]) { const card = node("div", undefined, "metric-card"); card.append(node("small", label), node("strong", moving ? movementText(value, suffix, direction) : priceText(value, suffix), moving ? movementClass(value, direction) : "")); cards.append(card); }
      output.append(cards);
      if (snapshot.quote_note) output.append(node("p", snapshot.quote_note, "warning"));
      if (snapshot.reference_close != null) output.append(node("p", `등락 기준 전일종가 ${priceText(snapshot.reference_close)} · 차트 마지막 날짜 ${snapshot.chart?.at(-1)?.date || "없음"}`, "helper"));
      output.append(analysisSummary,chart(snapshot.chart || [], item.code));
    }else output.append(analysisSummary,chart([],item.code));
    detailInspector(item, snapshot);
  }
  async function loadQuote(auto = false, quoteOnly = false) {
    if (inflight || !valid()) return;
    inflight = true; refresh.disabled = true; refresh.textContent = auto ? "자동 시세 갱신 중…" : "시세 조회 중…";
    if (!auto) status.textContent = "나무증권에서 현재가와 일별 시세를 가져오는 중입니다…";
    try {
      const path = `/market/instruments/${item.code}/refresh${auto ? `?auto=true&quote_only=${quoteOnly}` : ""}`;
      let data;
      try { data = await api(path, {method:"POST"}); }
      catch (error) {
        if (error.code !== "NAMUH_AUTH_REQUIRED" || state.user?.role === "viewer" || !valid()) throw error;
        status.textContent = "나무증권 인증을 갱신하고 최신 시세를 다시 조회합니다…";
        const policy = await api("/user/brokers/namuh");
        if (!valid()) return;
        if (!policy.enabled || !policy.key_owner_matches || policy.missing_settings?.length) throw error;
        const verified = await api("/user/brokers/namuh/verify", {method:"POST", body:JSON.stringify({version:policy.version})});
        if (!valid()) return;
        if (verified.last_probe?.status !== "completed" || verified.last_probe?.policy_version !== policy.version) throw new Error(verified.last_probe?.message || "나무증권 인증 갱신에 실패했습니다.");
        data = await api(path, {method:"POST"});
      }
      if (valid()) { display(data.snapshot); if (auto) autoState.textContent = "정규장 자동 갱신 · 현재가/당일 차트 약 3초 · 과거 일별 차트 약 60초"; }
    }
    catch (error) { if (valid()) { lastFailedAutoAt = Date.now(); if (auto) { if (error.status === 409) marketStatusCheckedAt = 0; autoState.textContent = `자동 갱신 실패: ${error.message} · 30초 뒤 다시 시도`; } else status.textContent = `최신 시세 조회 실패: ${error.message} · 이전 저장 자료를 표시합니다.`; } }
    finally { inflight = false; if (valid()) { refresh.disabled = state.user.role === "viewer"; refresh.textContent = "나무 시세 새로 조회"; } }
  }
  refresh.addEventListener("click", () => loadQuote(false));
  renderDetailAnalysis(analysisSummary,item);
  try { const data = await api(`/market/instruments/${item.code}`); if (!valid()) return; item = data.instrument; display(data.snapshot); }
  catch (error) { status.textContent = error.message; detailInspector(item, null); }
  if (!valid()) return;
  if (state.user.role !== "viewer") await loadQuote(false);
  else autoState.textContent = "조회 전용 계정 · 저장된 시세를 표시합니다.";
  async function autoTick() {
    if (!valid() || document.hidden || inflight || state.user.role === "viewer") return;
    try {
      const statusAge = Date.now() - marketStatusCheckedAt;
      const wasOpen = marketStatus?.is_open;
      if (!marketStatus || statusAge >= (marketStatus.is_open ? 30_000 : 60_000)) { marketStatus = await api("/market/session"); marketStatusCheckedAt = Date.now(); }
      if (!valid()) return;
      // Replace the last intraday provisional bar with the provider's daily close once.
      if (wasOpen && !marketStatus.is_open) await loadQuote(false);
      if (!valid()) return;
      if (!marketStatus.is_open) { autoState.textContent = `주기적 자동 갱신 중지 · ${marketStatus.reason}. 화면 진입 시 1회 조회하며 수동 새로 조회도 가능합니다.`; return; }
      autoState.textContent = "정규장 자동 갱신 · 현재가/당일 차트 약 3초 · 과거 일별 차트 약 60초";
      if (Date.now() - lastFailedAutoAt < 30_000) return;
      const quoteAge = currentSnapshot ? Date.now() - new Date(currentSnapshot.collected_at).getTime() : Infinity;
      if (Number.isFinite(quoteAge) && quoteAge < 2900) return;
      const chartAge = currentSnapshot?.chart_fetched_at ? Date.now() - new Date(currentSnapshot.chart_fetched_at).getTime() : Infinity;
      await loadQuote(true, Number.isFinite(chartAge) && chartAge < 60_000);
    } catch (error) { if (valid()) autoState.textContent = `장 상태 확인 실패: ${error.message} · 자동 시세 조회는 중지했습니다.`; }
  }
  if (!valid()) return;
  stockPollTimer = setInterval(() => autoTick().catch(reportError), 3_000);
  await autoTick();
}

function dimensionChart(points, dimension, normalized) {
  const wrap = node("div", undefined, "dimension-chart");
  const finite = points.filter(point => point.value != null && Number.isFinite(point.value));
  if (!finite.length) { wrap.append(node("p", "선택 기간에 표시할 관측값이 없습니다. 수집 상태와 원천 범위를 확인하세요.", "helper")); return wrap; }
  const low = Math.min(...finite.map(point => point.value)), high = Math.max(...finite.map(point => point.value));
  const transformed = value => normalized ? (high === low ? 0.5 : (value - low) / (high - low)) : value;
  const minimum = normalized ? 0 : low, maximum = normalized ? 1 : high, span = maximum - minimum || 1;
  const firstDate = Date.parse(points[0].date), lastDate = Date.parse(points.at(-1).date), duration = lastDate - firstDate || 86400000;
  const xAt = day => 85 + (Date.parse(day) - firstDate) / duration * 680;
  const yAt = value => 185 - (transformed(value) - minimum) / span * 150;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"); svg.setAttribute("viewBox", "0 0 800 240"); svg.setAttribute("role", "img"); svg.setAttribute("tabindex", "0");
  svg.setAttribute("aria-label", `${dimension.name} 일별 관측 ${finite.length}건. 클릭하거나 좌우 방향키로 날짜와 값을 확인하세요.`);
  function element(tag, attrs, text) { const el = document.createElementNS("http://www.w3.org/2000/svg", tag); for (const [key,value] of Object.entries(attrs)) el.setAttribute(key, String(value)); if (text !== undefined) el.textContent = text; svg.append(el); return el; }
  for (let tick = 0; tick < 4; tick++) { const value = minimum + span * tick / 3, y = 185 - tick / 3 * 150; element("line", {x1:85,x2:765,y1:y,y2:y,stroke:"var(--line)"}); element("text", {x:78,y:y+4,"text-anchor":"end",fill:"var(--muted)","font-size":11}, value.toLocaleString("ko-KR", {maximumFractionDigits:3})); }
  const segments = []; let segment = [];
  for (const point of points) { if (point.value == null) { if (segment.length) segments.push(segment); segment = []; } else segment.push(point); }
  if (segment.length) segments.push(segment);
  for (const part of segments) {
    element("polyline", {points:part.map(point => `${xAt(point.date)},${yAt(point.value)}`).join(" "),fill:"none",stroke:"var(--blue)","stroke-width":2.5});
    if (part.length === 1) element("circle", {cx:xAt(part[0].date),cy:yAt(part[0].value),r:3,fill:"var(--blue)"});
  }
  element("text", {x:85,y:215,fill:"var(--muted)","font-size":11}, points[0].date);
  element("text", {x:765,y:215,"text-anchor":"end",fill:"var(--muted)","font-size":11}, points.at(-1).date);
  const detail = node("p", undefined, "chart-selection"); detail.setAttribute("role", "status"); let selected = finite.length - 1;
  function select(index) { selected = Math.max(0, Math.min(finite.length - 1,index)); const point = finite[selected]; svg.querySelectorAll(".dimension-marker").forEach(el => el.remove()); element("line", {class:"dimension-marker",x1:xAt(point.date),x2:xAt(point.date),y1:30,y2:190,stroke:"var(--muted)"}); element("circle", {class:"dimension-marker",cx:xAt(point.date),cy:yAt(point.value),r:4,fill:"var(--text)"}); detail.textContent = `${point.date} · ${point.value.toLocaleString("ko-KR", {maximumFractionDigits:5})} ${dimension.unit}${normalized ? ` · 표시값 ${transformed(point.value).toFixed(3)}` : ""}`; }
  svg.addEventListener("keydown", event => { if (["ArrowLeft","ArrowRight","Home","End"].includes(event.key)) { event.preventDefault(); select(event.key === "Home" ? 0 : event.key === "End" ? finite.length-1 : selected + (event.key === "ArrowLeft" ? -1 : 1)); } });
  svg.addEventListener("click", event => { const p = svg.createSVGPoint(); p.x = event.clientX; p.y = event.clientY; const local = p.matrixTransform(svg.getScreenCTM().inverse()); let nearest = 0; for (let i=1;i<finite.length;i++) if (Math.abs(xAt(finite[i].date)-local.x)<Math.abs(xAt(finite[nearest].date)-local.x)) nearest=i; select(nearest); });
  wrap.append(svg,detail,node("p", normalized ? "선택 구간의 최소·최대 기준 0~1 시각화입니다. 상수 계열은 0.5에 표시하며 학습·상관 분석에는 사용하지 않습니다." : `원자료 단위: ${dimension.unit} · 빈 관측값에서는 선을 끊습니다.`, "helper")); select(selected); return wrap;
}

// Explicit async actions only: never intercept order/approval button events globally.
async function buttonTask(button,label,task){
  if(button.dataset.working==="true")return;
  const owner=state.user?.id,disabled=button.disabled,text=button.textContent;
  button.dataset.working="true";button.disabled=true;button.textContent=label;
  button.classList.add("task-busy");button.setAttribute("aria-busy","true");
  try{return await task();}finally{
    button.classList.remove("task-busy");button.setAttribute("aria-busy","false");
    delete button.dataset.working;button.textContent=text;
    button.disabled=disabled||state.user?.id!==owner;
  }
}

async function renderDimensionData(area) {
  const owner=state.user.id;
  const section = panel("다차원 데이터 · 원천 수집 점검"); section.id = "dimensionData"; area.append(section);
  const status = node("p", "활성 후보와 수집 기록을 확인하고 있습니다…", "helper task-busy"); status.setAttribute("role","status"); section.append(status);
  const valid = () => section.isConnected && state.user?.id===owner && (state.view === "data" || (state.view === "analysis" && state.analysisTab === "dimensions"));
  const catalog = await api("/dimensions"); if (!valid()) return;
  const availability = {collectable:"수집 경로 연결",license_required:"이용권 대기",definition_pending:"정의·접근 확인 필요"};
  const implementationLabel=item=>item.implementation_label || availability[item.assessment];
  status.className="helper";status.textContent = `${catalog.total}개 후보 점검 · 수집 경로 연결 ${catalog.collectable}개 · 내 수집 성공 ${catalog.collected}개`;
  section.append(node("p", "각 항목의 원천 주기와 실제 수집 성공을 구분합니다. 일별 자료만 한국 거래일과 대조합니다. 월간 자료는 원래의 월간 가격·지수로 표시하며 일별 복제·일별 벡터 입력은 하지 않습니다.", "helper"));
  const controls = node("div", undefined, "vector-controls"); section.append(controls);
  function combo(label) { const field = node("label", label), select = node("select"); select.setAttribute("aria-label",label); field.append(select); controls.append(field); return select; }
  const family = combo("차원 유형"), choice = combo("다차원 데이터 선택");
  for (const [key,label] of [["all","전체"],...vectorFactorGroups.map(([key,label])=>[key,label])]) { const option=node("option",label); option.value=key; family.append(option); }
  family.value = dimensionDraft.family;
  const today = new Intl.DateTimeFormat("en-CA", {timeZone:state.user.timezone || "Asia/Seoul",year:"numeric",month:"2-digit",day:"2-digit"}).format(new Date()); if (!dimensionDraft.end) dimensionDraft.end=new Date(Date.parse(`${today}T00:00:00Z`)-86400000).toISOString().slice(0,10);
  const start=node("input"), end=node("input");
  for (const [input,key,label] of [[start,"start","차원 조회 시작일"],[end,"end","차원 조회 종료일"]]) { input.type="date";input.min="2000-01-01";input.max=today;input.required=true;input.value=dimensionDraft[key];input.setAttribute("aria-label",label);const field=node("label",label);field.append(input);controls.append(field); }
  const alignment=combo("거래일 표시"), display=combo("그래프 값");
  for(const [value,label] of [["source","원천 관측일"],["krx","한국 거래일 날짜대응"]]){const option=node("option",label);option.value=value;alignment.append(option);} alignment.value=dimensionDraft.alignment;
  for(const [value,label] of [["raw","원자료"],["normalized","0~1 시각화"]]){const option=node("option",label);option.value=value;display.append(option);} display.value=dimensionDraft.display;
  const actions=node("div",undefined,"source-row"),read=node("button","저장 자료 조회","secondary"),refresh=node("button","수동 수집","primary"); read.type=refresh.type="button";refresh.title="선택한 지표·기간의 부족한 자료를 수동 수집합니다. 일일 수집 한도는 유지됩니다.";actions.append(read,refresh);section.append(actions);
  const manualResult=node("p","선택한 지표·기간을 수동 수집합니다. LLM 미사용 · 토큰 0개. 외부 API 비용은 제공자 정책에 따라 별도입니다.","helper");manualResult.setAttribute("role","status");manualResult.setAttribute("aria-live","polite");section.append(manualResult);
  const filterLabel=node("label",undefined,"vector-check"),onlyCollected=node("input");onlyCollected.type="checkbox";onlyCollected.checked=dimensionDraft.onlyCollected;filterLabel.append(onlyCollected,node("span","수집된 자료만 보기"));section.append(filterLabel,node("p","선택 목록의 ‘미수집·0건’은 전체 저장 기록 기준입니다. 수집된 항목도 조회 기간에 따라 0건일 수 있으며, 선택하면 해당 기간의 건수를 표시합니다.","helper"));
  const result=panel("원자료 추이와 거래일 대응");result.id="dimensionSeries";area.append(result);const output=node("div");result.append(output);
  const overview=panel(`${catalog.total}개 활성 차원 · 수집 가능성 목록 · 제외 ${catalog.excluded || 0}개`);area.append(overview);const details=node("details");details.append(node("summary","전체 점검표 펼치기 · 문서 점검과 실수집 상태"));overview.append(details);
  const table=node("table"),head=node("thead"),tr=node("tr");for(const label of ["유형·차원","일별 제공·연결 상태","계열·출처","내 수집 상태"])tr.append(node("th",label));head.append(tr);table.append(head);const body=node("tbody");table.append(body);const tableWrap=node("div",undefined,"table-wrap");tableWrap.append(table);details.append(tableWrap);
  function overviewRows(){body.replaceChildren();for(const item of catalog.items){const row=node("tr");row.append(node("td",`${item.group_name} · ${item.name}`),node("td",`${implementationLabel(item)} · ${item.daily_feasibility}`),node("td",`${item.series_code || "코드 미확정"} · ${item.source_name}`),node("td",item.last_attempt_status === "failed" ? `최근 실패${item.last_success_at ? " · 이전 성공 있음" : ""}` : item.last_success_at ? `성공 · ${formatTime(item.last_success_at)}` : "미수집"));body.append(row);}}overviewRows();
  let currentData=null,sequence=0,busy=false;
  function selected(){return catalog.items.find(item=>item.id===choice.value);}
  function populate(){choice.replaceChildren();const items=catalog.items.filter(item=>(family.value==="all"||item.family===family.value)&&(!onlyCollected.checked||item.last_success_at||item.source_changed||item.connection_verified));const priority=items.find(item=>item.id==='control-11'&&item.connection_verified);if(priority){const option=node('option',`${priority.name} · 연결 확인 · ${priority.last_success_at?'저장 자료 있음':'미수집·0건'}`);option.value=priority.id;choice.append(option);}for(const stored of [true,false]){const members=items.filter(item=>item!==priority&&Boolean(item.last_success_at)===stored);if(!members.length)continue;const group=node("optgroup");group.label=`${stored?"저장 자료 있음":"미수집 · 0건"} (${members.length}개)`;for(const item of members){const option=node("option",`${item.name} · ${item.source_changed?"출처 전환 · 재수집 필요":stored?"저장 자료 있음":"미수집·0건"} · ${implementationLabel(item)}`);option.value=item.id;group.append(option);}choice.append(group);}if([...choice.options].some(option=>option.value===dimensionDraft.selected))choice.value=dimensionDraft.selected;choice.disabled=!items.length;if(!items.length){const option=node("option","이 유형에 수집된 자료가 없습니다");option.value="";choice.append(option);}dimensionDraft.selected=choice.value;}
  function datesOK(){return start.checkValidity()&&end.checkValidity()&&start.value<=end.value&&(Date.parse(end.value)-Date.parse(start.value))/86400000<=1826;}
  function buttons(){read.disabled=busy||!datesOK()||!selected();refresh.disabled=busy||!datesOK()||!selected()?.can_collect||state.user.role==="viewer";for(const input of [family,choice,start,end,alignment,display,onlyCollected])input.disabled=busy||(input===alignment&&selected()?.representation==="monthly_benchmark")||(input===choice&&!selected());}
  function sourceInfo(item,data){const rail=$("#liveInspector");rail.replaceChildren(node("p","DIMENSION DATA","eyebrow"),node("h2",item.name),node("p",item.source_name),node("p",`계열: ${item.series_code||"미확정"} · ${item.frequency} · ${item.unit}`,"helper"),node("p",item.access_method,"helper"),node("p",item.license,"helper"),node("p",item.reason,"helper"));if(item.source_url){const link=node("a","공식 출처·계열 설명");link.href=item.source_url;link.target="_blank";link.rel="noopener noreferrer";rail.append(link);}rail.append(node("p",data?.last_success_at?`마지막 수집: ${formatTime(data.last_success_at)}`:"수집 기록 없음","helper"),node("p",`정의·접근 점검일: ${item.assessment_date}. 실제 수집 상태는 별도 표시합니다.`,"helper"),node("p","관측일의 날짜 일치는 당시 이용 가능 시각의 검증이 아닙니다. 해외 시차·공개 지연을 확인한 뒤 벡터 분석에 사용할 수 있습니다.","warning"));}
  function show(data){currentData=data;const item=data.dimension;sourceInfo(item,data);if(data.retrieval_providers?.includes("kiwoom_domestic_history"))$("#liveInspector").append(node("p","최근 NH 250거래일 이전 구간은 키움 ka10081 비수정 종가로 소급 수집했습니다. 공급자 간 가격·기업행사 보정 차이는 별도 검증이 필요합니다.","warning"));output.replaceChildren();const c=data.coverage,monthly=item.representation==="monthly_benchmark";alignment.disabled=monthly;
    const option=[...choice.options].find(entry=>entry.value===item.id);if(option)option.textContent=`${item.name} · ${c.source_observations}건(선택 기간)${!item.last_success_at&&!data.last_success_at?" · 미수집":""} · ${implementationLabel(item)}`;
    output.append(node("h3",`${item.series_label || item.name} · ${data.start} ~ ${data.end}`));
    if(data.sync)output.append(node("p",data.sync.mode === "cached"?"DB 저장 자료 재사용 · 외부 API 호출 없음":`새 구간만 수집·DB 저장: ${data.sync.fetched.join(" ~ ")}${data.sync.remaining.length?` · 미조회 구간 ${data.sync.remaining.length}개 남음`:""}`,"helper"));
    if(item.representation === "etf_proxy") output.append(node("p",`ETF 대체 지표: ${item.instrument_name} (${item.series_code}). ${item.proxy_definition || "원지수·원자재 현물 가격이 아닌 ETF 가격"} · ${item.unit}. 수정주가·분배금·환율·환헤지·선물 롤오버 영향은 별도 검증이 필요합니다.`,"warning"));
    if(item.issuer_url) { const issuer = node("a","ETF 발행사·상품 근거"); issuer.href=item.issuer_url; issuer.target="_blank"; issuer.rel="noopener noreferrer"; output.append(issuer); }
    if(item.representation === "industry_proxy") output.append(node("p",`업종 대체 지표: ${item.instrument_name} (${item.series_code}). 분석 주제 전체와 동일한 지수가 아니므로 구성 범위를 확인하세요.`,"warning"));
    const metrics=node("div",undefined,"price-grid");for(const [label,value]of [["원천 관측",`${c.source_observations}건`],["KRX 날짜 대응",monthly?"월간 자료 · 적용 안 함":`${c.matched} / ${c.krx_sessions}일`],["값 없는 KRX일",monthly?"적용 안 함":`${c.missing}일`]]){const card=node("div",undefined,"metric-card");card.append(node("small",label),node("strong",value));metrics.append(card);}output.append(metrics);
    output.append(node("p",data.last_success_at?`원천 범위 ${c.first||"없음"} ~ ${c.last||"없음"} · 수집 ${formatTime(data.last_success_at)}`:"선택 기간의 수집 기록이 없습니다.","helper"));
    if(item.source_changed)output.append(node("p","공식 원천을 변경했습니다. 선택한 과거 기간을 수동 수집하면 새 출처로 학습 자료를 구성합니다. 이전 출처의 기록은 보존하지만 서로 다른 기준의 자료를 섞지 않습니다.","warning"));
    if(data.last_attempt?.status==="failed")output.append(node("p",data.last_attempt.error_code==="SOURCE_NOT_PUBLISHED"?"원천 발표 대기 중입니다. 연결 오류가 아니며 이전 자료를 보존합니다.":"최근 수집이 실패했습니다. 아래는 남아 있는 이전 성공 자료입니다.","warning"));
    if(!item.can_collect)output.append(node("p",`${implementationLabel(item)}: ${item.reason}`,"warning"));
    const values=!monthly&&alignment.value==="krx"?data.krx_points.concat(data.missing.map(point=>({date:point.date,value:null}))).sort((a,b)=>a.date.localeCompare(b.date)):data.points;
    output.append(dimensionChart(values,item,display.value==="normalized"),node("p",data.calendar.note,"warning"),node("p",`거래일 달력: ${data.calendar.name} · ${data.calendar.provider} ${data.calendar.version}. 국가별 휴장과 공개 지연으로 날짜 대응률이 100%가 아닐 수 있습니다.`,"helper"));
    const missing=node("details");missing.append(node("summary",`값 없는 한국 거래일 ${c.missing}개 확인`));const list=node("div",undefined,"dimension-missing");for(const point of data.missing)list.append(node("p",`${point.date} · ${point.reason}`,"helper"));missing.append(list);output.append(missing);
    const rows=node("details");rows.append(node("summary",`원천 ${monthly?"월별":"일별"} 값 ${data.points.length}행 확인`));const raw=node("div",undefined,"dimension-missing");for(const point of data.points)raw.append(node("p",`${point.date} · ${point.value==null?"결측":point.value} ${item.unit}`,"helper"));rows.append(raw);output.append(rows);
  }
  async function load(collect=false){const token=++sequence;currentData=null;busy=false;buttons();const item=selected();if(!item){output.replaceChildren(node("p","이 유형에 수집된 자료가 없습니다. ‘수집된 자료만 보기’를 해제하면 미수집 후보와 출처를 확인할 수 있습니다.","helper"));$("#liveInspector").replaceChildren(node("p","DIMENSION DATA","eyebrow"),node("h2","수집 자료 없음"));return;}sourceInfo(item,null);if(!datesOK()){output.replaceChildren(node("p","유효한 시작·종료일과 최대 5년 범위를 확인하세요.","warning"));return;}busy=true;buttons();output.replaceChildren(node("p",collect?"공식 원자료 수집 중…":"저장된 원자료 조회 중…","helper"));
    const began=performance.now(),range=`${item.name} · ${start.value} ~ ${end.value}`;
    if(collect)manualResult.textContent=`수동 수집 중 · ${range} · LLM 미사용 · 토큰 0개`;
    try{const data=await api(`/dimensions/${encodeURIComponent(item.id)}/${collect?"collect":"series"}${collect?"":`?start=${start.value}&end=${end.value}`}`,collect?{method:"POST",body:JSON.stringify({start:start.value,end:end.value})}:{});if(!valid()||token!==sequence)return;show(data);if(collect){manualResult.textContent=`수동 수집 완료 · ${range} · ${data.sync?.mode==="cached"?"저장 자료 재사용 · 외부 API 호출 없음":"원천 수집 처리 완료"} · 소요 ${((performance.now()-began)/1000).toFixed(1)}초 · LLM 사용 토큰 0개 (LLM 미사용)`;const had=catalog.items.find(row=>row.id===item.id);if(!had.last_success_at)catalog.collected++;had.last_success_at=data.last_success_at;had.last_attempt_status="completed";overviewRows();status.textContent=`${catalog.total}개 후보 점검 · 수집 경로 연결 ${catalog.collectable}개 · 내 수집 성공 ${catalog.collected}개`;}}
    catch(error){if(valid()&&token===sequence){output.replaceChildren(node("p",error.message,"warning"));if(collect){manualResult.textContent=`수동 수집 실패 · ${range} · ${error.message} · LLM 사용 토큰 0개 (LLM 미사용)`;output.append(node("p","저장 자료 조회를 누르면 이전 성공 기록을 확인할 수 있습니다.","helper"));}}}finally{if(valid()&&token===sequence){busy=false;buttons();}}
  }
  family.addEventListener("change",()=>{dimensionDraft.family=family.value;populate();load().catch(reportError);});choice.addEventListener("change",()=>{dimensionDraft.selected=choice.value;load().catch(reportError);});
  onlyCollected.addEventListener("change",()=>{dimensionDraft.onlyCollected=onlyCollected.checked;populate();load().catch(reportError);});
  for(const[input,key]of[[start,"start"],[end,"end"]])input.addEventListener("change",()=>{dimensionDraft[key]=input.value;load().catch(reportError);});
  for(const[input,key]of[[alignment,"alignment"],[display,"display"]])input.addEventListener("change",()=>{dimensionDraft[key]=input.value;if(currentData)show(currentData);});
  read.addEventListener("click",()=>buttonTask(read,"저장 자료 조회 중…",()=>load()).finally(buttons).catch(reportError));refresh.addEventListener("click",()=>buttonTask(refresh,"수동 수집 중…",()=>load(true)).finally(buttons).catch(reportError));populate();await load();
}

function vectorChoiceState(item) {
  if(!item)return {enabled:false,reason:"상태 확인 불가"};
  if(!item.can_collect)return {enabled:false,reason:"미연결 · 수집 경로 또는 개인 연결 확인 필요"};
  if(item.daily_analysis_eligible===false)return {enabled:false,reason:"일별 분석 미지원 · 월간 자료 또는 별도 검증 대기"};
  if(item.vector_available!==true)return {enabled:false,reason:item.vector_unavailable_reason||"저장 데이터 확인 필요 · 데이터 수집 화면에서 상태를 확인하세요"};
  return {enabled:true,reason:`저장 관측 ${item.stored_observations}건 · 기간별 공통 20쌍 충족은 별도`};
}

async function renderVectorAnalysis(area) {
  const generation = stockViewGeneration;
  await vectorSaveQueue.catch(()=>{});
  const persisted = await api("/vectors/state");vectorStateVersion=persisted.selection.version;
  const activeCatalog = await api("/dimensions"), watchItems = [];
  const blockedSelectionNames=new Set();
  function selectableDraft(keys){return new Set([...keys].filter(key=>{const entry=activeCatalog.items.find(d=>`${d.family}:${d.name}`===key);if(vectorChoiceState(entry).enabled)return true;blockedSelectionNames.add(entry?.name||key.split(":").slice(1).join(":"));return false;}));}
  watchItems.push(...(await api("/vectors/instruments")).items);
  if (generation !== stockViewGeneration || !state.user || state.view !== "analysis" || state.analysisTab !== "vectors") return;
  if(persisted.selection.value){const saved=persisted.selection.value;vectorDraft.start=saved.start;vectorDraft.end=saved.end;vectorDraft.endMode=saved.end_mode;vectorDraft.horizons=saved.horizons.filter(h=>[1,5,21].includes(h));if(!vectorDraft.horizons.length)vectorDraft.horizons=[1,5,21];vectorDraft.window=String(saved.window);vectorDraft.scale=String(saved.levels);vectorDraft.selected=new Set(saved.dimensions.map(id=>activeCatalog.items.find(d=>d.id===id)).filter(Boolean).map(d=>`${d.family}:${d.name}`));if(!state.selectedInstrument&&saved.code)state.selectedInstrument=watchItems.find(row=>row.code===saved.code)||null;if(state.selectedInstrument?.code!==saved.code)vectorDraft.selected=new Set();}
  const activeNames = new Set(activeCatalog.items.map(item => `${item.family}:${item.name}`));
  const activeGroups = vectorFactorGroups.map(([key,name,candidates]) => [key,name,candidates.filter(label => activeNames.has(`${key}:${label}`))]);
  vectorDraft.selected = new Set([...vectorDraft.selected].filter(id => activeNames.has(id)));
  vectorDraft.selected=selectableDraft(vectorDraft.selected);
  const section = panel("트레이딩 벡터 · 일별 상관 분석"); section.id = "vectorAnalysis"; area.append(section);
  section.append(node("p", "관심종목과 선택한 다차원 데이터의 변화 관계를 탐색합니다. 조건·알고리즘을 변경하면 저장 자료로 결과를 다시 계산합니다.", "helper"));
  let item = watchItems.find(row=>row.code === state.selectedInstrument?.code) || watchItems[0] || null;
  if(item){state.selectedInstrument=item;state.newsCode=item.code;}
  const heading = node("p", item ? `${item.name} · ${item.code}` : "관심종목을 먼저 등록하세요.", "stock-heading");section.append(heading);
  const watchMembership=loadWatchMembership(),watchAction=node("div");section.append(watchAction);if(item)watchAction.append(watchlistAddControl(item,watchMembership,()=>generation===stockViewGeneration));
  const businessDescription=node("section",undefined,"saved-business-description");section.append(businessDescription);
  if(item)renderSavedBusiness(businessDescription,item.code,()=>generation===stockViewGeneration);
  const topForecast=node("section",undefined,"forecast-overview");topForecast.id="vectorForecastOverview";section.append(topForecast);let forecastOverviewVersion=0;
  let initialQuoteCode=null;
  const returnText=value=>value==null?"—":`${value>0?"+":""}${(value*100).toFixed(3)}%`;
  const returnNode=value=>node("strong",returnText(value),`forecast-return ${value>0?"forecast-up":value<0?"forecast-down":"forecast-flat"}`);
  function predictedCell(forecast){const cell=node("td");if(forecast?.predicted_close==null){cell.append(node("span","추정 불가"));if(forecast?.reason)cell.append(node("small",forecast.reason));}else cell.append(node("span",priceText(Math.round(forecast.predicted_close))),document.createTextNode(" / "),returnNode(forecast.predicted_return));return cell;}
  function showOverview(data){
    if(stockPollTimer)clearInterval(stockPollTimer);stockPollTimer=null;
    const ticket=++forecastOverviewVersion;topForecast.replaceChildren(node("h3",`${data.instrument.name} · 종가·현재가와 예측 요약`));
    topForecast.append(node("p",data.persistence?.reused?"새 데이터 없음 · 저장 결과 재사용 (계산 생략)":"분석 결과 · 데이터나 조건이 변경된 경우에만 재계산","helper"));
    const quote=node("p","저장된 현재가 확인 중…","helper"),prices=node("div",undefined,"price-grid");topForecast.append(prices,quote);
    const previous=data.comparison[0]?.forecast;const last=previous?.base_close;
    function movement(target,amount,base,label="직전 종가 대비"){target.replaceChildren(node("small",label));if(amount==null||!(base>0)){target.append(node("span","자료 없음"));return;}const text=node("strong",`${amount>0?"+":""}${Math.round(amount).toLocaleString("ko-KR")} 원`,amount>0?"forecast-up":amount<0?"forecast-down":"forecast-flat");target.append(text,returnNode(amount/base));}
    const previousCard=node("div",undefined,"metric-card"),previousLine=node("div",undefined,"forecast-price-line"),previousMove=node("div",undefined,"forecast-price-movement");previousLine.append(node("strong",priceText(last)),previousMove);previousCard.append(node("small",`마지막 확정 종가 · ${previous?.base_date||"자료 없음"}`),previousLine);prices.append(previousCard);
    const lastReturn=data.points.find(p=>p.date===previous?.base_date)?.stock_return,priorClose=last!=null&&lastReturn!=null&&lastReturn>-1?last/(1+lastReturn):null;movement(previousMove,priorClose==null?null:last-priorClose,priorClose);
    const currentCard=node("div",undefined,"metric-card"),currentValue=node("strong","조회 중…"),currentLine=node("div",undefined,"forecast-price-line"),currentMove=node("div",undefined,"forecast-price-movement");currentLine.append(currentValue,currentMove);currentCard.append(node("small","현재가 · 조회 시각 기준"),currentLine);prices.append(currentCard);
    const refresh=node("button","현재가 새로 조회","secondary"),autoQuoteState=node("p","현재가 자동 갱신 준비 중…","helper");autoQuoteState.id="vectorQuoteAutoState";autoQuoteState.setAttribute("role","status");refresh.disabled=state.user.role==="viewer";topForecast.append(refresh,autoQuoteState);
    topForecast.append(node("p",`오늘 예상 종가는 ${data.today} 기준입니다. 주봉의 ‘봉 마감 예상’과 ‘오늘 예상’은 별도 모델입니다. 오늘 모델은 직전 완료 봉의 벡터를 유지해 익일 수익률을 학습하며, 반복 입력을 제외한 20개 완료 봉이 필요합니다. 현재가는 비교 표시용이며 예측 학습에 넣지 않습니다.`,"helper"));
    const table=node("table");table.id="vectorForecastSummary";table.setAttribute("aria-label","전체 방식별 오늘 및 봉 마감 예측 비교");const head=node("tr");for(const label of ["방식 / 알고리즘","봉 입력 기준 → 예측 마감일","봉 마감 예상가 / 수익률","오늘 예상가 / 전일종가 대비 수익률","현재가 − 봉 마감 예상가 / 예상가 대비 괴리율","상태 / 오늘 모델"])head.append(node("th",label));const thead=node("thead"),body=node("tbody");thead.append(head);table.append(thead,body);const gaps=[];
    for(const [h,label] of [[1,"일봉"],[5,"주봉"],[0,"기간 통합"]]){const group=data.horizon_results?.find(g=>g.horizon===h);for(const method of data.comparison){const todayForecast=h===0?method.forecast:group?.today_comparison?.find(v=>v.method===method.method)?.forecast;const bar=h===0?method.forecast:group?.forecasts?.find(v=>v.method===method.method);const row=node("tr");row.dataset.summaryPeriod=h;row.dataset.summaryMethod=method.method;row.append(node("td",`${label} · ${method.label}`),node("td",bar?`${bar.base_date||"없음"} → ${bar.target_date||"없음"}`:"미선택 / 이전 버전 결과"),predictedCell(bar),h===1||h===0?node("td"):predictedCell(todayForecast));const gap=node("td","현재가 확인 중…");gaps.push({cell:gap,forecast:bar});row.append(gap,node("td",todayForecast?.reason||todayForecast?.model||"해당 봉 선택 후 다시 분석하세요."));body.append(row);}}
    const wrap=node("div",undefined,"table-wrap");wrap.append(table);topForecast.append(wrap,node("p","차액 = 현재가 − 봉 마감 예상가, 괴리율 = 차액 ÷ 봉 마감 예상가입니다. 일봉·기간 통합의 중복 오늘 예상가 칸은 비워 둡니다. 주봉 괴리율은 주봉 마감 예상가 기준입니다. 음수는 현재가가 예상가보다 낮음(파랑), 양수는 높음(빨강)을 뜻하며 예측 수익률·적중률과 다릅니다. 오늘 종가가 확정되기 전에는 어느 예측이 맞았는지 확정할 수 없습니다. 모든 수익률은 양수 빨강·음수 파랑·0 중립색으로 표시합니다.","helper"));
function displayQuote(snapshot){if(ticket!==forecastOverviewVersion||!topForecast.isConnected)return;const current=snapshot?.price,quoteDay=snapshot?.collected_at?new Intl.DateTimeFormat("en-CA",{timeZone:"Asia/Seoul",year:"numeric",month:"2-digit",day:"2-digit"}).format(new Date(snapshot.collected_at)):null;currentValue.textContent=priceText(current);const useLast=quoteDay===today&&last>0,reference=useLast?last:snapshot?.reference_close;movement(currentMove,current!=null&&reference>0?current-reference:null,reference,useLast?"표시된 마지막 종가 대비":"제공사 기준 종가 대비");quote.textContent=snapshot?`NH PLUG · 조회 ${formatTime(snapshot.collected_at)} · ${quoteDay===today?"오늘 조회 시세":"오늘 시세 아님 · 저장 시세"} · 실시간 스트림 아님${snapshot.quote_note?` · ${snapshot.quote_note}`:""}`:"저장된 현재가가 없습니다. 현재가 새로 조회를 눌러 확인하세요.";if(useLast&&snapshot?.reference_close>0&&Math.abs(snapshot.reference_close-last)>0.5)quote.textContent+=` · 주의: 제공사 기준 종가 ${priceText(snapshot.reference_close)}와 저장 종가 ${priceText(last)}가 다릅니다. 표시 수익률은 저장 종가 기준입니다.`;for(const {cell,forecast} of gaps){cell.replaceChildren();if(current>0&&forecast?.predicted_close!=null&&forecast.target_date>=today&&data.today===today&&quoteDay===today){cell.append(node("span",`${Math.round(current-forecast.predicted_close).toLocaleString("ko-KR")} 원 / `),returnNode(current/forecast.predicted_close-1));}else cell.textContent="비교 불가";}}
    let inflight=false,lastFailure=0,lastSnapshot=null,marketStatus=null,statusCheckedAt=0;
    const quoteActive=()=>ticket===forecastOverviewVersion&&generation===stockViewGeneration&&section.isConnected&&state.view==="analysis"&&state.analysisTab==="vectors";
    async function loadQuote(fresh=false,auto=false){
      if(inflight||!quoteActive())return;inflight=true;refresh.disabled=true;
      try{const path=`/market/instruments/${data.instrument.code}${fresh?`/refresh${auto?"?auto=true&quote_only=true":""}`:""}`;let result;
        try{result=await api(path,fresh?{method:"POST"}:{});}catch(error){
          if(error.code!=="NAMUH_AUTH_REQUIRED"||state.user?.role==="viewer"||!fresh||!quoteActive())throw error;
          const policy=await api("/user/brokers/namuh");if(!quoteActive())return;
          if(!policy.enabled||!policy.key_owner_matches||policy.missing_settings?.length)throw error;
          const verified=await api("/user/brokers/namuh/verify",{method:"POST",body:JSON.stringify({version:policy.version})});if(!quoteActive())return;
          if(verified.last_probe?.status!=="completed"||verified.last_probe?.policy_version!==policy.version)throw error;
          result=await api(path,{method:"POST"});
        }
if(!quoteActive())return;lastSnapshot=result.snapshot;displayQuote(lastSnapshot);if(fresh){lastFailure=0;autoQuoteState.textContent=auto?`정규장 현재가 5초 자동 갱신 · 차액 동시 갱신 · 확인 ${formatTime(new Date().toISOString())}`:"현재가 최신 조회 완료 · 정규장 중 5초 자동 갱신";}
      }catch(error){if(quoteActive()){lastFailure=Date.now();if(!lastSnapshot)currentValue.textContent="조회 실패";quote.textContent=`시세 갱신 실패: ${error.message}${lastSnapshot?` · 마지막 성공 조회 ${formatTime(lastSnapshot.collected_at)}`:""}`;autoQuoteState.textContent="갱신 실패 · 자동 재시도는 30초 후. 차액 비교를 보류합니다.";for(const {cell} of gaps)cell.textContent="갱신 실패 · 비교 보류";if(error.status===409)statusCheckedAt=0;}}
      finally{inflight=false;if(quoteActive())refresh.disabled=state.user.role==="viewer";}
    }
    async function quoteTick(){
      if(!quoteActive()){if(ticket===forecastOverviewVersion&&stockPollTimer){clearInterval(stockPollTimer);stockPollTimer=null;}return;}
      if(document.hidden||inflight||state.user.role==="viewer")return;
      try{if(!marketStatus||Date.now()-statusCheckedAt>=(marketStatus.is_open?30000:60000)){marketStatus=await api("/market/session");statusCheckedAt=Date.now();}if(!quoteActive())return;
        if(!marketStatus.is_open){autoQuoteState.textContent=`자동 갱신 중지 · ${marketStatus.reason} · 종목 변경 시 1회 조회, 수동 조회 가능`;return;}
        if(Date.now()-lastFailure<30000)return;
        await loadQuote(true,true);
      }catch(error){if(quoteActive()){statusCheckedAt=Date.now();marketStatus={is_open:false,reason:"장 상태 확인 실패"};autoQuoteState.textContent=`장 상태 확인 실패: ${error.message} · 자동 조회 중지`;}}
    }
    refresh.addEventListener("click",()=>loadQuote(true));
    const firstForCode=initialQuoteCode!==data.instrument.code;initialQuoteCode=data.instrument.code;
    loadQuote().then(async()=>{if(!quoteActive())return;if(state.user.role==="viewer"){autoQuoteState.textContent="조회 전용 계정 · 저장 시세만 표시합니다.";return;}if(firstForCode)await loadQuote(true);});
    if(state.user.role!=="viewer")stockPollTimer=setInterval(()=>quoteTick(),5000);
  }
  const attributeStatus=node("p","종목 다차원 속성 확인 중…","helper"),retryDefaults=node("button","기본 속성 생성 재시도","secondary");section.append(attributeStatus,retryDefaults);let attributesReady=false,attributeGeneration=0,pendingDraftSignature=null;
  async function loadAttributes(code,initialize=true,attempt=0){const ticket=++attributeGeneration;try{let result=await api(`/vectors/defaults/${code}`);if(!result.profile&&initialize&&state.user.role!=="viewer")result=await api(`/vectors/defaults/${code}`,{method:"POST"});if(ticket!==attributeGeneration||generation!==stockViewGeneration||item?.code!==code||!section.isConnected)return;const profile=result.profile;retryDefaults.hidden=!!profile&&(profile.origin!=="llm_default"||profile.status==="ready");if(!profile){attributeStatus.textContent="저장된 속성 없음 · 관리자 자동 생성 정책을 확인하세요.";return;}if(profile.origin==="llm_default"){attributeStatus.textContent=`사업 설명 기반 기본 속성 · ${profile.status} · ${profile.generation_method==="reviewed_business_rules"?"검토된 사업 매핑 · LLM 미호출":profile.model?.model||"내 기본 모델"}${profile.error?` · ${profile.error}`:""} · ${profile.reason||""} · 미검증 제안`;if(profile.status==="pending"&&attempt===0)pendingDraftSignature=JSON.stringify([...vectorDraft.selected].sort());if(profile.status==="pending"&&attempt<15)setTimeout(()=>{if(generation===stockViewGeneration&&item?.code===code&&section.isConnected)loadAttributes(code,false,attempt+1).catch(reportError);},2000);}else attributeStatus.textContent=`사용자 저장 속성 · ${profile.selected_dimensions?.length||0}개 차원 · 자동 생성으로 덮어쓰지 않습니다.`;if(profile.business_context){attributeStatus.append(node("span",` · 사업: ${profile.business_context.description}`));for(const url of profile.business_context.sources||[]){const link=node("a"," 사업 설명 출처");link.href=url;link.target="_blank";link.rel="noopener noreferrer";attributeStatus.append(link);}if(profile.unconnected_topics?.length)attributeStatus.append(node("span",` · 미연결 주제: ${profile.unconnected_topics.join(", ")}`));}if((attempt===0||pendingDraftSignature===JSON.stringify([...vectorDraft.selected].sort()))&&profile.selected_dimensions?.length&&(profile.origin!=="llm_default"||profile.status==="ready")){vectorDraft.selected=new Set(profile.selected_dimensions.map(id=>activeCatalog.items.find(d=>d.id===id)).filter(Boolean).map(d=>`${d.family}:${d.name}`));if(attributesReady)update();}}catch(error){if(ticket===attributeGeneration&&item?.code===code)attributeStatus.textContent=`기본 속성 확인 실패: ${error.message}`;}}
  retryDefaults.addEventListener("click",async()=>{if(!item)return;retryDefaults.disabled=true;try{await runDefaultInitialization(retryDefaults,()=>valid()&&!!item,`/vectors/defaults/${item.code}`);await loadAttributes(item.code,false);}catch(error){attributeStatus.textContent=error.message;}finally{retryDefaults.disabled=false;}});if(item)await loadAttributes(item.code);
  if (!watchItems.length) {
    const choose = node("button", "관심종목에서 종목 선택", "secondary"); choose.type = "button";
    choose.addEventListener("click", () => openView("watchlist")); section.append(choose);
  }
  const controls = node("div", undefined, "vector-controls"); controls.id="vectorTopControls";section.insertBefore(controls,topForecast);
  const stockField=node("label","분석 관심종목"), stockChoice=node("select");stockChoice.setAttribute("aria-label","분석 관심종목");
  for(const row of watchItems){const option=node("option",`${row.name} · ${row.code}`);option.value=row.code;stockChoice.append(option);}
  stockChoice.disabled=!watchItems.length;stockChoice.value=item?.code || "";stockField.append(stockChoice);controls.append(stockField);
  stockChoice.addEventListener("change",()=>{if(stockChoice.value)renderSavedBusiness(businessDescription,stockChoice.value,()=>generation===stockViewGeneration);});
  stockChoice.addEventListener("change",()=>{const chosen=watchItems.find(row=>row.code===stockChoice.value);watchAction.replaceChildren();if(chosen)watchAction.append(watchlistAddControl(chosen,watchMembership,()=>generation===stockViewGeneration));});
  const today = new Intl.DateTimeFormat("en-CA", {timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit"}).format(new Date());
  const forecastTiming=await api("/vectors/forecast-clock"),yesterday=forecastTiming.available_cutoff;
  if (!vectorDraft.end || vectorDraft.end > yesterday || vectorDraft.endMode === "rolling_yesterday") vectorDraft.end = yesterday;
  const start = node("input"), end = node("input");
  for (const [input, key, label] of [[start, "start", "분석 시작일"], [end, "end", "분석 종료일"]]) {
    input.type = "date"; input.value = vectorDraft[key]; input.max = yesterday; input.required = true; input.setAttribute("aria-label", label);
    const field = node("label", label); field.append(input); controls.append(field);
  }
  function selectField(key, label, choices) {
    const field = node("label", label), select = node("select"); select.setAttribute("aria-label", label);
    for (const [value, text] of choices) { const option = node("option", text); option.value = value; select.append(option); }
    select.value = vectorDraft[key]; select.addEventListener("change", () => { vectorDraft[key] = select.value; update(); });
    field.append(select); controls.append(field); return select;
  }
  selectField("window", "일별 보조 분석 창", [["60", "60 거래일"], ["120", "120 거래일"], ["252", "252 거래일"]]);
  controls.append(node("p","Pearson · 선형 동행 / OLS","helper"));
  vectorDraft.method="pearson";
  selectField("scale", "강도 표시", [["5", "5단계"], ["9", "9단계"]]);
  const rollingField=node("label","종료일 자동 · 장 마감 반영"),rollingEnd=node("input");rollingEnd.type="checkbox";rollingEnd.checked=vectorDraft.endMode === "rolling_yesterday";rollingEnd.addEventListener("change",()=>{vectorDraft.endMode=rollingEnd.checked?"rolling_yesterday":"fixed";if(rollingEnd.checked){vectorDraft.end=yesterday;end.value=yesterday;}update();});rollingField.append(rollingEnd);controls.append(rollingField);
  const horizons = node("fieldset", undefined, "vector-horizons"); horizons.append(node("legend", "봉 단위 · 일봉 / 달력 주봉 / 달력 월봉")); section.append(horizons);
  for (const [days, label] of [[1, "일봉"], [5, "주봉"], [21, "월봉"]]) {
    const field = node("label", undefined, "vector-check"), check = node("input"); check.type = "checkbox"; check.checked = vectorDraft.horizons.includes(days);
    check.addEventListener("change", () => { vectorDraft.horizons = check.checked ? [...vectorDraft.horizons, days].sort((a,b) => a-b) : vectorDraft.horizons.filter(value => value !== days); update(); });
    field.append(check, node("span", label)); horizons.append(field);
  }
  section.append(node("p", "조회 시작일~종료일 전체의 일봉·달력 주봉·달력 월봉 종가 추이를 각각 비교합니다. 주봉·월봉은 각 주·월 마지막 거래일 종가이며 이동 5·21일 변화가 아닙니다. 기간 변경 시 종목·선택 차원의 미수집 구간을 DB에 소급 저장합니다. 다른 화면의 조회 기간은 바뀌지 않습니다.", "helper"));
  const catalog = panel("차원 후보 선택"); area.append(catalog);
  catalog.append(node("p", "1. 차원 선택 → 2. 마지막 변경 약 0.3초 후 자동 분석 → 3. 분석 완료 후 ‘종목 속성으로 저장’. 선택 조건은 화면 복원용으로 자동 저장되지만 종목 속성은 버튼을 눌러야 확정됩니다. 미수집 요인을 포함하면 혼합 벡터가 계산되지 않을 수 있습니다. ETF는 원지수·현물과 다르며, 동일 ETF를 중복 선택하면 가중 효과가 중복됩니다.", "helper"));
  const searchLabel = node("label", "후보 검색", "vector-search"), search = node("input"); search.type = "search"; search.placeholder = "예: 반도체, 구리, Nasdaq"; searchLabel.append(search); catalog.append(searchLabel);
  const summary = node("p", undefined, "helper"); summary.setAttribute("role", "status"); catalog.append(summary);
  const selectedArea = node("div", undefined, "vector-selected"); selectedArea.setAttribute("aria-label", "선택한 차원"); catalog.append(selectedArea);
  const groups = [];
  for (const [key, name, candidates] of activeGroups) {
    const group = node("details", undefined, "vector-factor-group"); group.open = key === "sector"; group.append(node("summary", `${name} · ${candidates.length}개 후보`));
    const grid = node("div", undefined, "vector-factor-grid"); const entries = [];
    for (const label of candidates) {
      const id = `${key}:${label}`, field = node("label", undefined, "vector-check"), check = node("input"); check.type = "checkbox"; check.checked = vectorDraft.selected.has(id);
      const availability=vectorChoiceState(activeCatalog.items.find(d=>`${d.family}:${d.name}`===id));check.disabled=!availability.enabled;field.title=availability.reason;
      check.addEventListener("change", () => { if(!availability.enabled){check.checked=false;return;}if (check.checked) vectorDraft.selected.add(id); else vectorDraft.selected.delete(id); update(); });
      field.append(check, node("span", label));if(!availability.enabled){field.classList.add("vector-unavailable");field.append(node("small",availability.reason));}grid.append(field); entries.push({id, label, field, check});
    }
    group.append(grid); catalog.append(group); groups.push({key, name, group, entries});
  }
  const clear = node("button", "선택 초기화", "secondary"); clear.type = "button"; clear.addEventListener("click", () => { vectorDraft.selected.clear(); update(); }); catalog.append(clear);
  const preview = panel("혼합 벡터 · 추이와 상관도"); preview.id="vectorResults";area.append(preview);
  const shape = node("p"), readiness = node("p", "차원 후보를 선택하세요.", "helper"); readiness.setAttribute("role", "status"); preview.append(shape, readiness);
  const validation = node("p", undefined, "helper"); validation.setAttribute("role", "status"); preview.append(validation);
  const attributeSave=node("button","종목 속성으로 저장","primary"),attributeNote=node("p","저장 대기 · 차원을 선택하고 자동 분석을 완료하세요.","helper"),attributeView=node("div"),saveActions=node("div",undefined,"source-row");
  attributeSave.type="button";attributeSave.disabled=true;attributeNote.setAttribute("role","status");saveActions.append(attributeSave,attributeNote);preview.append(saveActions,attributeView);
  const charts = node("div",undefined,"vector-trend-charts"), resultArea=node("div");preview.append(charts,resultArea);
  const legend = node("p", undefined, "vector-legend"); preview.append(legend);
  const actions = node("div", undefined, "source-row"), exportButton = node("button", "분석 JSON 다운로드", "secondary"), run = node("button", "저장 자료로 다시 분석", "secondary"), refreshStock=node("button","종목 시세 새로 조회 후 분석","secondary"); exportButton.type = run.type = refreshStock.type = "button"; actions.append(run, refreshStock, exportButton);saveActions.after(actions);
  preview.append(node("p", "탐색용 분석·실험적 종가 추정입니다. 회귀 모델은 과거 표본에만 맞추며 검증된 예측 성과는 없습니다. 결과 JSON을 내려받을 수 있고, 알고리즘 채택·자동 주문은 실행하지 않습니다.", "helper"));
  let analysisResult=null, requestVersion=0, analysisTimer=null;
  const storageStatus=node("p", "선택 조건 자동 저장 · 분석 결과 DB 이력 보존", "helper");preview.append(storageStatus);
  let lastSelectionSignature="";
  function saveSelection(){if(state.user.role === "viewer" || !start.checkValidity() || !end.checkValidity() || vectorDraft.selected.size>20)return;const value={code:item?.code||null,start:vectorDraft.start,end:vectorDraft.end,end_mode:vectorDraft.endMode,dimensions:[...vectorDraft.selected].map(id=>activeCatalog.items.find(d=>`${d.family}:${d.name}`===id)?.id).filter(Boolean),horizons:[...vectorDraft.horizons],window:Number(vectorDraft.window),levels:Number(vectorDraft.scale)};const signature=JSON.stringify(value);if(signature===lastSelectionSignature)return;lastSelectionSignature=signature;const owner=state.user.id;vectorSaveQueue=vectorSaveQueue.catch(()=>{}).then(async()=>{if(state.user?.id!==owner)return;const result=await api("/vectors/selection",{method:"PUT",body:JSON.stringify({version:vectorStateVersion,value})});vectorStateVersion=result.version;if(valid())storageStatus.textContent=`선택 조건 DB 저장 완료 · 버전 ${result.version}`;}).catch(error=>{lastSelectionSignature="";if(valid())storageStatus.textContent=`자동 저장 실패: ${error.message}`;});}
  const history=node("details"),historyBody=node("div");history.append(node("summary","저장된 분석 이력 · 최근 20건"),historyBody);preview.append(history);
  async function loadHistory(){const data=await api("/vectors/history");if(!valid())return;historyBody.replaceChildren();for(const row of data.items){const button=node("button",`${row.request.code} · ${row.data_cutoff} · ${formatTime(row.created_at)} 결과 보기`,"secondary");button.addEventListener("click",async()=>{const saved=await api(`/vectors/history/${row.id}`);if(!valid())return;clearTimeout(analysisTimer);requestVersion++;analysisResult={...saved.result,persistence:{saved:true,id:saved.id,created_at:saved.created_at}};showResult(analysisResult);readiness.textContent=`저장 결과 보기 · ${saved.result.instrument.name} · 자료 마감 ${saved.result.data_cutoff} (위 입력 조건과 다를 수 있음)`;});historyBody.append(button);}if(!data.items.length)historyBody.append(node("p","저장된 분석 결과가 없습니다.","helper"));}
  const valid=()=>generation === stockViewGeneration && section.isConnected && state.analysisTab === "vectors";
  function showResult(data) {
    // Preserve persisted results; the current presentation uses Pearson only.
    data={...data,comparison:data.comparison.filter(v=>v.method==="pearson"),horizon_results:data.horizon_results?.map(g=>({...g,comparison:g.comparison.filter(v=>v.method==="pearson"),forecasts:g.horizon===21?[]:g.forecasts?.filter(v=>v.method==="pearson"),today_comparison:g.horizon===21?[]:g.today_comparison?.filter(v=>v.method==="pearson")}))};
    charts.replaceChildren();resultArea.replaceChildren();
    showOverview(data);
    if(data.schema_version>=5&&data.horizon_results){
      for(const group of data.horizon_results){
        const label=({1:"일봉",5:"주봉",21:"월봉"})[group.horizon];
        const block=panel(`${label} · 종가 추이와 독립 상관도`);block.dataset.horizon=group.horizon;
        const pair=node("div",undefined,"vector-period-pair"),stockPanel=panel(`${data.instrument.name} · ${label} 종가`),factorPanel=panel(`혼합 차원 지수 · ${label} 종가`);pair.append(stockPanel,factorPanel);
        stockPanel.append(dimensionChart(group.points.map(p=>({date:p.date,value:p.stock_close})),{name:`종목 ${label} 종가`,unit:"원"},false));
        factorPanel.append(dimensionChart(group.points.map(p=>({date:p.date,value:p.vector_score})),{name:`벡터 ${label}`,unit:"조회 구간 첫 유효값 기준 100"},false));block.append(pair);
        block.append(node("p",`달력 기준 ${label} 종가를 한 번씩 비교합니다. 요청 ${data.request.start} ~ ${data.data_cutoff} 전체 구간 · ${group.window_sessions}봉 · 대응 ${group.first||"없음"} ~ ${group.last||"없음"} · 진행/부분 봉 ${group.partial_bars}개는 차트에만 표시하고 상관도에서는 제외합니다.`,"helper"));
        block.append(node("p","주·월별 마지막 거래일 종가 기준 선형 추이입니다(장중 OHLC 캔들이 아님). 추이 수준 상관도는 공통 상승·추세로 높아질 수 있어 봉 간 수익률 상관도를 함께 확인하세요. 3봉 이상만 탐색 계산하며 20봉 미만은 표본 부족 주의입니다.","warning"));
        const table=node("table");table.setAttribute("aria-label",`${label} 알고리즘별 상관도`);const head=node("tr");for(const title of ["알고리즘","종가 추이 상관도","봉 간 수익률 상관도","강도","유효 봉"])head.append(node("th",title));table.append(head);
        for(const value of group.comparison){const row=node("tr");row.dataset.method=value.method;for(const text of [value.label,value.correlation==null?"계산 불가":value.correlation.toFixed(4),value.return_correlation==null?"계산 불가":value.return_correlation.toFixed(4),value.strength.label,`${value.samples}봉 / 변화 ${value.return_samples}쌍${value.low_sample?" · 표본 부족 주의":""}`])row.append(node("td",text));table.append(row);}
        const wrap=node("div",undefined,"table-wrap");wrap.append(table);block.append(wrap);
        if(group.horizon!==21){block.append(node("h3",`${label} 다차원 벡터 → 다음 봉 수익률·예상 종가`));
        block.append(node("p","완료된 직전 봉의 차원 지수로 다음 봉 수익률을 학습합니다. 오늘·미완성 봉은 제외하며 최소 20쌍이 필요합니다. 예측 상관도는 위 동시 추이 상관도와 다릅니다. 검증은 과거 자료만 순차 학습한 최근 최대 30개 예측의 수익률 오차이며, 투자 성과 보장은 아닙니다.","helper"));
        const forecasts=node("table");forecasts.dataset.periodForecasts=group.horizon;forecasts.setAttribute("aria-label",`${label} 알고리즘별 수익률과 예상 주가`);const forecastHead=node("tr");for(const title of ["알고리즘 / 모델","기준 종가 → 예측 마감일","예측 상관도 / 학습 표본","예상 종가 / 수익률","과거 순차 검증","상태"])forecastHead.append(node("th",title));forecasts.append(forecastHead);
        for(const f of group.forecasts||[]){const row=node("tr");row.dataset.forecastMethod=f.method;const v=f.validation;for(const text of [`${f.label} / ${f.model}`,`${f.base_date||"없음"} · ${f.base_close==null?"결측":f.base_close.toLocaleString("ko-KR")}원 → ${f.target_date||"없음"}`,`${f.lagged_correlation==null?"계산 불가":f.lagged_correlation.toFixed(4)} / ${f.samples}쌍`,f.predicted_close==null?"추정 불가":`${Math.round(f.predicted_close).toLocaleString("ko-KR")}원 / ${(f.predicted_return*100).toFixed(3)}%`,v?.samples?`${v.samples}건 · MAE ${(v.mae*100).toFixed(3)}%p · 방향 ${(v.direction_accuracy*100).toFixed(1)}%`:"검증 표본 부족",f.reason||`실험적 추정${f.extrapolation?" · 학습 범위 밖 외삽 주의":""}`])row.append(node("td",text));forecasts.append(row);}const forecastWrap=node("div",undefined,"table-wrap");forecastWrap.append(forecasts);block.append(forecastWrap);
        }
        const detail=node("details");detail.append(node("summary",`${label} 종가·봉 구간 표`));const periods=node("table"),header=node("tr");for(const text of ["종가일","봉 시작","봉 종료","종목 종가","차원 지수","봉 상태"])header.append(node("th",text));periods.append(header);
        for(const p of [...group.points].reverse()){const row=node("tr");for(const text of [p.date,p.period_start,p.period_end,p.stock_close==null?"결측":p.stock_close.toLocaleString("ko-KR"),p.vector_score==null?"결측":p.vector_score.toFixed(4),p.partial?"진행/부분 봉 · 상관도 제외":"완료 봉"])row.append(node("td",text));periods.append(row);}const detailWrap=node("div",undefined,"table-wrap");detailWrap.append(periods);detail.append(detailWrap);block.append(detail);charts.append(block);
      }
    }else charts.append(node("p","이전 버전 결과입니다. 일봉·주봉·월봉 추이 비교는 현재 기간으로 다시 분석하세요.","warning"));
    const combined=node("details");combined.append(node("summary","기존 혼합 분석 · 종가와 모든 선택 기간의 통합 벡터 추이"));charts.append(combined);
    const first=panel(`${data.instrument.name} · 종가 추이`), second=panel("혼합 벡터 · 표준화 변화 점수");combined.append(first,second);
    first.append(dimensionChart(data.points.map(p=>({date:p.date,value:p.stock_close})),{name:"선택 종목 종가",unit:"원"},false));
    second.append(dimensionChart(data.points.map(p=>({date:p.date,value:p.vector_score})),{name:"혼합 벡터",unit:"z-score 평균"},false));
    resultArea.append(node("p","위 기간별 표는 같은 기간의 변화끼리 독립 비교합니다. 아래 기존 통합 분석은 선택한 모든 기간의 혼합 벡터와 종목 일별 수익률을 비교하며, 오늘 종가 추정과 수동 속성 저장에 사용합니다. 접힌 종가 차트의 가격 수준을 직접 상관 분석한 값은 아닙니다.","helper"));
    const s=data.summary;
    resultArea.append(node("h3","기간 통합 벡터 · 일별 상관도와 오늘 예상 종가 비교"));
    resultArea.append(node("p",`자료 마감 ${data.data_cutoff} · 추정 대상 ${data.today} 종가 · 오늘 관측값은 분석·학습에서 제외합니다.`,"helper"));
    const comparison=node("table");comparison.id="vectorComparison";comparison.setAttribute("aria-label","알고리즘별 상관도와 오늘 예상 종가");const comparisonHead=node("tr");
    for(const label of ["알고리즘","동일 일 상관도 / 강도","전일→익일 상관도 / 학습 표본","오늘 예상 종가 / 수익률","추정 모델 / 상태"])comparisonHead.append(node("th",label));const comparisonThead=node("thead");comparisonThead.append(comparisonHead);comparison.append(comparisonThead);const comparisonBody=node("tbody");
    for(const value of data.comparison){const forecast=value.forecast,row=node("tr");row.dataset.method=value.method;const number=v=>v==null?"계산 불가":v.toFixed(4);row.append(node("td",value.label),node("td",`${number(value.correlation)} / ${value.strength.label} · ${value.samples}쌍`),node("td",`${number(forecast.lagged_correlation)} / ${forecast.samples}쌍`),node("td",forecast.predicted_close==null?"추정 불가":`${forecast.predicted_close.toLocaleString("ko-KR",{maximumFractionDigits:0})} 원 / ${(forecast.predicted_return*100).toFixed(3)}%`));const description=node("td");description.append(node("p",forecast.model),node("p",forecast.reason || `기준 ${forecast.base_date} 종가 ${forecast.base_close.toLocaleString("ko-KR")} 원 · 학습 ${forecast.training_first} ~ ${forecast.training_last}`,"helper"));if(forecast.extrapolation)description.append(node("p","전일 벡터가 학습 범위 밖입니다. 외삽·분위수 경계 추정 주의","warning"));row.append(description);comparisonBody.append(row);}comparison.append(comparisonBody);const comparisonWrap=node("div",undefined,"table-wrap");comparisonWrap.append(comparison);resultArea.append(comparisonWrap);
    resultArea.append(node("p",`최근 ${s.window_sessions}/${data.request.window} 거래일 창 · 유효 ${s.samples}쌍 · 결측 ${s.missing_sessions}일 · 최소 ${s.required_samples}쌍 필요`,"helper"));
    resultArea.append(node("p",`대응 기간 ${s.first || "없음"} ~ ${s.last || "없음"} · 종목 저장 자료 마지막 날짜 ${s.stock_last || "없음"}`,"helper"));
    if(s.correlation == null)resultArea.append(node("p","표본 부족 또는 상수 계열로 상관도를 계산할 수 없습니다. 미수집 요인·조회 기간·종목 시세를 확인하세요.","warning"));
    const rolling=data.points.filter(p=>p.correlation!=null).at(-1);
    if(rolling)resultArea.append(node("p",`일별 이동 Pearson 상관도: ${rolling.date} 기준 ${rolling.correlation.toFixed(4)} (${rolling.sample_count}쌍) · 세 방식은 아래 일별 표에서 비교합니다.`,"helper"));
    const daily=node("details");daily.append(node("summary","일별 이동 상관도 결과"));const dailyTable=node("table"),dailyHead=node("tr");for(const label of ["날짜","종목 일별 수익률","벡터 점수","Pearson","표본"])dailyHead.append(node("th",label));dailyTable.append(dailyHead);for(const point of [...data.points].reverse()){const row=node("tr");for(const text of [point.date,point.stock_return==null?"결측":`${(point.stock_return*100).toFixed(3)}%`,point.vector_score==null?"결측":point.vector_score.toFixed(4),...["pearson"].map(method=>point.correlations[method]==null?"계산 불가":point.correlations[method].toFixed(4)),`${point.sample_count}쌍`])row.append(node("td",text));dailyTable.append(row);}const dailyWrap=node("div",undefined,"table-wrap");dailyWrap.append(dailyTable);daily.append(dailyWrap);resultArea.append(daily);
    const details=node("details"), table=node("table");details.append(node("summary","요인별 상관도 · 데이터 출처"));const head=node("thead"), row=node("tr");for(const text of ["요인 / 변화 기간","상관도 / 표본","출처 / 최근 관측"] )row.append(node("th",text));head.append(row);table.append(head);const body=node("tbody");
    for(const feature of data.features){const source=data.dimensions.find(d=>d.id===feature.dimension_id), tr=node("tr");tr.append(node("td",`${feature.name} · ${feature.horizon}일${source.same_instrument?" · 동일 종목 주의":""}`),node("td",`${feature.correlation==null?"계산 불가":feature.correlation.toFixed(4)} / ${feature.samples}쌍`));const td=node("td"),link=node("a",source.source);link.href=source.source_url;link.target="_blank";link.rel="noopener noreferrer";td.append(link,node("p",`${source.last_observation || "미수집"} · ${source.lag === "next_krx_session"?"다음 한국 거래일로 이동":"동일 관측일"}`,"helper"));if(source.proxy_definition)td.append(node("p",source.proxy_definition,"helper"));tr.append(td);body.append(tr);}table.append(body);const tableWrap=node("div",undefined,"table-wrap");tableWrap.append(table);details.append(tableWrap);resultArea.append(details);
    const selectedIds=()=>[...vectorDraft.selected].map(key=>activeCatalog.items.find(d=>`${d.family}:${d.name}`===key)?.id).sort();
    const matches=()=>data.request.code===item?.code&&JSON.stringify([...data.request.dimensions].sort())===JSON.stringify(selectedIds())&&data.request.start===vectorDraft.start&&data.request.end===vectorDraft.end&&data.request.window===Number(vectorDraft.window)&&JSON.stringify(data.request.horizons)===JSON.stringify(vectorDraft.horizons);
    attributeSave.disabled=state.user.role==="viewer"||!data.persistence?.saved||data.status!=="completed"||!matches();
    attributeNote.textContent=state.user.role==="viewer"?"저장 불가 · 조회 전용 사용자입니다.":!matches()?"저장 불가 · 현재 선택 조건과 다른 결과입니다. 현재 조건으로 다시 분석하세요.":data.status!=="completed"?`저장 불가 · 자료 부족 또는 상수 계열로 유효한 상관도가 없습니다 (유효 ${s.samples}쌍 / 최소 ${s.required_samples}쌍). 미수집 요인·기간을 확인하세요.`:!data.persistence?.saved?"저장 불가 · 분석 결과의 DB 저장이 확인되지 않았습니다.":"저장 가능 · 현재 조합과 기간·상관도를 종목 속성으로 확정합니다. 기존 속성은 교체됩니다.";
    attributeSave.onclick=async()=>{if(attributeSave.disabled||!matches())return;const version=requestVersion;attributeSave.disabled=true;attributeNote.textContent="종목 속성 저장 중…";try{const profile=await api("/vectors/attributes",{method:"POST",body:JSON.stringify({result_id:data.persistence.id})});if(!valid()||version!==requestVersion)return;attributeNote.textContent=`${data.instrument.name} · 종목 속성 저장 완료 · ${profile.selected_dimensions.length}개 차원 · 기존 속성 교체 · 뉴스 분석에 적용`;attributeView.replaceChildren(factorProfileView(profile));attributeStatus.textContent=`사용자 저장 속성 · ${profile.selected_dimensions.length}개 차원 · LLM 기본값을 교체했습니다.`;retryDefaults.hidden=true;}catch(error){if(valid()&&version===requestVersion)attributeNote.textContent=`속성 저장 실패 · ${error.message}`;}finally{if(valid()&&version===requestVersion)attributeSave.disabled=state.user.role==="viewer"||data.status!=="completed"||!data.persistence?.saved||!matches();}};
for(const warning of data.warnings)resultArea.append(node("p",warning,"helper"));
    for(const cell of resultArea.querySelectorAll("#vectorComparison td:nth-child(4)")){const match=cell.textContent.match(/([+-]?\d+(?:\.\d+)?)%$/);if(match){const prefix=cell.textContent.slice(0,match.index);cell.replaceChildren(document.createTextNode(prefix),returnNode(Number(match[1])/100));}}
    for(const cell of charts.querySelectorAll("[data-period-forecasts] tr[data-forecast-method] td:nth-child(4)")){const match=cell.textContent.match(/([+-]?\d+(?:\.\d+)?)%$/);if(match){const prefix=cell.textContent.slice(0,match.index);cell.replaceChildren(document.createTextNode(prefix),returnNode(Number(match[1])/100));}}
    topForecast.setAttribute("aria-busy","false");readiness.textContent=data.status!=="completed"?"자동 분석 종료 · 자료 부족/계산 불가 · 종목 속성 저장 불가":data.freshness?.needs_refresh?"분석 완료 · 원천 부족·갱신 지연 주의 · 저장 자료 기반 탐색 결과":data.persistence?.reused?"분석 완료 · 동일 입력의 저장 결과 재사용":"자동 분석 완료 · 탐색용 결과";
    exportButton.disabled=false;
    if(data.persistence?.saved)storageStatus.textContent=`분석 이력 DB 저장 (종목 속성 확정과 별개) · ${data.persistence.id}`;
  }
  async function analyzeNow(version) {
    if(!valid() || version!==requestVersion)return;
    readiness.textContent="자동 분석 중 · 저장 결과 확인 및 현재 선택 조건 계산…";
    attributeSave.disabled=true;attributeNote.textContent=state.user.role==="viewer"?"저장 불가 · 조회 전용 사용자입니다.":"저장 대기 · 자동 분석 중입니다.";
    try {
      const dimensions=[...vectorDraft.selected].map(id=>activeCatalog.items.find(d=>`${d.family}:${d.name}`===id)?.id).filter(Boolean);
      const request=vectorAnalysisRequest(item.code,{start:vectorDraft.start,end:vectorDraft.end},dimensions,{end_mode:vectorDraft.endMode==="rolling_yesterday"?"latest_completed":"fixed",horizons:[...vectorDraft.horizons].sort((a,b)=>a-b),window:Number(vectorDraft.window),method:vectorDraft.method,levels:Number(vectorDraft.scale)});
      const cached=await api("/vectors/analyze",{method:"POST",body:JSON.stringify({...request,cache_only:true})});
      if(!valid()||version!==requestVersion)return;
      if(cached.persistence?.reused&&cached.status==="completed"){analysisResult=cached;showResult(cached);return;}
      const range={start:vectorDraft.start,end:vectorDraft.end};const syncNotes=[];
      if(state.user.role!=="viewer"){
        readiness.textContent="종목 주가 확인 중 · 차원 원천은 독립 수집기가 갱신합니다…";
        try{const result=await api("/vectors/stock-history",{method:"POST",body:JSON.stringify({code:item.code,...range})});if(result.remaining.length)syncNotes.push(`종목 미제공 ${result.remaining.length}거래일`);}catch(error){syncNotes.push(`종목 과거 수집: ${error.message}`);}
        // Dimension collection is owned by the independent data worker.
      }
      if(!valid()||version!==requestVersion)return;
      const data=await api("/vectors/analyze",{method:"POST",body:JSON.stringify(request)});
      if(!valid() || version!==requestVersion)return;analysisResult=data;showResult(data);for(const note of syncNotes)resultArea.prepend(node("p",note,"warning"));loadHistory().catch(error=>{if(valid())storageStatus.textContent=error.message;});
    }catch(error){if(valid() && version===requestVersion){attributeSave.disabled=true;attributeNote.textContent=`저장 불가 · 분석 실패: ${error.message}`;readiness.textContent="자동 분석 실패";topForecast.setAttribute("aria-busy","false");topForecast.replaceChildren(node("p",`분석 중단: ${error.message}`,"warning"));resultArea.replaceChildren(node("p","분석 실패: 현재 조건의 결과가 없습니다.","warning"));}}
  }
  run.addEventListener("click",()=>update());
  refreshStock.addEventListener("click",async()=>{if(!item)return;attributeSave.disabled=true;attributeSave.onclick=null;attributeView.replaceChildren();attributeNote.textContent="저장 대기 · 종목 시세 조회 후 자동 분석합니다.";refreshStock.disabled=true;requestVersion++;clearTimeout(analysisTimer);analysisResult=null;charts.replaceChildren();resultArea.replaceChildren();exportButton.disabled=true;readiness.textContent="종목 시세 조회 중…";try{await api(`/market/instruments/${item.code}/refresh`,{method:"POST"});if(valid())update();}catch(error){if(valid()){readiness.textContent=error.message;attributeNote.textContent=`저장 불가 · 종목 시세 조회 실패: ${error.message}`;}}finally{if(valid())refreshStock.disabled=state.user.role === "viewer" || !item;}});
  stockChoice.addEventListener("change",async()=>{item=watchItems.find(row=>row.code===stockChoice.value);state.selectedInstrument=item;state.newsCode=item.code;heading.textContent=`${item.name} · ${item.code}`;vectorDraft.selected=new Set();update();await loadAttributes(item.code);});
  exportButton.addEventListener("click", () => {
    if (exportButton.disabled) return;
    const draft = {...analysisResult, generated_at:new Date().toISOString()};
    const url = URL.createObjectURL(new Blob([JSON.stringify(draft, null, 2)], {type: "application/json"})); const link = node("a"); link.href = url; link.download = `vector-analysis-${item.code}-${vectorDraft.end}.json`; document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  const rail = $("#liveInspector"); rail.replaceChildren(node("p", "VECTOR ANALYSIS", "eyebrow"), node("h2", "트레이딩 벡터"), node("p", "종목별 관련 요인은 기간과 시장 환경에 따라 달라질 수 있습니다.", "helper"), node("p", "선택 구간 전체를 달력 일봉·주봉·월봉의 종가 추이로 각각 비교합니다. 종가 추이 수준 및 봉 간 수익률 상관도를 구분하며, 보조 일별 예측 분석은 별도로 표시합니다.", "helper"), node("p", "2026년만으로 계절성·분기 안정성을 검증하기에는 표본이 부족합니다. 과거 이력 확장과 시간순 검증이 필요합니다.", "helper"), node("p", "관계 0은 약한 관측 관계입니다. 데이터 부족·미검증은 별도 상태로 표시합니다.", "helper"));
  function update() {
    vectorDraft.selected=selectableDraft(vectorDraft.selected);
    attributeSave.disabled=true;attributeSave.onclick=null;attributeView.replaceChildren();
    saveSelection();
    for (const group of groups) for (const entry of group.entries) entry.check.checked = vectorDraft.selected.has(entry.id);
    const activeGroups = groups.filter(group => group.entries.some(entry => vectorDraft.selected.has(entry.id)));
    summary.textContent = `${vectorDraft.selected.size}개 차원 선택 · ${activeGroups.map(group => group.name).join(" + ") || "선택 전"}`;
    if(blockedSelectionNames.size)summary.textContent+=` · 분석 조합에서 제외: ${[...blockedSelectionNames].join(", ")} (미연결·데이터 없음·일별 미지원). 기존 종목 저장 속성은 변경하지 않습니다.`;
    selectedArea.replaceChildren();
    for (const id of vectorDraft.selected) { const chip = node("button", `${id.split(":")[1]} ×`, "secondary"); chip.type = "button"; chip.setAttribute("aria-label", `${id.split(":")[1]} 선택 해제`); chip.addEventListener("click", () => { vectorDraft.selected.delete(id); update(); }); selectedArea.append(chip); }
    shape.textContent = `${vectorDraft.selected.size}개 차원 × ${vectorDraft.horizons.length}개 봉 단위 · 조회 기간 전체의 종가 추이 별도 비교`;
    const dateOK = start.checkValidity() && end.checkValidity() && vectorDraft.start <= vectorDraft.end;
    ++forecastOverviewVersion;if(stockPollTimer)clearInterval(stockPollTimer);stockPollTimer=null;const waiting=node("p","선택 조건의 예측 요약 확인·계산 중…","analysis-loading");waiting.prepend(node("span",undefined,"spinner"));topForecast.setAttribute("aria-busy","true");topForecast.replaceChildren(waiting);
    validation.textContent = !dateOK ? "사용 가능한 마감일 이전의 날짜와 시작일 ≤ 종료일을 확인하세요." : !vectorDraft.horizons.length ? "변화 기간을 하나 이상 선택하세요." : vectorDraft.selected.size > 20 ? "혼합 벡터는 최대 20개 요인까지 선택하세요." : `자료 마감 ${yesterday} · 예측 대상 ${forecastTiming.target_date}. 장 마감 후 오늘 확정 자료로 다음 거래일을 추정하며 미수집 시 추정 불가로 표시합니다.`;
    const ready=item && dateOK && vectorDraft.horizons.length && vectorDraft.selected.size && vectorDraft.selected.size<=20;
    run.disabled=!ready;exportButton.disabled=true;refreshStock.disabled=!item || state.user.role === "viewer";
    clearTimeout(analysisTimer);const version=++requestVersion;analysisResult=null;charts.replaceChildren();resultArea.replaceChildren();
    readiness.textContent=ready?"자동 분석 대기 · 마지막 변경 약 0.3초 후 시작합니다…":"분석 대기 · 종목·유효한 기간·1~20개 차원·봉 단위를 선택하세요.";
    attributeNote.textContent=state.user.role==="viewer"?"저장 불가 · 조회 전용 사용자입니다.":ready?"저장 대기 · 자동 분석 완료 후 유효한 결과가 있으면 활성화됩니다.":"저장 불가 · 분석 조건을 먼저 선택하세요.";
    if(ready)analysisTimer=setTimeout(()=>analyzeNow(version),300);
    legend.textContent = vectorDraft.scale === "5" ? "5단계: ++ 강한 양 · + 양 · 0 약한 관계 · − 음 · −− 강한 음 / 미검증 별도" : "9단계: +4 · +3 · +2 · +1 · 0 · −1 · −2 · −3 · −4 / 미검증 별도";
  }
  for (const [input, key] of [[start, "start"], [end, "end"]]) input.addEventListener("change", () => { vectorDraft[key] = input.value;if(key==="end"){vectorDraft.endMode="fixed";rollingEnd.checked=false;} update(); });
  search.addEventListener("input", () => { const term = search.value.trim().toLocaleLowerCase(); for (const group of groups) { let matches = 0; for (const entry of group.entries) { const visible = !term || entry.label.toLocaleLowerCase().includes(term) || group.name.includes(term); entry.field.hidden = !visible; if (visible) matches++; } group.group.hidden = !matches; if (term && matches) group.group.open = true; } });
  update();
  attributesReady=true;
  if(persisted.latest_result?.result?.schema_version<6)persisted.latest_result=null;
  const previous=persisted.latest_result;if(previous?.result?.schema_version>=4&&previous.result.horizon_results){const request=previous.result.request,ids=[...vectorDraft.selected].map(key=>activeCatalog.items.find(d=>d.id===key||`${d.family}:${d.name}`===key)?.id).sort();if(previous.result.today===forecastTiming.target_date&&request.code===item?.code&&request.start===vectorDraft.start&&request.end===vectorDraft.end&&JSON.stringify([...request.dimensions].sort())===JSON.stringify(ids)&&JSON.stringify(request.horizons)===JSON.stringify(vectorDraft.horizons)&&request.window===Number(vectorDraft.window)&&request.levels===Number(vectorDraft.scale)){analysisResult={...previous.result,persistence:{saved:true,reused:true,id:previous.id,created_at:previous.created_at}};showResult(analysisResult);readiness.textContent=`DB 저장 결과 복원 · ${previous.result.data_cutoff} 마감 · 신규 자료만 확인하며 동일 입력은 재계산하지 않습니다.`;}}
  loadHistory().catch(error=>{if(valid())storageStatus.textContent=error.message;});
}

async function renderAnalysis() {
  stopStockPolling();
  $("#liveInspector").replaceChildren();updateScreenHelp();
  const area = $("#liveContent"); area.replaceChildren();
  const tabs = node("div", undefined, "admin-tabs"); tabs.setAttribute("role", "tablist"); area.append(tabs);
  for (const [key, label] of [["detail", "종목 상세"], ["vectors", "트레이딩 벡터"], ["sources", "뉴스 및 통계 분석"]]) {
    const button = node("button", label, "admin-tab"); button.setAttribute("role", "tab"); button.setAttribute("aria-selected", String(state.analysisTab === key));
    button.addEventListener("click", () => { state.analysisTab = key; renderAnalysis().catch(reportError); }); tabs.append(button);
  }
  if (state.analysisTab === "detail") await renderStockDetail(area);
  else if (state.analysisTab === "dimensions") { openView("data"); return; }
  else if (state.analysisTab === "vectors") await renderVectorAnalysis(area);
  else { const section = panel("공시·뉴스 및 통계 분석 · 최근 2~3주 종목 영향 평가"); area.append(section); section.append(node("p", "기본 목록에서 시작하는 내 뉴스 및 통계 분석입니다. 추가·수정·사용 중지는 본인 목록에만 적용됩니다.", "helper")); const rail = $("#liveInspector"); rail.replaceChildren(node("p", "DETAILS", "eyebrow"), node("h2", "뉴스 및 통계 분석"), node("p", "최근 2~3주에 게시된 공개 피드를 수집하며 출처·근거·모델·평가 이력을 DB에 보존합니다. 후보 등록은 평가 사용 승인이 아닙니다.", "helper")); await renderDailyNews(section); await renderCatalog(section); }
}

function factorProfileView(profile){
  const area=node("details",undefined,"factor-profile");area.append(node("summary","종목 속성 · 트레이딩 벡터 연관 요인 — 펼쳐보기"));if(!profile||profile.status==="missing"){area.append(node("p",profile?.note||"트레이딩 벡터 분석 후 연관 요인이 저장됩니다.","helper"));return area;}
  if(profile.origin==="llm_default"){area.append(node("p",`사업 설명 기반 기본 속성 · ${profile.status} · ${profile.generation_method==="reviewed_business_rules"?"검토된 사업 매핑 · LLM 미호출":profile.model?.model||"내 기본 모델"}`,"helper"),node("p",(profile.proposed_dimensions||[]).map(item=>item.name).join(" · ")||"제안된 차원 없음"),node("p",profile.reason||profile.note||"기본 제안 대기","helper"),node("p","미검증 초기 탐색 제안입니다. 관측 상관도·기업 사업 노출을 의미하지 않습니다. 트레이딩 벡터에서 검토 후 수동 저장하면 이 기본값이 교체됩니다.","warning"));return area;}
  area.append(node("p",`자료 마감 ${profile.data_cutoff} · 벡터 결과 ${profile.result_id} · ${profile.status||"저장됨"}`,"helper"),node("p",profile.rule,"helper"));const wrap=node("div",undefined,"table-wrap"),table=node("table"),head=node("tr");for(const label of ["요인 / 변화 기간","관계 방향","Pearson / Spearman / Kendall","평균 절댓값 / 표본","출처"])head.append(node("th",label));table.append(head);for(const factor of profile.factors){const row=node("tr");row.append(node("td",`${factor.name} · ${factor.horizon}일`),node("td",factor.direction==="positive"?"양의 관계":factor.direction==="negative"?"음의 관계":"방향 혼재·중립"),node("td",["pearson","spearman","kendall"].map(method=>factor.correlations[method]==null?"—":factor.correlations[method].toFixed(4)).join(" / ")),node("td",`${factor.strength==null?"—":factor.strength.toFixed(4)} / ${factor.samples}쌍`));const cell=node("td",factor.source||"출처 미확인");if(factor.source_url){const link=node("a","원천 설명");link.href=factor.source_url;link.target="_blank";link.rel="noopener noreferrer";cell.append(link);}row.append(cell);table.append(row);}wrap.append(table);area.append(wrap);if(!profile.factors.length)area.append(node("p","현재 기준에 맞는 신선한 연관 요인이 없습니다. 관계가 없다고 확정한 것은 아닙니다.","helper"));area.append(node("p",profile.scope,"helper"),node("p",profile.warning,"warning"));return area;
}

function newsSourceSummary(source){return source.kind==="statistics"?`통계 ${source.stored_count||0}건 · 평가 비대상`:`${source.today_count}건 / ${source.score_sum==null?"미평가":source.score_sum}`;}
function impactTotalLabel(data){return data.total_sum!=null?data.total_sum:data.stock_assessment_count?`산정 불가 · LLM 판단 불가 ${data.stock_uncertain_count}건`:"미평가";}
function renderArticleImpacts(data){
  const area=node("div",undefined,"table-wrap"),table=node("table"),head=node("tr");
  const total=node("div",undefined,"news-impact-total"),score=data.total_sum;
  total.setAttribute("role","status");total.append(node("span","선택 종목 뉴스 영향 합산"),node("strong",score==null?"산정 전 / 판단 불가":`${score>0?"+":""}${score}점`,score>0?"price-up":score<0?"price-down":""),node("small",`${data.levels}단계 평가 · 점수 산정 ${data.material_counts?.scored||0}건 · 평가 대기 ${Math.max(0,(data.analysis_pool_count||0)-(data.material_counts?.reviewed||0))}건 · 평가 완료 중 점수 제외 ${Math.max(0,(data.material_counts?.reviewed||0)-(data.material_counts?.scored||0))}건 · 뉴스 방향성 점수이며 수익률이 아닙니다.`));area.append(total);
  for(const label of ["게시일 / 종류","내용 요약 / 관련성","종목 영향 점수","LLM 판단 근거"])head.append(node("th",label));table.append(head);
  const impactGroup=item=>{const score=item.assessment?.score;return score==null?3:score>0?0:score<0?1:2;};
  const orderedImpacts=[...(data.article_impacts||[])].sort((a,b)=>impactGroup(a)-impactGroup(b)||Math.abs(b.assessment?.score||0)-Math.abs(a.assessment?.score||0)||String(b.article.published_at).localeCompare(String(a.article.published_at)));
  for(const item of orderedImpacts){
    const row=node("tr"),article=item.article,assessment=item.assessment,cell=node("td");
    const excerpt=(article.excerpt||"").replace(/\s+/g," ").trim(),reviewSummary=assessment?.reason?.match(/^.*?[.!?](?:\s|$)/)?.[0]?.trim();
    const summary=article.summary_ko||reviewSummary||excerpt||article.title,link=node("a",summary.length>260?`${summary.slice(0,260)}…`:summary,"news-summary-link");link.href=article.url;link.target="_blank";link.rel="noopener noreferrer";link.title=`${article.title} · 클릭하면 원문을 새 탭에서 엽니다`;
    cell.append(node("small",article.summary_ko?"한글 번역 요약":reviewSummary?"LLM 판단 근거 요약":excerpt?"공개 피드 발췌 요약":"요약 미제공 · 원문 제목"));
    cell.append(link,node("small",assessment?`상태: ${{direct:"직접 관련",industry:"산업 관련",uncertain:"관련성 불확실",unrelated:"무관"}[assessment.relevance]||"평가 완료"}`:"상태: 평가 대기 · 관련성 미확정"));
    row.append(node("td",`${formatTime(article.published_at)} · ${article.content_type==="disclosure"?"공시":"뉴스"}`),cell,node("td",assessment?assessment.score==null?"판단 불가":`${assessment.score>0?"+":""}${assessment.score}점`:"평가 대기"),node("td",assessment?`${assessment.reason} · 근거: ${assessment.evidence} · 모델: ${assessment.model}`:"LLM 평가 전 · 관련성 및 영향 점수 미확정 · 합산 제외"));table.append(row);
  }
  area.append(node("h3",`공시·뉴스 ${data.article_impacts?.length||0}건 · 평가 대기·불확실 포함`),node("p",data.coverage?.note||"","warning"),table);return area;
}
async function renderDailyNews(section){
  const stockChoice=node("select"),business=node("textarea"),analyzeNews=node("button","LLM 관련성·종목 영향 평가 (최대 5건씩)","primary");stockChoice.setAttribute("aria-label","공시·뉴스 분석 관심·보유종목");business.setAttribute("aria-label","뉴스 분석 종목 사업 정보");business.readOnly=true;business.placeholder="저장된 최신 사업설명을 사용합니다. 설명 변경은 설정의 사업 설명 조사에서 진행하세요.";const common=node("option","공통 섹터·원자재 평가 보기 (종목 선택 전)");common.value="";stockChoice.append(common);for(const item of (await api("/vectors/instruments")).items){const option=node("option",`${item.name} · ${item.code}`);option.value=item.code;stockChoice.append(option);}stockChoice.value=state.newsCode||stockChoice.options[1]?.value||"";
const modelPanel=panel("2. 저장 자료 분석 · 종목과 전용 모델 선택"),modelSelect=node("select"),modelRefresh=node("button","모델 목록 새로고침","secondary"),modelNote=node("p","","helper");modelPanel.classList.add("news-model-panel");modelSelect.setAttribute("aria-label","뉴스 평가 전용 모델");modelPanel.append(node("p","아래 버튼으로 저장된 사업설명에서 수집 계획을 만들고 승인된 출처의 뉴스·공시·통계를 증분 갱신·평가합니다. 종목 변경은 저장 결과만 다시 읽으며 유료 분석을 자동 실행하지 않습니다.","helper"),stockChoice,business,modelSelect,modelRefresh,analyzeNews,modelNote);section.append(modelPanel);let modelCatalog=null;
  const stockRow=node("div",undefined,"news-stock-row");stockChoice.before(stockRow);stockRow.append(stockChoice);
  const newsMembership=loadWatchMembership(),newsWatch=node("span");stockRow.append(newsWatch);
  function updateNewsWatch(){const code=stockChoice.value;newsWatch.replaceChildren();if(code)newsWatch.append(watchlistAddControl({code,name:stockChoice.selectedOptions[0]?.textContent||code},newsMembership,()=>stockChoice.value===code));}
  stockChoice.addEventListener("change",updateNewsWatch);updateNewsWatch();
  let businessWidth=0;const businessResize=new ResizeObserver(entries=>{if(!business.isConnected){businessResize.disconnect();return;}const width=entries[0].contentRect.width;if(width!==businessWidth){businessWidth=width;resizeBusinessDescription(business);}});businessResize.observe(modelPanel);
  const financialArea=node("section",undefined,"news-financials");financialArea.setAttribute("aria-label","공시 재무상태 및 추이");financialArea.append(node("h3","공시 재무상태 및 추이 · 최근 6개월"),node("p","재무 수치 미수집 · 현재 KIND RSS·계획 검색은 공시 제목·시각·링크·제공 발췌를 수집합니다. 공시 재무제표 수집기는 아직 연결되지 않아 매출·이익·자산·부채와 추이 그래프를 표시할 수 없습니다.","helper"),node("p","연결 시 표시할 항목: 매출액 · 영업이익 · 순이익 · 자산 · 부채 · 자본 · 영업현금흐름. 연결/별도, 분기/누적, 회계기간·공시일·정정 여부를 구분합니다.","helper"));const financialSource=node("a","금융감독원 OpenDART 재무제표 API 안내","news-summary-link");financialSource.href="https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS003&apiId=2019020";financialSource.target="_blank";financialSource.rel="noopener noreferrer";financialArea.append(financialSource,node("p","다음 연결 요건: OpenDART 인증키와 종목코드→공시 고유번호 매핑. 뉴스 제목이나 LLM 추정으로 재무 수치를 채우지 않습니다.","helper"));business.after(financialArea);
  const translateNews=node("button","영문 뉴스 한글 번역 (최대 10건)","secondary");modelPanel.append(translateNews);translateNews.disabled=state.user.role==="viewer";
  translateNews.addEventListener("click",async()=>{const chosen=modelSelect.value==="default"?modelCatalog.default:modelCatalog.items.find(item=>item.id===modelSelect.value),cloud=chosen?.provider==="openai";if(cloud&&!confirm("저장된 공개 뉴스 제목·발췌문을 선택한 클라우드 모델로 보내 번역합니다. 비용이 발생할 수 있습니다. 실행할까요?"))return;translateNews.disabled=true;try{const result=await llmButtonTask(translateNews,"뉴스 번역 중…",()=>panelArea.isConnected,call=>call("/news/translate",{method:"POST",body:JSON.stringify({model:modelSelect.value,cloud_confirmed:cloud})}));await load();message.textContent=`한글 번역 ${result.translated}건 DB 저장 · 미번역 ${result.pending}건 · 종목 점수 변경 없음`;}catch(error){reportError(error);}finally{translateNews.disabled=state.user.role==="viewer";}});
  async function loadModels(refresh=false){modelRefresh.disabled=true;try{modelCatalog=!refresh&&state.newsModelCatalog&&Date.now()-state.newsModelCatalogAt<60000?state.newsModelCatalog:await api(`/news/models?refresh=${refresh}`);state.newsModelCatalog=modelCatalog;state.newsModelCatalogAt=Date.now();modelSelect.replaceChildren();const base=node("option",`내 기본 · ${modelCatalog.default?.label||"미설정"}`);base.value="default";modelSelect.append(base);for(const item of modelCatalog.items){const option=node("option",item.label);option.value=item.id;modelSelect.append(option);}if(![...modelSelect.options].some(option=>option.value===modelCatalog.selected)){const unavailable=node("option",`${modelCatalog.selected} · 목록 확인 필요`);unavailable.value=modelCatalog.selected;modelSelect.append(unavailable);}modelSelect.value=modelCatalog.selected;modelSelect.disabled=state.user.role==="viewer";modelNote.textContent=`${modelCatalog.cloud_enabled?"":"클라우드 사용 꺼짐 · 유료 LLM 평가 전 설정에서 사용 허용 필요. "}${modelCatalog.cloud_error||modelCatalog.error||modelCatalog.default_source} · 허용된 모델만 선택 가능. GPT 5.6 이상은 계정 API 모델 목록에서 표시합니다. 선택 변경은 기존 평가를 재실행하지 않습니다.`;}finally{modelRefresh.disabled=false;}}
  modelRefresh.addEventListener("click",()=>loadModels(true).catch(reportError));modelSelect.addEventListener("change",async()=>{modelSelect.disabled=true;try{await api("/news/model",{method:"PUT",body:JSON.stringify({model:modelSelect.value})});modelNote.textContent="뉴스 전용 모델 선택 DB 저장 완료 · 다음 수집·평가부터 적용";}catch(error){reportError(error);await loadModels();}finally{modelSelect.disabled=state.user.role==="viewer";}});await loadModels();
  const panelArea=panel("최근 2~3주 뉴스·정보 · 섹터/원자재 영향 평가");panelArea.id="dailyNews";section.append(panelArea);const message=node("p","DB 저장 평가 조회 중…","helper");message.setAttribute("role","status");panelArea.append(message);
  const controls=node("div",undefined,"source-row"), scale=node("select"),filter=node("select"),collect=node("button","선택 공개 피드 수집·로컬 AI 평가","secondary"),reload=node("button","DB 평가 새로 조회","secondary");scale.setAttribute("aria-label","뉴스 평가 단계");filter.setAttribute("aria-label","뉴스 평가 차원 유형");for(const [value,label] of [[5,"5단계 · -2 ~ +2"],[9,"9단계 · -4 ~ +4"]]){const option=node("option",label);option.value=value;scale.append(option);}for(const [value,label]of [["all","섹터·원자재 전체"],["sector","섹터"],["commodity","원자재"]]){const option=node("option",label);option.value=value;filter.append(option);}const windowSelect=node("select");windowSelect.setAttribute("aria-label","뉴스 평가 기간");for(const [days,label] of [[14,"최근 2주"],[21,"최근 3주"]]){const option=node("option",label);option.value=days;windowSelect.append(option);}windowSelect.disabled=state.user.role==="viewer";windowSelect.addEventListener("change",async()=>{await api("/news/window",{method:"PUT",body:JSON.stringify({days:Number(windowSelect.value)})});await load();});controls.append(windowSelect,scale,filter,analyzeNews,reload,node("small","뉴스 기본 2주 · 자료가 적으면 3주로 조정 · 재무정보 6개월과 별도","helper"));panelArea.append(controls);const summary=node("div"),scores=node("div"),details=node("details"),sourceArea=node("div");details.append(node("summary","출처별 수집 가능한 정보·최근 2~3주 점수·사용 승인"),sourceArea);panelArea.append(summary,scores,details);let busy=false,current=null;
  const collectionPanel=panel("1. 저장 원천·수집 상태");collectionPanel.id="newsCollectionStep";modelPanel.id="newsAnalysisStep";
  const collectionHint=node("p","아래에서 출처 사용을 승인하면 LLM 수집 계획의 후보가 됩니다. 실제 선택한 출처·검색어·통계는 저장된 계획에서 확인하세요.","helper");collectionHint.id="newsCollectStatus";
  collectionPanel.append(node("p","원천은 데이터 수집 화면의 저장 계획과 독립 수집기가 준비합니다. 여기서는 저장 원문·통계를 가져와 종목별 영향만 평가합니다. 자료가 부족하면 수집 상태를 먼저 확인하세요.","helper"),message,details,collectionHint);
  panelArea.insertBefore(collectionPanel,controls);panelArea.insertBefore(modelPanel,controls);const scoreDetails=node("details");scoreDetails.append(node("summary","차원별 합산표·개별 뉴스 근거 펼치기"),scores);panelArea.append(scoreDetails);
  function buttons(){analyzeNews.disabled=busy||state.user.role==="viewer"||!stockChoice.value||!current?.enabled||!current?.sources.some(source=>source.eligible||(source.kind==="statistics"&&source.statistics_connected&&source.status==="active"&&source.disposition==="use"));stockChoice.disabled=busy;business.disabled=busy;collectionHint.textContent=busy?"저장 자료의 변경·미평가 항목 평가 중입니다.":"원천 수집은 데이터 수집 화면에서 관리합니다. 이 화면은 저장 자료만 평가하고 같은 입력의 완료 평가는 재사용합니다.";translateNews.disabled=busy||state.user.role==="viewer";modelRefresh.disabled=busy;modelSelect.disabled=busy||state.user.role==="viewer";windowSelect.disabled=busy||state.user.role==="viewer";reload.disabled=busy;scale.disabled=busy;if(current&&!summary.querySelector(".news-failure-note")){for(const result of current.runs[0]?.sources||[]){if(result.status === "failed")summary.append(node("p",`${current.sources.find(source=>source.id===result.id)?.name||result.id} 수집 실패${result.http_status?` · HTTP ${result.http_status}`:""} · ${result.error}`,"warning news-failure-note"));}}}
  function draw(data){requestAnimationFrame(()=>{if(business.isConnected)resizeBusinessDescription(business);});current=data;state.dailyNews=data;windowSelect.value=data.window_days||14;business.value=data.instrument?.business_context||data.stock_context||"";modelPanel.querySelector(".factor-profile")?.remove();if(data.instrument)modelPanel.insertBefore(factorProfileView(data.instrument.trading_factor_profile),modelSelect);summary.replaceChildren(node("p",data.instrument?`${data.instrument.name} · ${data.instrument.code} · 종목 순영향 합계 (섹터·원자재 점수는 별도)`:"공통 차원 평가 · 종목별 결과와 구분","helper"),node("p",`${data.start_date || data.date} ~ ${data.end_date || data.date} · ${data.timezone} · 최근 ${data.window_days||14}일 점수 합산 ${impactTotalLabel(data)} · ${data.articles.length}개 자료 표시 (대기·불확실 포함)`,"stock-heading"),node("p",data.note,"helper"));if(data.material_counts){const c=data.material_counts;summary.append(node("p",`기간 내 전체 저장 ${c.stored}건 · 선택 종목 평가 완료 ${c.reviewed}건 · 무관 ${c.unrelated}건 · 관련성 불확실 ${c.uncertain}건 · 점수 산정 ${c.scored}건`,"helper"));}if(data.instrument)summary.append(renderArticleImpacts(data));if(data.instrument&&data.collection_workflow)summary.append(collectionWorkflowView(data.collection_workflow,data.instrument.code));if(!data.enabled)summary.append(node("p","뉴스 실행이 꺼져 있습니다. TRADING_NEWS_ENABLED 설정이 필요합니다.","warning"));if(!data.local_model_configured)summary.append(node("p","로컬 모델 연결 설정이 없습니다. 수집 근거는 보존하고 모델 평가 실패를 별도 표시합니다.","warning"));for(const run of data.runs.slice(0,3))summary.append(node("p",`실행 ${formatTime(run.created_at)} · ${run.status}${run.error?` · ${run.error}`:""}${run.pending_articles?` · 평가 대기 ${run.pending_articles}건`:""}`,"helper"));
    scores.replaceChildren();const table=node("table"),head=node("tr");for(const label of ["섹터/원자재","관련 자료 / 평가 / 판단불가","점수 합산","평균"] )head.append(node("th",label));table.append(head);for(const item of data.aggregate.filter(item=>filter.value === "all"||item.family === filter.value)){const row=node("tr");for(const text of [item.name,`${item.articles} / ${item.evaluated} / ${item.uncertain}`,item.sum==null?"정보 없음·미평가":String(item.sum),item.mean==null?"—":item.mean.toFixed(2)])row.append(node("td",text));table.append(row);}const wrap=node("div",undefined,"table-wrap");wrap.append(table);scores.append(wrap);
    const evidence=node("details");evidence.append(node("summary",`개별 뉴스 평가·근거 ${data.assessments.length}건`));for(const item of data.assessments){if(filter.value!=="all"&&item.family!==filter.value)continue;const card=node("div",undefined,"panel"),link=node("a",item.article.title);link.href=item.url||item.article.url;link.target="_blank";link.rel="noopener noreferrer";card.append(link,node("p",`${formatTime(item.article.published_at)} · ${item.dimension} · ${item.score==null?"판단 불가":item.score}점`),node("p",item.reason),node("p",`근거: ${item.evidence}`,"helper"),node("p",`모델 ${item.model} · 실행 ${item.run_id}`,"helper"));evidence.append(card);}scores.append(evidence);const raw=node("details");raw.append(node("summary",`저장된 최근 2~3주 자료 ${data.articles.length}건 · 미평가 포함`));for(const item of data.articles){const link=node("a",`${formatTime(item.published_at)} · ${item.title}`);link.href=item.url;link.target="_blank";link.rel="noopener noreferrer";const paragraph=node("p");paragraph.append(link);raw.append(paragraph);}scores.append(raw);
    sourceArea.replaceChildren();const sourceTable=node("table"),sourceHead=node("tr");for(const label of ["출처","얻을 정보 / 연결 상태","최근 2~3주 자료","점수 합산","평가 사용"])sourceHead.append(node("th",label));sourceTable.append(sourceHead);for(const source of data.sources.filter(item=>item.kind!=="statistics"&&item.connected)){const row=node("tr"),name=node("td"),link=node("a",source.name);link.href=source.reference_url;link.target="_blank";link.rel="noopener noreferrer";name.append(link,node("small",source.domain));row.append(name,node("td",`${source.data} · ${source.connection}`),node("td",String(source.today_count)),node("td",source.score_sum==null?"미평가":String(source.score_sum)));const action=node("td");if(source.eligible){action.append(node("span","승인됨 · 수집 계획 후보"));}else if(source.connected&&source.status!=="retired"&&source.disposition!=="exclude"){const approve=node("button","공개 피드 뉴스 평가 사용 승인","secondary");approve.disabled=state.user.role==="viewer";approve.addEventListener("click",async()=>{if(!confirm(`${source.name}을 등록 활성·분석 사용으로 변경할까요? 공개 피드의 제목·발췌문을 로컬 AI에 보내 평가합니다.`))return;approve.disabled=true;try{await api(`/sources/${source.id}`,{method:"PUT",body:JSON.stringify({...source.catalog,version:source.version,status:"active",disposition:"use",reason:"사용자의 공개 피드 뉴스 평가 사용 승인"})});await load();}catch(error){message.textContent=error.message;approve.disabled=false;}});action.append(approve);}else action.append(node("span",source.status==="retired"||source.disposition==="exclude"?"사용 제외":"연결·이용 조건 확인 필요"));row.append(action);sourceTable.append(row);}const sourceWrap=node("div",undefined,"table-wrap");sourceWrap.append(sourceTable);sourceArea.append(sourceWrap);buttons();}
async function load(){const generation=++loadGeneration,requestedCode=stockChoice.value;if(state.dailyNews?.instrument?.code===requestedCode&&state.dailyNews.levels===Number(scale.value)){draw(state.dailyNews);message.textContent="종목 상세의 저장 결과 표시 · 재평가 없음 · DB 최신 상태 확인 중";}const data=await api(`/news/today?levels=${scale.value}${stockChoice.value?`&code=${stockChoice.value}`:""}`);if(!panelArea.isConnected||generation!==loadGeneration||requestedCode!==stockChoice.value)return;draw(data);scoreDetails.open=data.assessments.length>0;message.textContent=`저장 결과 조회 완료 · LLM 재평가 없음 · ${data.sources.filter(s=>s.connected).length}개 공개 피드 수집기 구현. 아래 출처별 목록에서 사용 승인 후 수집 대상을 선택하세요. 날짜·시각 미확인 자료는 평가 제외.`;for(const cell of document.querySelectorAll("[data-news-source-id]")){const source=data.sources.find(s=>s.id===cell.dataset.newsSourceId);if(source)cell.textContent=newsSourceSummary(source);}}
  analyzeNews.textContent="저장 자료의 변경·미평가 항목 평가 (최대 30건)";reload.textContent="저장 뉴스·평가 새로고침";
  let loadGeneration=0;stockChoice.addEventListener("change",()=>{state.newsCode=stockChoice.value;business.value="";scores.replaceChildren(node("p","선택 종목의 저장 평가 조회 중…"));load().catch(reportError);});
  scale.addEventListener("change",()=>load().catch(reportError));filter.addEventListener("change",()=>{if(current)draw(current);});reload.title="저장된 뉴스와 평가 결과만 다시 읽습니다. 뉴스 수집·LLM 평가는 실행하지 않습니다.";reload.addEventListener("click",async()=>{reload.disabled=true;reload.textContent="저장 결과 조회 중…";message.textContent="DB의 저장 뉴스·평가 결과를 다시 읽는 중입니다.";try{await load();message.textContent=`저장 결과 새로고침 완료 · ${formatTime(new Date().toISOString())} · 전체 ${current.material_counts?.stored||0}건 / 관련 표시 ${current.articles.length}건 · 뉴스 수집·LLM 평가 없음`;}catch(error){message.textContent=`새로고침 실패: ${error.message}`;reportError(error);}finally{reload.textContent="저장 뉴스·평가 새로고침";buttons();}});
  const evaluationProgress=node("div",undefined,"evaluation-progress");evaluationProgress.hidden=true;evaluationProgress.setAttribute("role","status");controls.after(evaluationProgress);
  analyzeNews.addEventListener("click",async()=>{
    if(busy||state.user.role==="viewer"||!stockChoice.value)return;
    if(!confirm("저장된 뉴스·공시·통계의 변경·미평가 항목만 평가합니다. 원천 검색은 하지 않으며 LLM 비용이 발생할 수 있습니다. 실행할까요?"))return;
    const code=stockChoice.value,ownerCurrent=llmOwnerGuard(),valid=()=>ownerCurrent()&&panelArea.isConnected&&stockChoice.value===code;
    busy=true;buttons();
    try{await newsEvaluationTask(analyzeNews,evaluationProgress,valid,async notify=>{
      const workflow=await refreshBusinessCollection(code,text=>{if(valid()){message.textContent=text;notify(text);}},false,valid);
      if(!valid())return workflow;
      notify("평가 결과를 불러오는 중…");
      await load();if(valid())message.textContent=`저장 자료 평가 ${workflow.status}${workflow.error?" · "+workflow.error:""} · 완료 단계 재사용`;
      return workflow;
    });}catch(error){if(valid())message.textContent=error.message;}
    finally{if(valid()){busy=false;buttons();}}
  });await load();await renderStatistics(section);
}

function seoulDate(){return new Intl.DateTimeFormat("en-CA",{timeZone:"Asia/Seoul",year:"numeric",month:"2-digit",day:"2-digit"}).format(new Date());}

async function renderStatistics(section){
  const owner=state.user.id;
  const area=panel("수출입·산업통계 · 수치 자료 (뉴스 점수 제외)");area.id="sourceStatistics";section.append(area);const controls=node("div",undefined,"source-row"),select=node("select"),start=node("input"),end=node("input"),refresh=node("button","선택 통계 원천 새로 조회","secondary"),reload=node("button","통계 DB 새로고침","secondary"),note=node("p","","helper"),output=node("div");select.setAttribute("aria-label","통계 지표");start.type=end.type="date";start.value="2020-01-01";end.value=seoulDate();start.setAttribute("aria-label","통계 시작일");end.setAttribute("aria-label","통계 종료일");controls.append(select,start,end,refresh,reload);area.append(note,controls,output);let data;
  function draw(){output.replaceChildren();const series=data.series.find(item=>item.id===select.value);if(series){const link=node("a",`${series.source} · ${series.id} · ${series.frequency} · ${series.unit}`);link.href=series.url;link.target="_blank";link.rel="noopener noreferrer";output.append(link,node("p",`최종 수집 ${series.collected_at?formatTime(series.collected_at):"없음"} · 관측 기간과 공개 시점은 다릅니다.`,"helper"));const points=series.points.filter(p=>p.date>=start.value&&p.date<=end.value);const graph=dimensionChart(points,series,false);graph.querySelector("svg")?.setAttribute("aria-label",`${series.name} 관측 주기별 추이 ${points.length}건`);output.append(graph);const details=node("details");details.append(node("summary",`관측값 표 ${points.length}건`));const table=node("table"),head=node("tr");head.append(node("th","관측 기간"),node("th",series.unit));table.append(head);for(const point of [...points].reverse()){const row=node("tr");row.append(node("td",point.date.slice(0,7)),node("td",point.value.toLocaleString("ko-KR")));table.append(row);}details.append(table);output.append(details);}
    if(series?.scope)output.prepend(node("p",`${series.scope} 최신 관측월 ${series.points.at(-1)?.date.slice(0,7)||"미수집"}${series.release_date?` · 원천 표 공개일 ${series.release_date} (개별 관측값의 최초 공개일과 다름)`:" · 개별 관측값의 최초 공개일 미확인"}`,"helper"));
    const details=node("details");details.append(node("summary",`수출입·통계 출처 ${data.sources.length}개 · 연결 요건`));const table=node("table"),head=node("tr");head.append(node("th","출처"),node("th","제공 정보·연결 요건"),node("th","평가"));table.append(head);for(const source of data.sources){const row=node("tr"),cell=node("td"),link=node("a",source.name);link.href=source.reference_url;link.target="_blank";link.rel="noopener noreferrer";cell.append(link);row.append(cell,node("td",source.data),node("td",`비대상 · ${source.statistics_connected ? "통계 수집기 연결됨" : "별도 통계 API 연결 필요"}`));table.append(row);}details.append(table);output.append(details);refresh.disabled=state.user.role==="viewer"||state.view!=="data";
  }
  const valid=()=>area.isConnected&&state.user?.id===owner;
  let busy=false;
  function buttons(){for(const input of [select,start,end,reload])input.disabled=busy;refresh.disabled=busy||state.user.role==="viewer"||state.view!=="data"||!select.value;}
  async function load(){const result=await api("/statistics");if(!valid())return;data=result;const previous=select.value;select.replaceChildren();for(const item of data.series){const option=node("option",`${item.name} · ${item.points.length}건`);option.value=item.id;select.append(option);}const chosen=data.series.find(item=>item.id===previous)||data.series.find(item=>item.points.length)||data.series[0];if(chosen)select.value=chosen.id;note.textContent=data.note;draw();}
  async function run(button,collect=false){if(busy||!valid())return;busy=true;buttons();try{await buttonTask(button,collect?"통계 원천 수집 중…":"저장 통계 조회 중…",async()=>{note.textContent=collect?"공식 통계 원천 조회 중… (LLM 호출 없음)":"저장 통계 조회 중…";if(collect)await api(`/statistics/${select.value}/refresh`,{method:"POST"});if(valid())await load();});}catch(error){if(valid())note.textContent=error.message;}finally{if(valid()){busy=false;buttons();}}}
  for(const input of [select,start,end])input.addEventListener("change",draw);reload.addEventListener("click",()=>run(reload));refresh.addEventListener("click",()=>run(refresh,true));await run(reload);
}

function holdingPurchaseReturn(item,price){return Number.isFinite(item.quantity)&&item.quantity>0&&Number.isFinite(item.purchase_price)&&item.purchase_price>0&&Number.isFinite(price)&&price>0?(price/item.purchase_price-1)*100:null;}
function paintHoldingPurchaseReturn(cell,item,price){const rate=holdingPurchaseReturn(item,price);cell.replaceChildren(node("strong",rate==null?"—":movementText(rate,"%"),movementClass(rate)),node("small",item.purchase_price>0?`평균매입가 ${priceText(item.purchase_price)}`:"평균매입가 확인 필요"),node("small","수수료·세금 제외"));}
async function renderHoldings() {
  $("#liveInspector").replaceChildren();updateScreenHelp();
  const area = $("#liveContent"); area.replaceChildren();
  const tabs = node("div", undefined, "admin-tabs"); tabs.setAttribute("role", "tablist"); tabs.setAttribute("aria-label", "보유종목 화면"); area.append(tabs);
  for (const [key, label] of [["positions", "내 보유종목"], ["returns", "기간별 수익률"]]) {
    const button = node("button", label, "admin-tab"); button.type = "button"; button.setAttribute("role", "tab"); button.setAttribute("aria-selected", String(state.holdingsTab === key));
    button.addEventListener("click", () => { if (state.holdingsTab !== key) { state.holdingsTab = key; renderHoldings().catch(reportError); } }); tabs.append(button);
  }
  if (state.holdingsTab === "returns") { await renderPeriodReturns(area); return; }
  const section = panel("내 보유종목"); area.append(section);
  const brokerField=node("label","증권사 "),brokerChoice=node("select");brokerChoice.setAttribute("aria-label","보유종목 증권사");for(const [id,label] of [["namuh","나무증권"],["kiwoom","영웅문 · 키움증권"]]){const option=node("option",label);option.value=id;brokerChoice.append(option);}brokerChoice.value=state.holdingsBroker||"namuh";brokerField.append(brokerChoice);section.append(brokerField);const isKiwoom=brokerChoice.value==="kiwoom";brokerChoice.addEventListener("change",()=>{state.holdingsBroker=brokerChoice.value;renderHoldings().catch(reportError);});
  section.append(node("p", isKiwoom?"영웅문 연결 계좌의 국내 주식 잔고 읽기 전용 조회입니다. 주문·매매는 실행하지 않습니다.":"NH PLUG 계좌 잔고 읽기 전용 조회입니다. 주문·매매는 실행하지 않습니다. 계좌 선택 목록에는 유형과 전체 계좌번호를 표시하며, 잔고 화면과 DB 기록에는 끝 4자리만 남깁니다.", "helper"));
  const status = node("p", "저장된 잔고 확인 중…", "helper"); status.setAttribute("role", "status"); section.append(status);
  const controls = node("div", undefined, "source-row"); const loadAccounts = node("button", "계좌 목록 확인", "secondary"); loadAccounts.disabled = state.user.role === "viewer";
  const select = node("select"); select.setAttribute("aria-label", "조회할 나무증권 계좌"); select.hidden = true;
  const refresh = node("button", "보유종목 새로 조회", "primary"); refresh.hidden = true; controls.append(loadAccounts, select, refresh); section.append(controls);
  const output = node("div"); section.append(output);
  const owner=state.user.id;let lastSnapshot=null,refreshing=false;const valid=()=>section.isConnected&&state.view==="holdings"&&state.user?.id===owner;refresh.remove();
  if(isKiwoom){loadAccounts.hidden=true;refresh.hidden=false;section.append(node("p","영웅문은 관리자 연결 키에 연결된 계좌를 조회합니다. 읽기 전용이며 주문을 실행하지 않습니다.","helper"));}
  function display(snapshot) {
    if(!valid())return;lastSnapshot=snapshot;output.replaceChildren();
    status.textContent = snapshot ? `${snapshot.environment === "mock" ? "모의" : "실전"} · 계좌 끝자리 ${snapshot.account_last4} · ${formatTime(snapshot.collected_at)}` : "조회된 보유종목이 없습니다. 계좌 목록 확인 후 조회하세요.";
    const rail = $("#liveInspector"); rail.replaceChildren(node("p", "DETAILS", "eyebrow"), node("h2", "보유종목 조회"), node("p", snapshot ? `계좌 끝자리 ${snapshot.account_last4} · ${snapshot.total}종목` : "조회된 계좌 잔고가 없습니다.", "helper"), node("p", snapshot ? `출처 ${snapshot.source} · ${formatTime(snapshot.collected_at)}` : "나무 연결 후 계좌 목록을 확인하세요.", "helper"), node("p", "이 화면은 읽기 전용이며 주문을 실행하지 않습니다.", "warning"));
    if (!snapshot) return;
    if (snapshot.partial) output.append(node("p", "조회 결과에 다음 페이지가 있어 일부 보유종목만 표시합니다. 합계로 판단하지 마세요.", "warning"));
    const cards = node("div", undefined, "price-grid"); for (const [label, value, suffix, moving] of [["평가금액", snapshot.valuation, "원", false], ["평가손익", snapshot.profit, "원", true], ["수익률", snapshot.profit_rate, "%", true]]) { const card = node("div", undefined, "metric-card"); card.append(node("small", label), node("strong", moving ? movementText(value, suffix) : priceText(value, suffix), moving ? movementClass(value) : "")); cards.append(card); } output.append(cards);
    const table = node("table"); const head = node("thead"); const row = node("tr"); ["종목", "보유수량", "현재가", "매입 대비 수익률", "평가금액", "손익", "저장 예측 · 기간 통합", "뉴스 평가", "동작"].forEach(label => row.append(node("th", label))); head.append(row); table.append(head); const body = node("tbody");
const predictions=api("/portfolio/predictions");for (const item of snapshot.items || []) { const tr = node("tr"); const name = node("td"); const instrument = {name:item.name, code:item.code, market:"KRX", asset_type:"stock"}; const nameButton = node("button", `${item.name} · ${item.code}`, "instrument-name"); nameButton.addEventListener("click", () => openInstrument(instrument)); name.append(nameButton); tr.append(name); for (const [value, unit, moving] of [[item.quantity, "주", false], [item.price, "원", false], [item.valuation, "원", false], [item.profit, "원", true]]) tr.append(node("td", moving ? movementText(value, unit) : priceText(value, unit), moving ? movementClass(value) : "")); if(item.unsettled_quantity!=null){tr.children[1].append(node("small",`잔고 ${item.balance_quantity??"—"} · 미결제 ${item.unsettled_quantity}`));}const prediction=node("td","저장 결과 확인 중…"),news=node("td","확인 중…");tr.append(prediction,news);predictions.then(data=>{if(!prediction.isConnected)return;const saved=data.items[item.code],forecast=saved?.forecasts?.[0];prediction.textContent=forecast?.predicted_close!=null?`${saved.target} · ${priceText(forecast.predicted_close)} / ${((forecast.predicted_return||0)*100).toFixed(3)}%`:"저장 예측 없음";prediction.className=movementClass(forecast?.predicted_return);}).catch(()=>prediction.textContent="조회 실패");api(`/news/today?code=${item.code}&levels=5`).then(data=>{if(!news.isConnected)return;news.textContent=data.total_sum==null?"미평가":`${data.total_sum>0?"+":""}${data.total_sum}점`;news.className=movementClass(data.total_sum);}).catch(()=>news.textContent="조회 실패");const cell = node("td"); const button = node("button", "종목 보기", "secondary"); button.addEventListener("click", () => openInstrument(instrument)); cell.append(button); tr.append(cell); body.append(tr); } table.append(body); const wrap = node("div", undefined, "table-wrap"); wrap.append(table); output.append(wrap); for(const [i,tr] of [...body.children].entries()){const code=snapshot.items[i]?.code;const item=snapshot.items[i],returns=node("td");tr.insertBefore(returns,tr.children[3]);paintHoldingPurchaseReturn(returns,item,item.price);if(code&&!isKiwoom)bindLivePrice(tr.children[2],code,(quote,open)=>{tr.children[2].replaceChildren(node("strong",priceText(quote.price)),node("small",`${open?"장중 자동 갱신":"장외 저장 시세"} · ${formatTime(quote.collected_at)}`));paintHoldingPurchaseReturn(returns,item,quote.price);});} output.append(node("p","매입 대비 수익률은 표시 현재가 ÷ 증권사 평균매입가 − 1 기준입니다. 수수료·세금 제외. 잔고는 60초마다 자동 조회하며 평가금액·손익은 증권사 잔고 조회 시각 기준입니다.","helper"));
    if (!snapshot.items?.length) output.append(node("p", "이 계좌에 표시할 국내 보유종목이 없습니다.", "helper"));
  }
  async function accounts(){
    const data=await api("/portfolio/accounts");if(!valid())return;select.replaceChildren();
    const empty=node("option","조회 계좌를 선택하세요");empty.value="";select.append(empty);
    for(const account of data.accounts){const option=node("option",`계좌 · 유형 ${account.account_type||"미확인"} · ${account.account_no}`);option.value=account.account_no;select.append(option);}
    const matching=data.accounts.filter(a=>a.account_no.endsWith(lastSnapshot?.account_last4||"not-selected"));
    if(matching.length===1)select.value=matching[0].account_no;else if(data.accounts.length===1)select.value=data.accounts[0].account_no;
    select.hidden=!data.accounts.length;
  }
  async function autoRefresh(){
    if(!valid()||document.hidden||refreshing)return;refreshing=true;
    try{
      if(state.user.role==="viewer"){display((await api(isKiwoom?"/portfolio/kiwoom/holdings":"/portfolio/holdings")).snapshot);return;}
      if(!isKiwoom&&!select.value)await accounts();
      if(!valid())return;
      if(!isKiwoom&&!select.value){status.textContent="자동 조회할 계좌를 선택하세요.";return;}
      const account=select.value;
      const data=await api(isKiwoom?"/portfolio/kiwoom/holdings/refresh":"/portfolio/holdings/refresh",{method:"POST",body:isKiwoom?undefined:JSON.stringify({account_no:account})});
      if(valid()&&(isKiwoom||select.value===account))display(data.snapshot);
    }catch(error){if(valid())status.textContent=`잔고 자동 조회 지연 · 이전 값 유지 · ${error.message}`;}
    finally{refreshing=false;}
  }
  select.addEventListener("change",()=>autoRefresh());
  loadAccounts.addEventListener("click",async()=>{loadAccounts.disabled=true;try{await accounts();await autoRefresh();}catch(error){if(valid())status.textContent=error.message;}finally{loadAccounts.disabled=state.user?.role==="viewer";}});
  try{display((await api(isKiwoom?"/portfolio/kiwoom/holdings":"/portfolio/holdings")).snapshot);}catch(error){if(valid())status.textContent=error.message;}
  await autoRefresh();
  const timer=setInterval(()=>{if(!valid()){clearInterval(timer);return;}autoRefresh();},60000);
}

async function renderPeriodReturns(area) {
  const brokerPanel=panel("증권사별 기간 수익 조회");area.append(brokerPanel);
  const controls=node("div",undefined,"source-row"),start=node("input"),end=node("input"),refresh=node("button","영웅문 기간 수익 조회","primary");start.type=end.type="date";start.value=`${new Date().getFullYear()}-01-01`;end.value=new Intl.DateTimeFormat("en-CA",{timeZone:"Asia/Seoul",year:"numeric",month:"2-digit",day:"2-digit"}).format(new Date());start.setAttribute("aria-label","수익 조회 시작일");end.setAttribute("aria-label","수익 조회 종료일");refresh.disabled=state.user.role==="viewer";controls.append(start,end,refresh);brokerPanel.append(controls);
  const brokerOutput=node("div");brokerOutput.setAttribute("role","status");brokerPanel.append(brokerOutput);
  const quick=node("div",undefined,"source-row");for(const [key,label] of [["year","연간"],["quarter","분기"],["month","월간"],["week","주간"]]){const button=node("button",label,"secondary");button.addEventListener("click",()=>{const date=new Date(`${end.value}T12:00:00`);if(key==="year")date.setMonth(0,1);else if(key==="quarter")date.setMonth(Math.floor(date.getMonth()/3)*3,1);else if(key==="month")date.setDate(1);else date.setDate(date.getDate()-((date.getDay()+6)%7));start.value=`${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,"0")}-${String(date.getDate()).padStart(2,"0")}`;});quick.append(button);}brokerPanel.insertBefore(quick,controls);
  function showBrokerReturn(snapshot){brokerOutput.replaceChildren(node("h3","영웅문 · 키움증권"));if(!snapshot){brokerOutput.append(node("p","저장된 기간 수익이 없습니다. 기간을 선택해 조회하세요.","helper"));return;}brokerOutput.append(node("p",`${snapshot.start} ~ ${snapshot.end} · ${snapshot.source} · ${formatTime(snapshot.collected_at)}`,"helper"));const table=node("table"),head=node("tr"),row=node("tr");for(const label of ["기초 순자산","기말 순자산","평가손익","기간 수익률","기간 입금","기간 출금"])head.append(node("th",label));for(const [key,unit] of [["tot_amt_fr","원"],["tot_amt_to","원"],["evltv_prft","원"],["prft_rt","%"],["termin_tot_trns","원"],["termin_tot_pymn","원"]])row.append(node("td",priceText(snapshot[key],unit),["evltv_prft","prft_rt"].includes(key)?movementClass(snapshot[key]):""));table.append(head,row);brokerOutput.append(table,node("p","증권사 제공 기간 수익률입니다. 현재 보유종목 손익률의 합계가 아니며 임의로 일별 그래프를 생성하지 않습니다.","helper"));}
  try{showBrokerReturn((await api("/portfolio/kiwoom/returns")).snapshot);}catch(error){brokerOutput.textContent=error.message;}
  refresh.addEventListener("click",async()=>{refresh.disabled=true;brokerOutput.textContent="영웅문 기간 수익 조회 중…";try{showBrokerReturn((await api("/portfolio/kiwoom/returns/refresh",{method:"POST",body:JSON.stringify({start:start.value,end:end.value})})).snapshot);}catch(error){brokerOutput.textContent=error.message;}finally{refresh.disabled=state.user.role==="viewer";}});
    const section = panel("나무증권 · 기간별 수익률 자료 상태"); area.append(section);
  section.append(node("p", "현재 보유분과 과거 매도분을 포함하는 계좌 전체 운용 수익률입니다. 입출금에 따른 원금 변화를 분리한 후 계산해야 합니다.", "helper"));
  const periods = node("div", undefined, "admin-tabs"); periods.setAttribute("role", "tablist"); periods.setAttribute("aria-label", "수익률 기간"); section.append(periods);
  const output = node("div", undefined, "return-chart-empty"); output.setAttribute("role", "status"); section.append(output);
  const names = {year: "연간", quarter: "분기", month: "월간", week: "주간"};
  function showPeriod() {
    for (const button of periods.children) button.setAttribute("aria-selected", String(button.dataset.period === state.returnPeriod));
    output.replaceChildren(node("p", `${names[state.returnPeriod]} 수익률 그래프 · 계산 자료 수집 전`, "return-chart-title"),
      node("p", "현재 나무 API에서 조회한 잔고만으로는 이 기간의 전체 수익률을 계산할 수 없습니다. 과거 매도분, 기간별 계좌 평가액, 입출금 내역을 대사한 뒤 그래프를 표시합니다.", "helper"));
  }
  for (const [key, label] of Object.entries(names)) {
    const button = node("button", label, "admin-tab"); button.type = "button"; button.dataset.period = key; button.setAttribute("role", "tab");
    button.addEventListener("click", () => { state.returnPeriod = key; showPeriod(); }); periods.append(button);
  }
  showPeriod();
  const source = node("p", "NH PLUG의 기간별 실현손익 API는 매도분을 확인할 수 있지만, 미실현 손익과 입출금 조정까지 포함한 전체 운용 수익률을 직접 제공하지 않습니다.", "helper"); section.append(source);
  const rail = $("#liveInspector"); rail.replaceChildren(node("p", "DETAILS", "eyebrow"), node("h2", "기간별 수익률"),
    node("p", "연간·분기·월간·주간 탭은 계좌 전체 기준입니다. 종목별 현재 잔고 수익률을 합산하지 않습니다.", "helper"),
    node("p", "과거 평가액과 입출금 자료가 검증되기 전에는 0%나 추정 그래프를 표시하지 않습니다.", "warning"));
}

async function renderSessionHistory(section, refreshUsers) {
  const area = node("details"); area.id="sessionHistory"; area.open=true; area.append(node("summary","접속 세션 이력"),node("p","활성은 만료되지 않은 로그인입니다. 기기·브라우저는 로그인 시 기록한 정보를 바탕으로 표시하며, 현재 온라인 여부를 뜻하지 않습니다. 기존 세션은 기기/시작 시각 정보가 없을 수 있습니다.","helper")); section.append(area);
  const filters = node("div",undefined,"user-form");
  function selectField(label, options) {const field=node("label",label,"inline-field");const select=node("select");select.setAttribute("aria-label",label);for(const [key,text] of options){const option=node("option",text);option.value=key;select.append(option);}field.append(select);filters.append(field);return select;}
  const users = await api("/users");
  const owner=selectField("세션 사용자",[["","전체 사용자"],...users.map(user=>[user.id,user.email])]);
  const status=selectField("세션 상태",[["all","전체"],["active","활성"],["expired","만료"],["revoked","로그아웃·해지"]]);
  const size=selectField("세션 목록 수",[["5","5개"],["10","10개"]]);
  const reload=node("button","세션 새로고침","secondary");reload.type="button";filters.append(reload);
  const message=node("p",undefined,"helper");message.setAttribute("role","status");const list=node("div");area.append(filters,message,list);let page=1;
  const reasons={logout:"로그아웃",replaced:"재로그인",policy_changed:"세션 정책 변경",user_restricted:"사용자 제한",code_reissued:"코드 재발급"};
  function date(value){return value==="unlimited"?"무제한":value?new Date(value).toLocaleString("ko-KR",{timeZone:state.user.timezone}):"기록 없음";}
  async function load(){for(const control of [owner,status,size,reload])control.disabled=true;message.textContent="세션 불러오는 중…";area.setAttribute("aria-busy","true");
    try{const data=await api(`/admin/sessions?status=${status.value}&user_id=${encodeURIComponent(owner.value)}&page=${page}&page_size=${size.value}`);for(const item of data.items)if(item.unlimited)item.expires_at="unlimited";list.replaceChildren();const table=node("table");const head=node("thead");const header=node("tr");for(const title of ["사용자·기기","상태","로그인 시각","마지막 요청","만료 시각","종료 시각"])header.append(node("th",title));head.append(header);table.append(head);const body=node("tbody");
      for(const session of data.items){const row=node("tr");const identity=node("td",session.email);identity.append(node("small",`${session.device} · ${session.browser}${session.is_current?" · 이 브라우저":""}`),node("small",session.client_ip||"주소 기록 없음"));row.append(identity,node("td",session.status==="active"?"활성":session.status==="expired"?"만료":reasons[session.end_reason]||"해지",session.status==="active"?"positive":"helper"));for(const value of [session.created_at,session.last_seen_at,session.expires_at,session.revoked_at])row.append(node("td",date(value)));body.append(row);}table.append(body);const wrap=node("div",undefined,"table-wrap session-table");wrap.append(table);list.append(wrap);if(!data.items.length)list.append(node("p","표시할 세션이 없습니다.","helper"));const paging=node("div",undefined,"pagination");for(const [label,delta] of [["‹ 이전",-1],["다음 ›",1]]){const button=node("button",label);button.disabled=delta<0?page<=1:page*data.page_size>=data.total;button.addEventListener("click",()=>{page+=delta;load();});paging.append(button);if(delta<0)paging.append(node("span",`${page} / ${Math.max(1,Math.ceil(data.total/data.page_size))} · ${data.total}건`,"helper"));}list.append(paging);message.textContent="";
    }catch(error){message.textContent=error.message;}finally{for(const control of [owner,status,size,reload])control.disabled=false;area.setAttribute("aria-busy","false");}}
  for(const control of [owner,status,size])control.addEventListener("change",()=>{page=1;load();});reload.addEventListener("click",async()=>{page=1;await load();try{await refreshUsers();}catch(error){reportError(error);}});await load();
}

async function renderKiwoomAdmin(section) {
  let policy = await api("/user/brokers/kiwoom");
  const area = panel("키움 REST API · 영웅문 연결"); area.id = "kiwoomConnection"; section.append(area);
  area.append(node("p", "개인 등록 키를 우선 사용하며 키 원문은 반환하지 않습니다. 기존 서버 키는 지정 소유자만 사용할 수 있습니다. 연결 허용 후 인증 확인을 실행하세요. 실전 주문 안전잠금은 별도입니다.", "helper"));
  const form = node("form", undefined, "settings-form");
  const environmentLabel = node("label", "키움 조회 환경"), environment = node("select"); environment.setAttribute("aria-label", "키움 조회 환경");
  for(const [value,label] of [["mock","모의투자"],["live","운영 · 읽기 전용"]]) { const option=node("option",label);option.value=value;environment.append(option); } environmentLabel.append(environment);
  const enabledLabel=node("label",undefined,"vector-check"),enabled=node("input"); enabled.type="checkbox";enabled.setAttribute("aria-label","키움 조회 연결 허용");enabledLabel.append(enabled,node("span","키움 조회 연결 허용"));
  const save=node("button","키움 설정 저장","primary");save.type="submit";
  const verify=node("button","키움 인증 확인","secondary");verify.type="button";
  const message=node("p");message.id="kiwoomMessage";message.setAttribute("role","status");
  const info=node("div");info.id="kiwoomStatus";form.append(environmentLabel,enabledLabel,save,verify,message);area.append(form,info);
  let busy=false;
  function show(){environment.value=policy.environment;enabled.checked=policy.enabled;info.replaceChildren(node("p",`키 소유자: ${policy.key_owner_matches?"일치":"불일치"} · 연결 ${policy.enabled?"허용":"중지"} · ${policy.token_valid?"인증 유효":"미인증/만료"}`),node("p",`누락 설정: ${policy.missing.length?policy.missing.join(", "):"없음"}`,"helper"),node("p",`최근 인증 ${formatTime(policy.last_verified_at)} · 만료 ${formatTime(policy.token_expires_at)}`,"helper"));if(policy.last_error)info.append(node("p",`최근 오류: ${policy.last_error}`,"warning"));buttons();}
  function buttons(){save.disabled=busy||!policy.key_owner_matches;verify.disabled=busy||!policy.key_owner_matches||!policy.enabled||policy.missing.length>0||environment.value!==policy.environment||enabled.checked!==policy.enabled;}
  form.addEventListener("submit",async event=>{event.preventDefault();busy=true;buttons();message.textContent="저장 중…";try{policy=await api("/user/brokers/kiwoom",{method:"PUT",body:JSON.stringify({version:policy.version,enabled:enabled.checked,environment:environment.value})});show();message.textContent="저장 완료 · 토큰 캐시가 초기화되었습니다. 인증 확인을 실행하세요.";}catch(error){message.textContent=error.message;}finally{busy=false;buttons();}});
  verify.addEventListener("click",async()=>{busy=true;buttons();message.textContent="키움 인증 확인 중…";try{policy=await api("/user/brokers/kiwoom/verify",{method:"POST",body:JSON.stringify({version:policy.version})});show();message.textContent=policy.token_valid?"키움 인증 성공 · 다차원 데이터 탭에서 수집할 수 있습니다.":"키움 인증 상태를 확인하세요.";}catch(error){message.textContent=error.message;}finally{busy=false;buttons();}});
  environment.addEventListener("change",buttons);enabled.addEventListener("change",buttons);show();
}

async function renderBrokerAdmin(section) {
  let policy = await api("/user/brokers/namuh");
  const area = node("details"); area.open = true; area.id = "namuhConnection"; area.append(node("summary", "나무증권 · NH PLUG 연결")); section.append(area);
  area.append(node("p", "나무 API 인증 후 시세·계좌를 읽기 전용으로 조회합니다. 주문은 연결하지 않습니다. 모의 환경에서도 인증은 운영 인증 서버를 사용하며, 현재가 API는 모의투자에서 제공되지 않을 수 있습니다.", "helper"));
  const form = node("form", undefined, "settings-form");
  const environmentLabel = node("label", "조회 환경"); const environment = node("select"); environment.name = "environment"; environment.setAttribute("aria-label", "나무 조회 환경");
  for (const [value, text] of [["mock", "모의투자 · 일부 조회 제한"], ["live", "실전투자 · 읽기 전용"]]) { const option = node("option", text); option.value = value; environment.append(option); } environmentLabel.append(environment);
  const enabledLabel = node("label", "나무 조회 허용"); const enabled = node("select"); enabled.name = "enabled"; enabled.setAttribute("aria-label", "나무 조회 허용");
  for (const [value, text] of [["false", "사용 안 함"], ["true", "사용"]]) { const option = node("option", text); option.value = value; enabled.append(option); } enabledLabel.append(enabled);
  const save = node("button", "나무 설정 저장", "primary"); save.type = "submit";
  const verify = node("button", "인증 확인", "secondary"); verify.type = "button";
  const message = node("p"); message.id = "namuhMessage"; message.setAttribute("role", "status");
  const info = node("div"); info.id = "namuhStatus"; form.append(environmentLabel, enabledLabel, save, verify, message); area.append(form, info);
  function display() {
    environment.value = policy.environment; enabled.value = String(policy.enabled);
    verify.disabled = !policy.enabled || policy.missing_settings.length > 0 || !policy.key_owner_matches;
    info.replaceChildren(node("p", `설정 버전 ${policy.version} · 실주문 미지원`, "helper"));
    if (policy.missing_settings.length) info.append(node("p", `서버 설정 필요: ${policy.missing_settings.join(", ")}`, "warning"));
    if (!policy.key_owner_matches) info.append(node("p", "위에서 본인의 앱키·시크릿키를 등록한 뒤 연결을 허용하고 인증을 확인하세요.", "helper"));
    if (policy.token_expires_at) info.append(node("p", `인증 토큰 만료: ${new Date(policy.token_expires_at).toLocaleString("ko-KR", {timeZone: state.user.timezone})}`, "helper"));
    if (policy.last_probe) info.append(node("p", `${policy.last_probe.created_at} · ${policy.last_probe.message}${policy.last_probe.reused_token ? " · 기존 토큰 재사용" : ""}`, policy.last_probe.status === "failed" ? "warning" : "helper"));
  }
  function busy(value, button, text) { for (const control of form.elements) control.disabled = value; area.setAttribute("aria-busy", String(value)); button.textContent = text; if (value) button.prepend(node("span", undefined, "spinner inline-spinner")); }
  form.addEventListener("submit", async (event) => {
    event.preventDefault(); const payload = {version: policy.version, enabled: enabled.value === "true", environment: environment.value}; busy(true, save, "저장 중…"); message.textContent = "";
    try { policy = await api("/user/brokers/namuh", {method:"PUT", body:JSON.stringify(payload)}); message.textContent = "저장 완료 · 설정 변경 시 기존 토큰 캐시를 폐기합니다."; }
    catch (error) { message.textContent = error.message; }
    finally { busy(false, save, "나무 설정 저장"); display(); }
  });
  verify.addEventListener("click", async () => {
    busy(true, verify, "인증 확인 중…"); message.textContent = "서버에서 인증을 확인하고 있습니다.";
    try { policy = await api("/user/brokers/namuh/verify", {method:"POST", body:JSON.stringify({version:policy.version})}); message.textContent = policy.last_probe.message; }
    catch (error) { message.textContent = error.message; }
    finally { busy(false, verify, "인증 확인"); display(); }
  });
  display();
}

async function renderCatalog(section) {
  const area = node("details"); area.open = true; area.id = "sourceCatalog";
  area.append(node("summary", "뉴스·통계 소스 목록"), node("p", "실제 뉴스·통계 데이터 수집에 성공한 소스만 표시합니다. API 키·계약 필요 또는 미수집 소스는 숨기며, 설정과 기존 기록은 DB에 보존합니다.", "helper")); section.append(area);
  const form = node("form", undefined, "settings-form");
  form.innerHTML = `<label>이름<input name="name" required maxlength="120" /></label>
    <label>도메인<input name="domain" required maxlength="253" placeholder="example.com" /></label>
    <label>주소<input name="url" type="url" required maxlength="500" placeholder="https://example.com/" /></label>
    <label>지역<select name="region"><option value="KR">국내</option><option value="US">미국</option><option value="GLOBAL">해외·글로벌</option></select></label>
    <label>자료 종류<input name="category" required maxlength="120" /></label>
    <label>수집 방식<select name="method"><option value="api">API</option><option value="file">파일</option><option value="rss">RSS</option><option value="web">웹</option></select></label>
    <label>이용 조건<textarea name="access_notes" maxlength="1000" placeholder="가입·유료·라이선스 조건만 입력하세요. API 키/비밀번호는 입력하지 마세요." ></textarea></label>
    <label>상태<select name="status"><option value="candidate">후보</option><option value="active">등록 활성</option><option value="retired">사용 중지</option></select></label>
    <label>분석 정책<select name="disposition"><option value="reference">참고만 · 점수 평가 제외</option><option value="use">분석 사용</option><option value="exclude">수집·분석 제외</option></select></label>
    <label>변경 사유<input name="reason" maxlength="200" placeholder="수정 시 필수" /></label>
    <button class="primary" type="submit">소스 추가</button><button class="secondary" type="button" id="cancelSourceEdit">새 항목</button>`;
  const editor = node("details"); editor.id = "sourceEditor"; editor.append(node("summary", "소스 추가·상세 수정"), form);
  const message = node("p", "", "helper"); message.setAttribute("role", "status");
  const controls = node("label", "목록 수 ", "inline-field"); const size = node("select"); size.setAttribute("aria-label", "소스 목록 수");
  for (const number of [5, 10]) { const option = node("option", `${number}개`); option.value = number; size.append(option); } controls.append(size); area.append(controls);
  const list = node("div"); list.id = "sourceCatalogList"; area.append(list, message, editor); let page = 1; let editing = null;
  function reset() { editing = null; form.reset(); form.elements.reason.required = false; form.querySelector('[type="submit"]').textContent = "소스 추가"; }
  form.querySelector("#cancelSourceEdit").addEventListener("click", reset);
  async function load() {
    list.textContent = "목록 불러오는 중…";
    const data = await api(`/sources?page=${page}&page_size=${size.value}&successful_only=true`);
    list.replaceChildren(); const wrap = node("div", undefined, "table-wrap"); const table = node("table");
    const header = node("thead"); const tr = node("tr"); for (const text of ["소스·도메인", "종류·이용 조건", "상태", "버전", "최근 2~3주 자료 / 평가 합계", "관리"]) tr.append(node("th", text)); header.append(tr); table.append(header);
    const body = node("tbody");
    for (const item of data.items) {
      const row = node("tr"); const title = node("td"); const link = node("a", item.name); link.href = item.url; link.target = "_blank"; link.rel = "noopener noreferrer"; title.append(link, node("small", item.domain));
      const description = node("td", `${item.category} · ${item.method}`); description.append(node("small", item.access_notes));
      row.append(title, description, node("td", {candidate:"후보",active:"등록 활성",retired:"사용 중지"}[item.status]), node("td", String(item.version)));
      const news=state.dailyNews?.sources.find(source=>source.id===item.id),newsCell=node("td",news?newsSourceSummary(news):"미조회");newsCell.dataset.newsSourceId=item.id;row.append(newsCell);
      if(news)description.append(node("small",news.connection_status==="search_connected"?"공개 기사 검색 수집기 · Tavily 연결 사용":news.connected?"공식 RSS 수집기 연결됨":news.statistics_connected?"별도 통계 수집기 연결됨 · 뉴스 비대상":`수집기 미연결 · ${news.connection}`));
      if(news?.collection_succeeded)description.append(node("small",news.connection_status==="search_connected"?"현재 수집 범위: 공개 기사 제목·검색 발췌문 · 검색/LLM 비용은 별도":news.connected?"현재 수집 범위: 키 없는 공식 RSS 제목·발췌문 (위 전체 상품 이용 조건과 구분)":"현재 수집 범위: 키 없는 공개 CSV/JSON 통계 · 저장 자료 보존"));
      const actions = node("td"); const edit = node("button", "수정", "secondary"); edit.type = "button";
      edit.addEventListener("click", () => { editing = item; editor.open = true; for (const field of ["name","domain","url","region","category","method","access_notes","status","disposition"]) form.elements[field].value = item[field]; form.elements.reason.value = ""; form.elements.reason.required = true; form.querySelector('[type="submit"]').textContent = "소스 수정 저장"; form.elements.name.focus(); });
      const retire = node("button", "사용 중지", "secondary"); retire.disabled = item.status === "retired";
      retire.addEventListener("click", async () => { if (!confirm(`${item.name} 출처를 사용 중지할까요? 기존 분석 기록은 보존됩니다.`)) return; retire.disabled = true; retire.textContent = "저장 중…";
        const {id, version, updated_at, ...value} = item;
        try { await api(`/sources/${id}`, {method:"PUT",body:JSON.stringify({...value, version, status:"retired",reason:"사용자 사용 중지"})}); message.textContent = "사용 중지 완료"; await load(); } catch(error) {message.textContent = error.message; retire.disabled = false; retire.textContent = "사용 중지";} });
      actions.append(edit, retire); row.append(actions); body.append(row);
    }
    table.append(body); wrap.append(table); list.append(wrap); if (!data.total) list.append(node("p", "등록된 소스가 없습니다.", "helper"));
    if(data.hidden_count)list.append(node("p",`미수집·키/계약 필요 소스 ${data.hidden_count}개 숨김 · 삭제하지 않고 DB에 보존합니다. 최근 2~3주 건수가 0이어도 과거 실제 수집 성공 이력이 있으면 표시합니다.`,"helper"));
    const pages = node("div", undefined, "pagination"); const previous = node("button", "‹ 이전", "secondary"); const next = node("button", "다음 ›", "secondary"); previous.disabled = page <= 1; next.disabled = page * Number(size.value) >= data.total;
    previous.addEventListener("click", () => {page--; load().catch(reportError);}); next.addEventListener("click", () => {page++; load().catch(reportError);}); pages.append(previous,node("span",`${page} / ${Math.max(1, Math.ceil(data.total / Number(size.value)))} · ${data.total}건`,"helper"),next); list.append(pages);
  }
  size.addEventListener("change", () => {page = 1; load().catch(reportError);});
  form.addEventListener("submit", async (event) => {event.preventDefault(); const button = form.querySelector('[type="submit"]'); button.disabled = true; button.textContent = "저장 중…";
    const value = Object.fromEntries(new FormData(form)); const reason = value.reason; delete value.reason;
    try { await api(editing ? `/sources/${editing.id}` : "/sources", {method:editing ? "PUT" : "POST",body:JSON.stringify(editing ? {...value,version:editing.version,reason} : value)}); message.textContent = "저장 완료 · 내 목록의 변경 이력은 감사 로그에 기록됩니다."; reset(); await load(); }
    catch(error) {message.textContent = error.message;} finally {button.disabled = false; button.textContent = editing ? "소스 수정 저장" : "소스 추가";} });
  await load();
}

function instrumentTable(items, actions, onNameClick = null, showPrices = false) {
  const wrap = node("div", undefined, "table-wrap"); const table = node("table"); const head = node("thead"); const labels = node("tr");
  for (const label of ["종목", "시장·유형", "상태", "동작"]) labels.append(node("th", label)); head.append(labels); table.append(head);
  if(showPrices){labels.insertBefore(node("th","현재가 / 마지막 종가"),labels.children[2]);labels.insertBefore(node("th","최근 3개월 최고가 대비"),labels.children[3]);}
  const body = node("tbody");
  for (const item of items) { const row = node("tr"); const name = node("td"); if (onNameClick) { const link = node("button", item.name, "instrument-name"); link.addEventListener("click", () => onNameClick(item)); name.append(link); } else name.append(node("span", item.name)); name.append(node("small", item.code));
    const market = node("td", `${item.market} · ${item.asset_type === "etf" ? "ETF" : "주식"}`); market.append(node("small", item.registration === "nh_public_master" ? "NH PLUG 공식 목록" : "수동 등록 · 공식 목록 미검증"));
    const buttons = node("td"); actions(item, buttons); row.append(name,market,node("td",item.status === "active" ? "사용 가능" : "사용 중지"),buttons); body.append(row); }
  if(showPrices)for(let i=0;i<items.length;i++){const cell=node("td","가격 확인 중…");cell.dataset.watchPrice=items[i].code;body.children[i].insertBefore(cell,body.children[i].children[2]);const peak=node("td","확인 중…");peak.dataset.watchPeak=items[i].code;body.children[i].insertBefore(peak,body.children[i].children[3]);}
  table.append(body); wrap.append(table); return wrap;
}
async function updateWatchPrices(wrap){
  const owner=state.user?.id;
  if(wrap._quotesBusy||!wrap.isConnected||state.view!=="watchlist"||document.hidden||Date.now()<(wrap._quotesDue||0))return;
  wrap._quotesBusy=true;
  const cells=[...wrap.querySelectorAll("[data-watch-price]")],codes=cells.map(cell=>cell.dataset.watchPrice);
  let delay=30000;
  try{
    const data=await api("/watchlist/quotes",{method:"POST",body:JSON.stringify({codes,refresh:false})});
    if(owner!==state.user?.id||!wrap.isConnected||state.view!=="watchlist")return;
    delay=2000;
    for(const cell of cells){
      const item=data.items[cell.dataset.watchPrice],snapshot=item?.snapshot;
      if(!snapshot){cell.replaceChildren(node("small",item?.error||"나무증권 시세 수집 대기"));continue;}
      cell.replaceChildren(node("strong",priceText(snapshot.price)),node("small",`나무증권 · DB 저장 시세 · ${formatTime(snapshot.collected_at)}`));
      if(item.stale)cell.append(node("small",data.is_open?"갱신 지연 · 마지막 수집 시세":"장외 · 마지막 수집 시세"));
      if(Number.isFinite(snapshot.change_rate))cell.append(node("small",movementText(snapshot.change_rate,"%"),movementClass(snapshot.change_rate)));
      const peak=cell.parentElement.querySelector("[data-watch-peak]"),result=threeMonthPeak(item.chart||[],snapshot.price,seoulDate());
      if(peak)peak.replaceChildren(...(result?[node("strong",movementText(result.change,"%"),movementClass(result.change)),node("small",`최고가 ${priceText(result.high)} · ${result.date}`),node("small",item.history_source)]:[node("small","3개월 고가 저장자료 부족")]));
    }
  }catch(error){if(owner===state.user?.id&&wrap.isConnected)for(const cell of cells){cell.querySelector('[data-batch-error]')?.remove();const note=node("small",`저장 시세 조회 지연 · ${error.message}`);note.dataset.batchError="true";cell.append(note);}}
  finally{
    wrap._quotesBusy=false;wrap._quotesDue=Date.now()+delay;clearTimeout(wrap._quotesTimer);
    if(owner===state.user?.id&&wrap.isConnected&&state.view==='watchlist')wrap._quotesTimer=setTimeout(()=>updateWatchPrices(wrap),delay);
  }
}
function threeMonthPeak(chart,price,today){
  const date=new Date(`${today}T12:00:00Z`),day=date.getUTCDate();date.setUTCDate(1);date.setUTCMonth(date.getUTCMonth()-3);const limit=new Date(Date.UTC(date.getUTCFullYear(),date.getUTCMonth()+1,0)).getUTCDate();date.setUTCDate(Math.min(day,limit));const start=date.toISOString().slice(0,10);
  const all=chart.filter(p=>p.date<=today&&p.high>0).sort((a,b)=>a.date.localeCompare(b.date));if(!(price>0)||!all.length||all[0].date>start)return null;
  const points=all.filter(p=>p.date>=start);if(!points.length)return null;const peak=points.reduce((a,b)=>a.high>=b.high?a:b);return {high:peak.high,date:peak.date,ratio:price/peak.high*100,change:(price/peak.high-1)*100};
}
function listPager(area, data, load) {
  const pages = node("div", undefined, "pagination"); const previous = node("button", "‹ 이전", "secondary"); const next = node("button", "다음 ›", "secondary"); previous.type = next.type = "button"; previous.disabled = data.page <= 1; next.disabled = data.page * data.page_size >= data.total;
  previous.addEventListener("click", () => load(data.page-1).catch(reportError)); next.addEventListener("click", () => load(data.page+1).catch(reportError)); pages.append(previous,node("span",`${data.page} / ${Math.max(1,Math.ceil(data.total/data.page_size))} · ${data.total}건`,"helper"),next); area.append(pages);
}
async function renderOfficialInstrumentSearch(parent, onSelect, actionLabel) {
  const area = node("details"); area.open = true; area.id = "officialInstrumentSearch";
  area.append(node("summary", "공식 종목 검색·추가"), node("p", "나무증권 공개 종목 목록 · 국내 주식/ETF. 등록은 주문이 아니며, 거래 가능 여부는 별도 확인이 필요합니다.", "helper")); parent.append(area);
  const sourceLink = node("a", "출처: NH PLUG 공식 종목 목록", "helper"); sourceLink.href="https://www.nhplug.com/instruments/m_new_stock.h"; sourceLink.target="_blank"; sourceLink.rel="noopener noreferrer"; area.append(sourceLink);
  const form = node("form", undefined, "source-row"); const label = node("label", "검색어 ", "inline-field"); const query = node("input"); query.maxLength = 80; query.placeholder = "예: 이티닉스, 삼성전자, 005930"; query.setAttribute("aria-label", "공식 종목 이름 또는 코드"); label.append(query);
  const search = node("button", "공식 종목 검색", "primary"); search.type = "submit"; form.append(label, search); area.append(form);
  const message = node("p", "", "helper"); message.setAttribute("role", "status"); const results = node("div"); area.append(message, results); let term = "";
  async function load(page = 1) {
    results.textContent = "검색 중…";
    const data = await api(`/instrument-search?q=${encodeURIComponent(term)}&page=${page}&page_size=${state.settings.value.page_size}`); results.replaceChildren();
    message.textContent = data.updated_at ? `목록 확인: ${new Date(data.updated_at).toLocaleString("ko-KR")} · ${data.total}건` : "공식 목록이 없습니다. 공식 목록 갱신을 눌러 가져오세요.";
    results.append(instrumentTable(data.items, (item, buttons) => {
      const add = node("button", item.registered_id && actionLabel === "종목 등록" ? "등록됨" : actionLabel, "secondary");
      add.disabled = state.user.role === "viewer" || (item.registered_id && actionLabel === "종목 등록");
      add.addEventListener("click", async () => { add.disabled = true; add.textContent = "저장 중…";
        try {
          const registered = item.registered_id ? { id: item.registered_id, ...item } : await api(`/admin/instrument-search/${item.code}?snapshot=${encodeURIComponent(data.snapshot)}`, { method: "POST" });
          await onSelect(registered); await load(page); message.textContent = `${item.name} · ${item.code} · 저장 완료`;
        } catch (error) { message.textContent = error.message; add.disabled = false; add.textContent = actionLabel; }
      }); buttons.append(add); if(item.trading_halted)buttons.append(node("span", "거래정지 표시", "helper"));
    }));
    if (data.updated_at && !data.total) results.append(node("p", "검색 결과가 없습니다. 이름 일부나 종목 코드를 입력하세요.", "helper"));
    listPager(results, data, (page) => load(page).catch(error => { message.textContent = error.message; }));
  }
  form.addEventListener("submit", async event => { event.preventDefault(); term = query.value.trim(); search.disabled = true; search.textContent = "검색 중…"; try { await load(); } catch(error) { message.textContent = error.message; } finally { search.disabled = false; search.textContent = "공식 종목 검색"; } });
  if (state.user.role !== "viewer") {
    const refresh = node("button", "공식 목록 갱신", "secondary"); refresh.type = "button";
    refresh.addEventListener("click", async () => { refresh.disabled = true; refresh.textContent = "목록 가져오는 중…"; message.textContent = "공식 목록을 확인하고 DB에 저장하고 있습니다.";
      try { await api("/admin/instrument-master/refresh", { method: "POST" }); await load(); } catch(error) { message.textContent = error.message; } finally { refresh.disabled = false; refresh.textContent = "공식 목록 갱신"; }
    }); form.append(refresh);
  }
  await load();
}
async function renderWatchlist() {
  const section=panel("내 관심종목"); const search=panel("등록 종목 검색"); search.id="registeredInstrumentSearch"; const area=$("#liveContent"); area.replaceChildren(section,search);
  section.append(node("p","관심종목은 본인 계정에만 저장됩니다. 종목 보기는 종목분석의 상세 화면으로 이동하며 자동 분석·매매가 아닙니다.","helper"));
  const list=node("div"); section.append(list); const message=node("p","","helper"); message.setAttribute("role","status"); section.append(message);
  function prepare(item) { openInstrument(item); }
  async function loadWatch(page=1) {
    list.textContent="목록 불러오는 중…";const data=await api(`/watchlist?page=${page}&page_size=${state.settings.value.page_size}`);list.replaceChildren();
    const watchTable=instrumentTable(data.items.map((item)=>item.instrument),(item,buttons)=>{const select=node("button","종목 보기","secondary");select.addEventListener("click",()=>prepare(item));const remove=node("button","관심 해제","secondary");remove.disabled=state.user.role==="viewer";remove.addEventListener("click",async()=>{remove.disabled=true;remove.textContent="저장 중…";try{await api(`/watchlist/${item.id}`,{method:"DELETE"});message.textContent="관심종목에서 해제했습니다.";await loadWatch();}catch(error){message.textContent=error.message;remove.disabled=false;remove.textContent="관심 해제";}});buttons.append(select,remove);},prepare,true);list.append(watchTable);updateWatchPrices(watchTable).catch(error=>{if(watchTable.isConnected)message.textContent=error.message;});let checking=false;const priceTimer=setInterval(async()=>{if(!watchTable.isConnected||state.view!=="watchlist"){clearInterval(priceTimer);return;}if(checking||document.hidden)return;checking=true;try{await updateWatchPrices(watchTable);}catch(error){if(watchTable.isConnected)message.textContent=error.message;}finally{checking=false;}},30000);
    if(!data.total)list.append(node("p","관심종목이 없습니다. 아래 검색 결과에서 추가하세요.","helper"));listPager(list,data,loadWatch);
  }
  const form=node("form",undefined,"source-row");const label=node("label","검색어 ","inline-field");const query=node("input");query.maxLength=80;query.setAttribute("aria-label","종목 이름 또는 코드");query.placeholder="이름 또는 6자리 코드";label.append(query);const button=node("button","종목 검색","primary");button.type="submit";form.append(label,button);search.append(form);const results=node("div");search.append(results);let term="";
  async function loadSearch(page=1) {
    results.textContent="검색 중…";const data=await api(`/instruments?q=${encodeURIComponent(term)}&page=${page}&page_size=${state.settings.value.page_size}`);results.replaceChildren();
    results.append(instrumentTable(data.items,(item,buttons)=>{const add=node("button","관심 추가","secondary");add.disabled=item.status!=="active"||state.user.role==="viewer";add.addEventListener("click",async()=>{add.disabled=true;add.textContent="저장 중…";try{await api("/watchlist",{method:"POST",body:JSON.stringify({instrument_id:item.id})});message.textContent="관심종목을 저장했습니다.";add.textContent="추가됨";await loadWatch();}catch(error){message.textContent=error.message;add.disabled=false;add.textContent="관심 추가";}});buttons.append(add);}));
    if(!data.total)results.append(node("p","검색 결과가 없습니다. 관리자가 종목 코드·이름을 먼저 등록해야 합니다.","helper"));listPager(results,data,loadSearch);
  }
  form.addEventListener("submit",async(event)=>{event.preventDefault();term=query.value;button.disabled=true;button.textContent="검색 중…";try{await loadSearch();}catch(error){message.textContent=error.message;}finally{button.disabled=false;button.textContent="종목 검색";}});
  await loadWatch();await loadSearch();
  const lookup = node("div"); area.insertBefore(lookup, search);
  await renderOfficialInstrumentSearch(lookup, async item => { await api("/watchlist", {method:"POST",body:JSON.stringify({instrument_id:item.id})}); await loadWatch(); }, "관심 추가");
}

async function render() {
  if(state.user&&state.view==="users"&&state.user.role!=="admin")state.view="dashboard";
  if (state.view !== "analysis" || !["detail","vectors"].includes(state.analysisTab)) stopStockPolling();
  if (!state.user) return; $("#pageTitle").textContent = titles[state.view]; $("#liveInspector").replaceChildren(); updateScreenHelp();
  if (state.view === "recommendations") await renderRecommendations(); else if (state.view === "data") await renderDataCollection(); else if (state.view === "settings") await renderSettings(); else if (state.view === "logs") await renderLogs(); else if (state.view === "users") await renderUsers(); else if (state.view === "watchlist") await renderWatchlist(); else if (state.view === "holdings") await renderHoldings(); else if (state.view === "analysis") await renderAnalysis(); else if (state.view === "orders") await renderTrading(); else await renderRuns();
}

installScreenHelp();
function connectEvents() {
  events?.close(); events = new EventSource("/api/events");
  events.addEventListener("expired", loggedOut);
  events.onmessage = async (event) => {
    const data = JSON.parse(event.data); const active = data.runs.filter((run) => ["queued", "running"].includes(run.status)).length;
    for (const run of data.runs) { const prior = state.statuses.get(run.id); if (prior && prior !== run.status && ["completed", "failed"].includes(run.status)) showToast(`${run.symbol} · ${statusLabels[run.status]}`); state.statuses.set(run.id, run.status); if (run.id === state.selected) inspector(run); }
    if (state.view==="logs") try { await render(); } catch (error) { reportError(error); }
  };
}

async function enter() {
  state.sessionEpoch=(state.sessionEpoch||0)+1;state.user = await api("/me"); state.settings = await api("/settings"); state.integrations = await api("/integrations"); document.documentElement.dataset.theme = state.settings.value.theme;
  $("#userIdentity").textContent = `${({admin:"관리자",trader:"일반 사용자 · 분석·매매 가능",viewer:"일반 사용자 · 조회 전용"})[state.user.role]||state.user.role} · ${state.user.email}`; $("#usersNav").hidden = state.user.role !== "admin";
  $("#loginGate").hidden = true; $("#appShell").hidden = false;
  if (window.innerWidth <= 800) { $("#leftRail").classList.add("collapsed"); $("#rightRail").classList.add("collapsed"); }
  const restored=state.settings.value.screen_state;
  state.dataCollectionTab=restored?.data_tab||"status";
  state.recommendationsMarket=restored?.recommendations_market||"000";
  state.reportFilters=restored?.report||null;
  state.realizedCode=restored?.realized_code||"";
  state.tradeBroker=restored?.trade_broker||"namuh";state.tradeCode=restored?.trade_code||"";if(restored?.dimension)Object.assign(dimensionDraft,restored.dimension);
  state.view=restored?.view||"dashboard";if(state.view==="users"&&state.user.role!=="admin")state.view="dashboard";
  if(restored){state.analysisTab=restored.analysis_tab;state.holdingsTab=restored.holdings_tab;state.returnPeriod=restored.return_period;state.newsCode=restored.news_code;state.chartStartByCode=new Map(Object.entries(restored.chart_start||{}));state.chartEndByCode=new Map(Object.entries(restored.chart_end||{}));if(restored.code){const owned=await api("/vectors/instruments");state.selectedInstrument=owned.items.find(v=>v.code===restored.code)||null;}}
  if(state.view==="orders")$("#rightRail").classList.add("collapsed");
  document.querySelectorAll(".nav-item[data-view]").forEach(button=>button.classList.toggle("active",button.dataset.view===state.view));updateClock(); inspector(null); await render(); connectEvents();
}

$("#loginForm").addEventListener("submit", async (event) => {
  event.preventDefault(); if ($("#codeStep").hidden) { await requestEmailCode(); return; }
  const button = $("#loginSubmit"); button.disabled = true; button.textContent = "확인 중…";
  try { await api("/auth/verify", { method: "POST", body: JSON.stringify({ email: $("#loginEmail").value, access_code: $("#loginCode").value, remember_email: $("#rememberEmail").checked }) }); cacheLoginEmail(); $("#loginCode").value = ""; await enter(); }
  catch (error) { $("#loginMessage").textContent = error.message; } finally { button.disabled = false; button.textContent = "코드 확인·로그인"; }
});
let resendTimer;
function resetCodeStep() {
  clearInterval(resendTimer); $("#codeStep").hidden = true; $("#loginCode").disabled = true; $("#loginCode").value = ""; $("#loginEmail").readOnly = false;
  $("#requestLoginCode").disabled = false; $("#requestLoginCode").textContent = "인증 코드 보내기";
}
async function requestEmailCode() {
  if (!$("#loginEmail").reportValidity() || $("#requestLoginCode").disabled) return;
  const button = $("#requestLoginCode"); button.disabled = true; button.textContent = "메일 발송 중…";
  $("#loginMessage").textContent = "";
  try {
    const result = await api("/auth/request-code", { method: "POST", body: JSON.stringify({ email: $("#loginEmail").value }) });
    $("#codeStep").hidden = false; $("#loginCode").disabled = false; $("#loginCode").value = ""; $("#loginEmail").readOnly = true;
    $("#loginMessage").textContent = `${result.message} · ${result.code_ttl_minutes}분 이내 입력`; $("#loginCode").focus();
    const until = Date.now() + result.retry_after_seconds * 1000;
    clearInterval(resendTimer);
    const update = () => { const left = Math.ceil((until - Date.now()) / 1000); button.textContent = left > 0 ? `재요청 ${left}초 후` : "인증 코드 다시 보내기"; if (left <= 0) { button.disabled = false; clearInterval(resendTimer); } };
    update(); resendTimer = setInterval(update, 1000);
  } catch (error) { button.disabled = false; button.textContent = "인증 코드 보내기"; $("#loginMessage").textContent = error.message; }
}
$("#requestLoginCode").addEventListener("click", requestEmailCode);
$("#changeLoginEmail").addEventListener("click", () => { resetCodeStep(); $("#loginMessage").textContent = ""; $("#loginEmail").focus(); });
$("#logoutButton").addEventListener("click", async () => { try { await api("/auth/logout", { method: "POST" }); loggedOut(); } catch (error) { reportError(error); } });
document.querySelectorAll("[data-toggle]").forEach((button) => button.addEventListener("click", () => document.getElementById(button.dataset.toggle).classList.toggle("collapsed")));
document.querySelectorAll(".nav-item[data-view]").forEach((button) => {
  if (!titles[button.dataset.view]) { button.disabled = true; button.title = "다음 구현 단계에서 제공됩니다."; return; }
  button.addEventListener("click", async () => { stopStockPolling(); state.view = button.dataset.view; document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item === button)); try { await render(); } catch (error) { reportError(error); } if (window.innerWidth <= 800) $("#leftRail").classList.add("collapsed"); });
});
function updateClock() {
  $("#systemClock").textContent = new Date().toLocaleString("ko-KR", { timeZone: state.user?.timezone || "Asia/Seoul" }) + ` · ${state.user?.timezone || "Asia/Seoul"}`;
  if(state.user?.session_unlimited){$("#sessionStatus").textContent="관리자 세션 무제한 · 로그아웃·강제 해지 시 종료";return;}
  if (state.user?.session_expires_at) {
    const remaining = new Date(state.user.session_expires_at).getTime() - Date.now();
    if (remaining <= 0) { syncSessionLifetime(); return; }
    const minutes = Math.ceil(remaining / 60000);
    $("#sessionStatus").textContent = `부여된 시간 ${state.user.session_hours}시간 · 세션 남은 시간 ${Math.floor(minutes / 60)}시간 ${minutes % 60}분 · 만료 ${new Date(state.user.session_expires_at).toLocaleString("ko-KR", { timeZone: state.user.timezone })}`;
  }
}
let sessionSyncBusy=false;
async function syncSessionLifetime(){
  if(!state.user||sessionSyncBusy||document.hidden)return;const owner=state.user.id;sessionSyncBusy=true;
  try{const user=await api("/me");if(owner===state.user?.id)state.user={...state.user,...user};}
  catch(error){if(owner===state.user?.id)reportError(error);}finally{sessionSyncBusy=false;}
}
setInterval(syncSessionLifetime,30000);
updateClock(); setInterval(updateClock, 1000);
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/service-worker.js").catch(() => {});
enter().catch((error) => { loggedOut(); if (error.status !== 401) $("#loginMessage").textContent = error.status === 404 ? "현재 서버에 로그인 API가 없습니다. 정적 파일 서버를 종료하고 FastAPI 서버로 실행하세요." : "로그인 서버에 연결하지 못했습니다. 서버 실행과 DB migration을 확인하세요."; });
