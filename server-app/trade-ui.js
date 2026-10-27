/* Order UI: reads saved analysis; never silently starts an order or paid analysis. */
function orderRealizedText(order){return order.side==="sell"&&order.filled_quantity>0?` · 실현손익 ${order.realized_profit==null?"확인 필요":priceText(order.realized_profit)} (수수료·세금 제외)`:"";}
function tradeSettingBox(title,...children){
  const box=node("fieldset",undefined,"trade-setting-box");box.setAttribute("aria-label",title);box.append(node("legend",title));
  const summaries={"공통 수량·비중":"비우면 비중 적용 · 자동매도 전량","자동매수":"이상: 시장가(상한 없음) / 이하: 지정가","수동 추가매수":"지정가 이하 · 별도 승인","자동 익절":"조건 충족 시 전량 시장가","자동 손절":"조건 충족 시 전량 시장가","수동 일부매도":"지정가 이상 · 비우면 시장가"};
  const notes=children.filter(child=>child.tagName==="SMALL");
  box.append(...children.filter(child=>!summaries[title]||child.tagName!=="SMALL"));
  if(summaries[title]&&notes.length){const help=node("details",undefined,"trade-box-help");help.append(node("summary",summaries[title]),...notes);box.append(help);}
  return box;
}
function prepareMobileTradeCard(row){
  if(!prepareMobileTradeCard.media){const media=window.matchMedia("(max-width:800px)");prepareMobileTradeCard.media=media;media.addEventListener("change",()=>{for(const details of document.querySelectorAll(".mobile-trade-details"))details.open=!media.matches;});}
  for(const [index,label] of [[2,"공통 매매 수량"],[3,"자동매수 · 추가매수"],[4,"자동매도 · 일부매도"]]){const cell=row.children[index],details=node("details",undefined,"mobile-trade-details"),summary=node("summary",label);details.open=!prepareMobileTradeCard.media.matches;details.append(summary);while(cell.firstChild)details.append(cell.firstChild);cell.append(details);}
  row.children[5].dataset.mobileSection="매매 실행 · 수동 주문 / 자동매매";
}
async function renderPaperTrading(area) {
  const box=panel("Paper 자동매매 · 증권사 주문 전송 없음");box.classList.add("paper-panel");area.append(box);
  const status=node("p","상태 조회 중…","helper"),message=node("p","","helper"),journal=node("div");
  const start=node("button","Paper 시작 · 즉시 검사","primary"),pause=node("button","일시중지 · Paper 긴급 차단","secondary"),check=node("button","지금 조건 검사 · 주문 없음","secondary");
  const pilot=node("button","Paper 대상 LG에너지솔루션으로 제한","secondary");box.append(pilot);pilot.disabled=state.user.role==="viewer";
  box.append(status,node("p","시작 버튼을 누르면 일괄 가상 시나리오와 현재 조건을 즉시 검사합니다. 장외·휴장 시 시세 기반 매매는 건너뜁니다. 가상 현금 1,000만원 · 수수료 2bp/슬리피지 5bp(가정, 세금 미반영). 저장된 자동매매 종목을 ID 순서로 처리하며 남은 가상 현금을 공유합니다. 실제 잔고·증권사 주문·알림 전송과 무관합니다.","helper"),start,pause,check,message);
  const suite=node("section"),suiteButton=node("button","전체 시나리오 일괄 테스트 · 주문 없음","primary"),suiteResult=node("div");
  suite.append(node("h3","장 시간·실제 예측과 무관한 자동매매 로직 검사"),node("p","양수·음수·0%·예측 누락·휴장·중복 실행·목표수익 매도 등을 모두 실행합니다. 매수를 건너뛰는 것이 예상 결과이면 통과입니다. 실제 증권사 API·알림은 호출하지 않으며 운영 Paper 지갑과 실행 상태는 바꾸지 않습니다.","helper"),suiteButton,suiteResult);box.append(suite);
  function drawSuite(value){suiteResult.replaceChildren();if(!value)return;const r=value.suite;suiteResult.append(node("h3",`일괄 검사 완료 · ${r.passed}/${r.total} 통과 · 실패 ${r.failed} · ${r.duration_ms}ms`),node("p",`${formatTime(value.at)} · ${r.assumptions}`,"helper"));const table=node("table"),head=node("tr");for(const label of ["시나리오","결과","예상 → 실제"])head.append(node("th",label));table.append(head);for(const c of r.cases){const row=node("tr");row.append(node("td",c.title),node("td",c.passed?"통과":"실패"),node("td",c.steps.map(s=>`${s.clock||''} ${s.expected||''} → ${s.action||'오류'}${s.quantity?' '+s.quantity+'주':''} · ${s.reason}`).join(' / ')));table.append(row);}suiteResult.append(table);}
  suiteButton.disabled=state.user.role==="viewer";
  suiteButton.addEventListener("click",async()=>{suiteButton.disabled=true;suiteButton.textContent="일괄 시나리오 검사 중…";try{drawSuite(await api("/trading/paper/scenario-suite",{method:"POST"}));await refresh();}catch(e){suiteResult.replaceChildren(node("p",e.message,"warning"));}finally{suiteButton.disabled=state.user.role==="viewer";suiteButton.textContent="전체 시나리오 일괄 테스트 · 주문 없음";}});
  const details=node("details"),summary=node("summary","가상 시각 시나리오 테스트 · 운영 지갑 미변경");details.append(summary);box.append(details);
  const plan=node("select"),at=node("input"),price=node("input"),prediction=node("input"),test=node("button","단발 가상 매수 검사","secondary");
  plan.setAttribute("aria-label","시나리오 종목");at.type="datetime-local";at.value=seoulDate()+"T09:00";prediction.type="number";setupPriceInput(price);price.placeholder="가정 현재가 (원)";prediction.placeholder="가정 예상수익률 (%)";
  for(const [label,input] of [["종목",plan],["가상 한국 시각",at],["가정 가격",price],["매매시점 가정 예측 (%)",prediction]]){const field=node("label",label);field.append(input);details.append(field);}details.append(test);box.append(journal);
  const readonly=state.user.role==="viewer";for(const b of [start,pause,check,test])b.disabled=readonly;
  async function refresh(){
    if(!box.isConnected)return;
    try{
      const d=await api("/trading/paper");if(!box.isConnected)return;const w=d.wallet;
      drawSuite(d.latest_suite);
      status.textContent=`${w.enabled?'Paper 실행 요청됨':'중지'} · 대상 ${w.pilot?'LG에너지솔루션만 (일봉/10%/목표 3%)':'저장된 자동매매 종목'} · 시작 ${w.started_at?formatTime(w.started_at):"기존 실행"} · 버튼 클릭 즉시 검사 · worker ${d.worker_alive?'정상':'응답 없음/중지'} · 마지막 확인 ${w.last_tick?formatTime(w.last_tick):'없음'} · 다음 확인 ${w.enabled&&w.next_tick?formatTime(w.next_tick):'중지/미정'} · 가상 현금 ${priceText(Math.round(w.cash))}`;
      journal.replaceChildren(node("h3",`가상 평가액 ${priceText(Math.round(d.equity))} · 가상 손익 ${priceText(Math.round(d.pnl))}`));
      for(const [id,p] of Object.entries(w.positions)){if(!p.quantity)continue;const option=[...plan.options].find(o=>o.value===id);journal.append(node("p",`${option?.textContent||id} · 가상 ${p.quantity}주 · 비용 포함 단가 ${priceText(p.cost)}`,"helper"));}
      journal.append(node("h3","가상 매매·조건 판단 이력 (최근 50건)"));
      for(const e of d.events){journal.append(node("p",`${e.at?formatTime(e.at):''} · ${e.code||''} ${e.action} ${e.simulated_action||''} · ${e.quantity||0}주 · ${e.reason||''}${e.realized_pnl!=null?' · 가상 실현손익 '+priceText(e.realized_pnl):''}`,"helper"));for(const r of e.results||[])journal.append(node("small",`${r.code} · ${r.action} · ${r.reason}`));}
      if(!d.events.length)journal.append(node("p","가상 실행 이력이 없습니다.","helper"));
    }catch(e){status.textContent=e.message;}
  }
  async function run(button,path,body,method="POST"){button.disabled=true;try{const r=await api(path,{method,body:body?JSON.stringify(body):undefined});message.textContent=r.result?`단발 가상 결과: ${r.result.action} · ${r.result.quantity||0}주 · ${r.result.price?priceText(r.result.price):''} · ${r.result.reason} · 실제 주문 전송 없음 · 운영 지갑 미변경`:r.results? r.results.map(v=>`${v.code}: ${v.action} · ${v.reason}`).join(' / ')||'저장된 자동매매 종목 없음':r.note|| (r.trade_time?`매매시점 ${r.trade_time} 저장 완료`:r.enabled?'Paper 실행 요청 저장 · worker 응답을 확인하세요.':'Paper 중지 저장');await refresh();}catch(e){message.textContent=e.message;}finally{button.disabled=readonly;}}
  start.addEventListener("click",()=>run(start,"/trading/paper",{enabled:true},"PUT"));pause.addEventListener("click",()=>run(pause,"/trading/paper",{enabled:false},"PUT"));check.addEventListener("click",()=>run(check,"/trading/paper/check"));
  pilot.addEventListener("click",()=>run(pilot,"/trading/paper/pilot",{},"PUT"));
  test.addEventListener("click",()=>{if(!plan.value||!at.value||!price.value){message.textContent="종목·시각·가격을 입력하세요.";return;}run(test,"/trading/paper/scenario",{plan_id:plan.value,at:at.value+":00+09:00",price:manualLimitPrice(price.value),predicted_return:prediction.value?Number(prediction.value)/100:null});});
  const lg=node("option","LG에너지솔루션 · 373220 · Paper 전용");lg.value="pilot:373220";plan.append(lg);
  try{const p=await api("/trading/plans");for(const v of p.items){const o=node("option",`${v.name} · ${v.code}`);o.value=v.id;plan.append(o);}}catch(e){message.textContent=e.message;}
  await refresh();async function poll(){if(!box.isConnected)return;await refresh();if(box.isConnected)setTimeout(poll,5000);}setTimeout(poll,5000);
}
async function renderAutomaticRuntime(section){
  const owner=state.user.id,box=node("div",undefined,"trade-runtime-options"),status=node("p","조회 중…","helper"),enabled=node("input"),label=node("label","내 자동매매 실행");
  enabled.type="checkbox";enabled.setAttribute("aria-label","내 자동매매 실행");label.prepend(enabled);
  box.append(label,status,node("p","자동매매: 체크 = 실행 허용, 해제 = 전체 일시정지. 활성 종목의 기존 회차를 이어갑니다. 일시정지 중에는 매수·익절·손절 모두 대기하며, 기존 주문 체결 확인과 수동 주문은 계속 가능합니다.","helper"));section.append(box);
  let value;
  function paint(){status.textContent=value.paused?"내 자동매매 전체 일시정지 중":"내 자동매매 감시 허용 · 활성 종목만 동작";enabled.checked=!value.paused;enabled.disabled=state.user.role==="viewer";}
  enabled.disabled=true;
  try{value=await api("/trading/automatic-runtime");if(owner!==state.user?.id||!box.isConnected)return;paint();}catch(e){status.textContent=e.message;}
  enabled.addEventListener("change",async()=>{
    if(!value||owner!==state.user?.id||state.user.role==="viewer")return;
    const paused=!enabled.checked;
    if(!paused&&!window.confirm("기존 자동매매 회차를 재개할까요? 현재 조건을 충족한 자동 주문이 다시 전송될 수 있습니다.")){paint();return;}
    enabled.disabled=true;
    try{value=await api("/trading/automatic-runtime",{method:"PUT",body:JSON.stringify({version:value.version,paused})});if(owner!==state.user?.id||!box.isConnected)return;paint();}
    catch(e){if(owner!==state.user?.id||!box.isConnected)return;paint();status.textContent=e.message;try{value=await api("/trading/automatic-runtime");if(owner===state.user?.id&&box.isConnected){paint();status.textContent=e.message;}}catch{enabled.disabled=true;}}
  });
  return box;
}
async function renderTradeNotificationChannels(section) {
  const channelsBox=node("fieldset"),legend=node("legend","매매 알림 채널"),emailChannel=node("input"),telegramChannel=node("input"),channelSave=node("button","알림 채널 저장","secondary"),channelMessage=node("p","","helper");
  let channelPolicy=await api("/trading/notification-channels");
  channelsBox.append(legend);
  for(const [input,label,key] of [[emailChannel,"이메일","email"],[telegramChannel,"텔레그램","telegram"]]){input.type="checkbox";input.checked=channelPolicy[key];input.disabled=state.user.role==="viewer";const field=node("label",label);field.prepend(input);channelsBox.append(field);}
  channelSave.disabled=state.user.role==="viewer";channelsBox.append(channelSave,channelMessage);section.append(channelsBox);
  channelSave.addEventListener("click",async()=>{channelSave.disabled=true;try{channelPolicy=await api("/trading/notification-channels",{method:"PUT",body:JSON.stringify({version:channelPolicy.version,email:emailChannel.checked,telegram:telegramChannel.checked})});channelMessage.textContent="저장 완료 · 선택한 채널로만 매매 알림 발송";}catch(e){channelMessage.textContent=e.message;}finally{channelSave.disabled=state.user.role==="viewer";}});
}
function tradeDayChange(snapshot){const price=snapshot?.price,reference=snapshot?.reference_close;if(!Number.isFinite(price)||price<=0||!Number.isFinite(reference)||reference<=0)return null;const amount=price-reference;return {amount,rate:amount/reference*100,reference};}
function manualLimitPrice(value) { const text=String(value).trim();if(text==="")return null;if(!/^(?:\d+|\d{1,3}(?:,\d{3})+)$/.test(text))throw Error("가격은 1원 이상의 정수로 입력하세요.");const price=Number(text.replaceAll(",",""));if(!Number.isSafeInteger(price)||price<=0||price>1000000000)throw Error("가격은 1원 이상 1,000,000,000원 이하로 입력하세요.");return price; }
function formatPriceInput(value) { const text=String(value);return /^\d[\d,]*$/.test(text)?text.replaceAll(",","").replace(/\B(?=(\d{3})+(?!\d))/g,","):text; }
function attachTradingPerformance(cell,plan) {
  const box=node("div",undefined,"trade-key-metrics"),quantity=node("strong","보유 수량 확인 중…"),purchase=node("small","매입평균가 확인 중…"),valuation=node("p","평가손익 · 시세 확인 중…"),valuationNote=node("small"),profit=node("p","일일 실현손익 확인 중…"),note=node("small"),refresh=node("button","잔고·손익 갱신","secondary");
  quantity.classList.add("trade-key-quantity");purchase.classList.add("trade-key-purchase");valuation.dataset.keyMetric="valuation";profit.dataset.keyMetric="realized";
  box.append(quantity,purchase,valuation,valuationNote,profit,note,refresh);cell.append(box);refresh.disabled=state.user.role==="viewer";
  const owner=state.user.id;let busy=false,position=null,quote=null;
  function paintValuation(){
    const result=tradeMarkToMarket(position,quote);
    valuation.textContent=result?`평가손익\n${movementText(result.profit,"원")}\n수익률 ${result.rate==null?"—":movementText(result.rate,"%")}`:"평가손익\n확인 불가\n잔고·매입단가·시세 확인 필요";
    valuation.className=result?movementClass(result.profit):"helper";
    valuationNote.textContent=quote?`${quote.stale?"지연 시세":"현재가 기준"} · 수수료·세금 제외`:"시세 수신 대기";valuationNote.title=quote?`현재가 ${priceText(quote.price)} · ${formatTime(quote.collected_at)}`:"";
  }
  async function update(fresh=false){if(busy||!box.isConnected||owner!==state.user?.id||document.hidden)return;busy=true;refresh.disabled=true;try{
    const path=`/trading/plans/${plan.id}/performance${fresh&&state.user.role!=="viewer"?"/refresh":""}`;
    const data=await api(path,{method:path.endsWith("/refresh")?"POST":"GET"});if(!box.isConnected||owner!==state.user?.id)return;
    quantity.textContent=data.position?`보유 ${priceText(data.position.quantity,"주")}`:"보유 수량 미조회";
    purchase.textContent=tradePurchaseLabel(data.position).replace(" · 증권사 잔고 기준","");
    position=data.position;paintValuation();
    const d=data.daily;profit.textContent=`오늘 앱 실현손익\n${d.profit==null?"확인 불가":movementText(d.profit,"원")}\n${d.return_rate==null?"수익률 —":"수익률 "+movementText(d.return_rate,"%")}`;profit.className=movementClass(d.profit);
    const stamp=data.position?.collected_at;
    const shortTime=stamp?new Intl.DateTimeFormat("sv-SE",{timeZone:"Asia/Seoul",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hourCycle:"h23"}).format(new Date(stamp)):"미조회";
    note.textContent=`잔고 ${shortTime} · ${data.position?.stale?"지연":d.incomplete?"자료 부족":"앱 체결 기준"}`;
    note.title=`${stamp?formatTime(stamp)+" · ":""}${d.note}${d.incomplete?" · 매입원가/체결일 자료 부족":""}`;
  }catch(e){if(box.isConnected)note.textContent=`잔고·손익 갱신 지연 · 이전 값 유지 · ${e.message}`;}finally{busy=false;if(box.isConnected)refresh.disabled=state.user?.role==="viewer";}}
  refresh.addEventListener("click",()=>update(true));
  setTimeout(async()=>{await update();await update(true);},0);
  const timer=setInterval(()=>{if(!box.isConnected||owner!==state.user?.id){clearInterval(timer);return;}update(true);},30000);
  return {setQuote(snapshot){if(box.isConnected&&owner===state.user?.id){quote=snapshot;paintValuation();}}};
}
function tradeMarkToMarket(position,quote) {
  const quantity=position?.quantity,price=quote?.price,basis=position?.purchase_price;
  if(!Number.isFinite(quantity)||quantity<0||!Number.isFinite(price)||price<=0)return null;
  if(quantity===0)return {profit:0,rate:null};
  if(!Number.isFinite(basis)||basis<=0)return null;
  return {profit:(price-basis)*quantity,rate:(price/basis-1)*100};
}
function tradePurchaseLabel(position) {
  if(!position)return "매입평균가 미조회";
  if(position.quantity===0)return "매입평균가 — · 보유 없음";
  const price=position.purchase_price;
  return typeof price==="number"&&Number.isFinite(price)&&price>0?`매입평균가 ${priceText(price)} · 증권사 잔고 기준`:"매입평균가 확인 불가";
}

function stableTradePlans(items) {
  return [...items].sort((a,b)=>a.code.localeCompare(b.code)||a.broker.localeCompare(b.broker)||a.id.localeCompare(b.id));
}

function captureTradeViewport(section,planId) {
  const rows=[...section.querySelectorAll("[data-trade-row]")];
  const row=rows.find(r=>r.dataset.tradeRow===planId)||rows.find(r=>r.getBoundingClientRect().bottom>100);
  return row?{id:row.dataset.tradeRow,top:row.getBoundingClientRect().top,alignTop:!!planId,left:section.querySelector(".table-wrap")?.scrollLeft||0}:null;
}

function restoreTradeViewport(section,anchor) {
  if(!anchor||!section.isConnected)return;
  const row=[...section.querySelectorAll("[data-trade-row]")].find(r=>r.dataset.tradeRow===anchor.id);
  if(!row)return;
  for(let parent=row.parentElement;parent;parent=parent.parentElement){
    if(parent.scrollHeight>parent.clientHeight&&/auto|scroll/.test(getComputedStyle(parent).overflowY)){
      const top=anchor.alignTop?Math.max(0,parent.getBoundingClientRect().top)+8:anchor.top;
      parent.scrollTop+=row.getBoundingClientRect().top-top;break;
    }
  }
  const wrap=section.querySelector(".table-wrap");if(wrap)wrap.scrollLeft=anchor.left;
  row.classList.add("trade-row-selected");
}
function setupPriceInput(input) {
  input.type="text";input.inputMode="numeric";input.value=formatPriceInput(input.value);
  input.addEventListener("input",()=>{
    const before=input.value,at=input.selectionStart??before.length,digits=before.slice(0,at).replace(/,/g,"").length;
    input.value=formatPriceInput(before);
    let cursor=0,count=0;while(cursor<input.value.length&&count<digits){if(input.value[cursor]!==",")count++;cursor++;}
    input.setSelectionRange(cursor,cursor);
    try{manualLimitPrice(input.value);input.setCustomValidity("");}catch(error){input.setCustomValidity(error.message);}
  });
}
function tradeSnapTarget(rows,top,tolerance=36) {
  return rows.map(row=>({row,delta:row.getBoundingClientRect().top-top})).filter(v=>Math.abs(v.delta)<=tolerance).sort((a,b)=>Math.abs(a.delta)-Math.abs(b.delta))[0]||null;
}
function installTradeScrollSnap(section) {
  const area=section.closest(".content"),abort=new AbortController();if(!area)return;
  let timer,last=null,intentUntil=0,manualUntil=0;
  const options={passive:true,signal:abort.signal};
  function intent(event){intentUntil=Date.now()+1200;if(event.altKey)manualUntil=Date.now()+1500;}
  area.addEventListener("wheel",intent,options);area.addEventListener("touchmove",intent,options);
  area.addEventListener("pointerdown",intent,options);area.addEventListener("pointermove",event=>{if(event.buttons)intent(event);},options);
  area.addEventListener("keydown",event=>{if(["ArrowDown","ArrowUp","PageDown","PageUp","Home","End"," "].includes(event.key))intent(event);},options);
  area.addEventListener("scroll",()=>{
    clearTimeout(timer);if(Date.now()>intentUntil||Date.now()<manualUntil)return;
    timer=setTimeout(()=>{
      if(!section.isConnected||Date.now()<manualUntil||section.querySelector("input:focus,select:focus,textarea:focus"))return;
      const target=tradeSnapTarget([...section.querySelectorAll("[data-trade-row]")],area.getBoundingClientRect().top+8);
      if(!target){last=null;return;}
      if(target.row.dataset.tradeRow===last)return;
      last=target.row.dataset.tradeRow;
      area.scrollTop+=target.delta;
    },160);
  },options);
  const observer=new MutationObserver(()=>{if(!section.isConnected){clearTimeout(timer);abort.abort();observer.disconnect();}});
  observer.observe(area,{childList:true});
}
async function renderTrading() {
  const owner=state.user.id;
  const area = $("#liveContent");area.replaceChildren();
  if(state.tradingTabOwner!==state.user.id){state.tradingTab="orders";state.tradingTabOwner=state.user.id;}
  const tabs=node("div",undefined,"admin-tabs");tabs.setAttribute("role","tablist");tabs.setAttribute("aria-label","종목 매매 화면");area.append(tabs);
  for(const [key,label] of [["orders","매매 관리"],["realized","실현손익"]]){const button=node("button",label,"admin-tab");button.setAttribute("role","tab");button.setAttribute("aria-selected",String((state.tradingTab||"orders")===key));button.addEventListener("click",()=>{state.tradingTab=key;updateScreenHelp();renderTrading().catch(reportError);});tabs.append(button);}
  if(state.tradingTab==="realized"){await renderRealizedSales(area);return;}
  const section=panel("종목 매매 · 현금 / KRX"),intro=node("p","불러오는 중…","warning");area.append(section);section.append(intro);
  intro.hidden=true;
  const safetyBox=node("fieldset",undefined,"trade-safety-options"),safetyNote=node("p","안전 옵션 조회 중…","helper");safetyBox.append(node("legend","주문 안전 · 자동매매 실행 옵션"));section.prepend(safetyBox);safetyBox.append(safetyNote);
  const runtimeBox=await renderAutomaticRuntime(safetyBox);
  if(owner!==state.user?.id||!section.isConnected)return;
  function drawSafety(){if(!metadata?.safety)return;const value=metadata.safety;safetyBox.replaceChildren(node("legend","주문 안전 · 자동매매 실행 옵션"));
    for(const [key,label,readonly] of [["new_orders_locked","내 신규 주문 차단",false],["server_locked","서버 실전 주문 잠금",true],["automatic_locked","서버 자동매매 차단",true]]){
      const input=node("input"),field=node("label",label);input.type="checkbox";input.checked=value[key];input.disabled=readonly?state.user.role!=="admin":state.user.role==="viewer";field.prepend(input);safetyBox.append(field);
      if(readonly&&state.user.role==="admin")input.addEventListener("change",async()=>{
        const owner=state.user.id,locked=input.checked;
        if(!locked&&!confirm(`${label}을 해제할까요? 전체 사용자에게 적용됩니다. 다른 차단과 주문 승인 절차는 유지됩니다.`)){input.checked=value[key];return;}
        input.disabled=true;
        try{const result=await api("/trading/server-safety",{method:"PUT",body:JSON.stringify({version:value.server_safety_version,field:key,locked,...(!locked?{confirmation:"전체 사용자 주문 차단 해제 확인"}:{})})});if(owner!==state.user?.id||!section.isConnected)return;metadata.safety=result;metadata.live_enabled=!result.new_orders_locked&&!result.server_locked;drawSafety();}
        catch(e){if(owner===state.user?.id&&section.isConnected){input.checked=value[key];safetyNote.textContent=e.message;}}
        finally{input.disabled=state.user?.role!=="admin";}
      });
      if(!readonly)input.addEventListener("change",async()=>{const locked=input.checked;if(!locked&&!confirm("본인의 신규 주문 차단을 해제할까요? 서버 잠금·승인 범위와 최종 주문 확인은 그대로 유지됩니다.")){input.checked=true;return;}input.disabled=true;
        try{const result=await api("/trading/safety",{method:"PUT",body:JSON.stringify({version:value.version,new_orders_locked:locked,...(!locked?{confirmation:"신규 주문 잠금 해제 확인"}:{})})});metadata.safety=result;metadata.live_enabled=!result.new_orders_locked&&!result.server_locked;drawSafety();safetyNote.textContent="내 안전 옵션 저장 완료 · 설정 변경만으로 주문은 전송되지 않습니다.";}
        catch(e){input.checked=value.new_orders_locked;safetyNote.textContent=e.message;}finally{input.disabled=state.user.role==="viewer";}});
    }
    safetyNote.textContent="위 세 옵션은 체크하면 차단됩니다. 서버 항목은 관리자만 변경하며 전체 사용자·테넌트에 적용됩니다. 서버 배포 설정의 안전 잠금은 화면에서 해제할 수 없습니다.";
    const explanations=node("div",undefined,"trade-safety-explanations");
    for(const text of ["내 신규 주문 차단: 내 수동·자동 신규 매수/매도를 모두 차단합니다. 다른 사용자에게는 영향이 없습니다.","서버 실전 주문 잠금: 모든 사용자의 수동·자동 신규 매수/매도를 차단하는 전체 긴급 잠금입니다.","서버 자동매매 차단: 모든 사용자의 자동 신규 매수·익절·손절 주문만 차단합니다. 수동 주문은 다른 잠금이 해제된 경우 가능합니다.","차단은 기존 접수 주문을 자동 취소하지 않습니다. 체결 확인은 계속하며, 서버 배포 잠금·계좌 권한 등 별도 제한은 유지됩니다."])explanations.append(node("p",text,"helper"));
    safetyBox.append(safetyNote,explanations,runtimeBox);
  }
  const message=node("p","","helper");message.setAttribute("role","status");section.append(message);
  const controls=node("div",undefined,"trade-add-controls");section.append(controls);
  function choice(label,values,value,parent=controls,subject=""){const field=node("label",subject?label.slice(subject.length).trim():label),select=node("select");select.setAttribute("aria-label",label);for(const [key,text] of values){const option=node("option",text);option.value=key;select.append(option);}select.value=String(value);field.append(select);parent.append(field);return select;}
  const broker=choice("주문 증권사",[["namuh","나무증권"],["kiwoom","영웅문 · 키움증권"]],state.tradeBroker||"namuh");
  const accounts=choice("주문 계좌",[["","나무 계좌 목록 확인 필요"]],"");
  const accountButton=node("button","계좌 목록 확인","secondary");controls.append(accountButton);
  accountButton.disabled=state.user.role==="viewer";
  accountButton.addEventListener("click",async()=>{accountButton.disabled=true;try{const data=await api("/portfolio/accounts");accounts.replaceChildren();for(const row of data.accounts){const option=node("option",`${data.environment==='mock'?'모의':'실전'} · 끝자리 ${row.account_no.slice(-4)}`);option.value=row.account_no;accounts.append(option);}}catch(error){message.textContent=error.message;}finally{accountButton.disabled=state.user.role==="viewer";}});
  function brokerChanged(){const kiwi=broker.value==="kiwoom";state.tradeBroker=broker.value;accounts.disabled=kiwi;accountButton.disabled=kiwi||state.user.role==="viewer";persistScreenState();}broker.addEventListener("change",brokerChanged);brokerChanged();
  const instruments={items:[]};let watchPage=1,watchTotal=Infinity;
  while(instruments.items.length<watchTotal){const page=await api(`/watchlist?page=${watchPage}&page_size=10`);watchTotal=page.total;instruments.items.push(...page.items.map(i=>i.instrument));if(!page.items.length)break;watchPage++;}
  if(owner!==state.user?.id||!section.isConnected)return;
  const stock=choice("관심종목에서 매매 종목 선택",instruments.items.map(i=>[i.code,`${i.name} · ${i.code}`]),instruments.items[0]?.code||"");
  if(instruments.items.some(v=>v.code===state.tradeCode))stock.value=state.tradeCode;stock.addEventListener("change",()=>{state.tradeCode=stock.value;persistScreenState();});
  const add=node("button","매매 목록에 추가","primary");controls.append(add);add.disabled=state.user.role==="viewer"||!instruments.items.length;
  if(!instruments.items.length){message.textContent="관심종목이 없습니다. 관심종목 화면에서 먼저 등록하세요.";const go=node("button","관심종목 관리","secondary");go.addEventListener("click",()=>openView("watchlist"));controls.append(go);}
  const archivedArea=node("details");archivedArea.hidden=true;section.append(archivedArea);
  const tableArea=node("div"),orderPanel=panel("주문 이력 · 접수와 체결 구분"),orderArea=node("div",undefined,"trade-order-log"),cancelArea=node("div"),logTools=node("div",undefined,"trade-log-tools"),clearLog=node("button","화면 비우기","secondary"),restoreLog=node("button","이력 다시 조회","secondary");
  orderArea.setAttribute("role","region");orderArea.setAttribute("aria-label","주문 이력 로그");orderArea.tabIndex=0;
  logTools.append(clearLog,restoreLog,node("small","화면만 비웁니다. DB의 주문·체결 기록은 보존됩니다.","helper"));
  orderPanel.append(logTools,orderArea,cancelArea);section.append(tableArea);area.append(orderPanel);
  clearLog.addEventListener("click",()=>orderArea.replaceChildren(node("p","화면을 비웠습니다. ‘이력 다시 조회’를 누르면 DB 기록을 다시 표시합니다.","helper")));
  restoreLog.addEventListener("click",async()=>{restoreLog.disabled=true;try{await load();}finally{restoreLog.disabled=false;}});
  let inlineMessage=null;
  tableArea.addEventListener("click",event=>{const btn=event.target.closest("button");if(!btn||!(/매수|매도/.test(btn.textContent)))return;inlineMessage=btn.parentElement.querySelector(".order-feedback");if(!inlineMessage){inlineMessage=node("p","","order-feedback warning");inlineMessage.setAttribute("role","status");btn.after(inlineMessage);}inlineMessage.textContent="요청 처리 중… 최종 확인 전에는 주문이 전송되지 않습니다.";},true);
  const messageObserver=new MutationObserver(()=>{if(inlineMessage?.isConnected)inlineMessage.textContent=message.textContent;else if(!section.isConnected)messageObserver.disconnect();});messageObserver.observe(message,{childList:true,characterData:true,subtree:true});
  let metadata,loading=false,reloadAgain=false;
  async function saveNew(item){const archived=(metadata?.archived_items||[]).find(p=>p.code===item?.code&&p.broker===broker.value);if(archived){await api(`/trading/plans/${archived.id}/restore`,{method:"POST",body:JSON.stringify({version:archived.version})});}else{if(broker.value==="namuh"&&!accounts.value)throw Error("주문 계좌 목록을 확인하고 계좌를 선택하세요.");await api("/trading/plans",{method:"PUT",body:JSON.stringify({code:item.code,broker:broker.value,account_no:broker.value==="namuh"?accounts.value:""})});}if(section.isConnected&&owner===state.user?.id)await load();}
  add.addEventListener("click",async()=>{if(owner!==state.user?.id)return;try{await buttonTask(add,"매매 목록에 추가 중…",()=>saveNew(instruments.items.find(i=>i.code===stock.value)));if(owner===state.user?.id&&section.isConnected)message.textContent="매매 목록 저장 완료 · 기존 기록 유지 · 자동매매는 별도 시작 승인이 필요합니다.";}catch(error){if(owner===state.user?.id&&section.isConnected)message.textContent=error.message;}finally{if(owner===state.user?.id)add.disabled=state.user.role==="viewer";}});
  async function changeListing(plan,action,button){
    if(owner!==state.user?.id||!section.isConnected)return;
    if(action==="archive"&&!window.confirm(`${plan.name}을 매매 목록에서 제거할까요? 자동매매는 중지됩니다. 보유종목은 변경하지 않으며 기존 주문은 취소하지 않습니다. 주문·체결·실현손익 기록은 보존되고 기존 주문의 체결 확인도 계속됩니다.`))return;
    try{await buttonTask(button,action==="archive"?"목록 제거 중…":"재추가 중…",async()=>{await plan._settingsPending;if(owner!==state.user?.id||!section.isConnected)return;await api(`/trading/plans/${plan.id}/${action}`,{method:"POST",body:JSON.stringify({version:plan.version})});if(owner!==state.user?.id||!section.isConnected)return;await load();message.textContent=action==="archive"?"매매 목록에서 제거했습니다. 기존 주문·체결·실현손익은 보존되며 접수 주문을 자동 취소하지 않습니다.":"기존 기록·설정을 유지한 채 재추가했습니다. 자동매매는 시작·예약 확인 후 별도로 승인하세요.";});}catch(error){if(owner===state.user?.id&&section.isConnected)message.textContent=error.message;}
  }
  const refresh=node("button","목록·저장 분석 새로고침","secondary");section.append(refresh);refresh.addEventListener("click",()=>load().catch(error=>message.textContent=error.message));
  let adjustmentBusy=false;
  async function adjustPosition(plan,side,quantity,button,limitPrice){
    if(adjustmentBusy||owner!==state.user?.id||!section.isConnected)return;
    const originalLabel=button.textContent;adjustmentBusy=true;button.disabled=true;button.setAttribute("aria-busy","true");button.textContent="주문 확인 중…";button.prepend(node("span",undefined,"spinner inline-spinner"));
    let dialog;
    const valid=()=>owner===state.user?.id&&section.isConnected;
    try{
      await plan._settingsPending;if(!valid())return;
      const intent=await api("/trading/orders/preview",{method:"POST",body:JSON.stringify({plan_id:plan.id,side,quantity:quantity.value?Number(quantity.value):null,request_key:requestIdentifier(),purpose:"position_adjustment",limit_price:limitPrice})});
      if(!valid())return;
      if(!metadata.live_enabled){message.textContent="실전 주문 안전잠금으로 전송하지 않았습니다.";return;}
      await new Promise(resolve=>{
        dialog=node("dialog",undefined,"trade-confirm-dialog");const yes=node("button","실전 주문 전송","primary"),no=node("button","취소","secondary"),label=side==="buy"?"추가매수":"일부매도";
        let confirmed=false;
        dialog.append(node("h3",label+" · 최종 확인"),node("p",`${plan.broker==="namuh"?"나무증권":"영웅문"} · 계좌 ${plan.account_last4||"API 연결 계좌"} · ${plan.name}(${plan.code})`),node("p",`조회가 ${priceText(intent.reference_price)} · 시세 조회 ${intent.reference_quote_at?formatTime(intent.reference_quote_at):"시각 미확인"}`),node("strong",`${label} ${intent.quantity}주 · ${intent.limit_price?priceText(intent.limit_price)+" 지정가":"시장가"}`),node("p","20초 내 직접 승인해야 전송됩니다. 접수는 체결 완료가 아닙니다. 체결 확인 전에는 같은 증권사의 자동·추가 주문을 차단합니다. 체결 후 증권사 잔고로 수량·평균매입가를 다시 확인합니다.","warning"),yes,no);
        dialog.addEventListener("close",()=>{if(!confirmed&&valid())api(`/trading/orders/${intent.id}/dismiss`,{method:"POST"}).catch(()=>{});dialog.remove();resolve();},{once:true});
        no.addEventListener("click",()=>dialog.close());
        yes.addEventListener("click",async()=>{
          if(!valid()){dialog.close();return;}confirmed=true;yes.disabled=no.disabled=true;yes.setAttribute("aria-busy","true");yes.textContent="전송 확인 중…";yes.prepend(node("span",undefined,"spinner inline-spinner"));
          try{const result=await api("/trading/orders/confirm",{method:"POST",body:JSON.stringify({intent_id:intent.id,confirmation:"실전 주문 확인"})});if(valid()){message.textContent=`주문 상태 ${result.status} · ${result.note||""} · 접수 후 체결·잔고 확인 필요`;await load();}}
          catch(error){if(valid())message.textContent=error.message;}finally{dialog.close();}
        });
        document.body.append(dialog);dialog.showModal();
      });
    }catch(error){if(valid())message.textContent=error.message;}
    finally{adjustmentBusy=false;if(button.isConnected){button.disabled=state.user?.role==="viewer";button.removeAttribute("aria-busy");button.textContent=originalLabel;}}
  }
  async function load(anchor=captureTradeViewport(section)){if(owner!==state.user?.id||!section.isConnected)return;if(loading){reloadAgain=true;return;}loading=true;refresh.disabled=true;try{
    metadata=await api("/trading/plans");if(!section.isConnected||owner!==state.user?.id)return;
    metadata.items=stableTradePlans(metadata.items);
    const archived=metadata.archived_items||[];archivedArea.hidden=!archived.length;archivedArea.replaceChildren(node("summary",`제거된 매매 종목 ${archived.length}개 · 기록 보존·재추가`),node("p","목록에서만 제거된 종목입니다. 기존 설정·주문·체결·실현손익은 보존됩니다. 재추가만으로 주문이나 자동매매가 실행되지 않습니다.","helper"));
    for(const plan of archived){const line=node("div",undefined,"source-row"),restore=node("button","매매 목록에 재추가","secondary");restore.setAttribute("aria-label",`${plan.name} 매매 목록에 재추가`);restore.disabled=state.user.role==="viewer";restore.addEventListener("click",()=>changeListing(plan,"restore",restore));line.append(node("span",`${plan.name} · ${plan.code} · ${plan.broker==='namuh'?'나무':'영웅문'}`),restore);archivedArea.append(line);}
    intro.textContent=`${metadata.live_enabled?'실전 주문 연결 사용 가능 · 최종 확인 후 전송':'실전 주문 안전잠금 ON · 주문 전송 차단'} / 자동매매: ${metadata.automatic_note} / 텔레그램: ${metadata.telegram_configured?'설정됨':'미설정'}`;
    const predictions=await api("/portfolio/predictions");if(!section.isConnected||owner!==state.user?.id)return;
    tableArea.replaceChildren();const wrap=node("div",undefined,"table-wrap"),table=node("table",undefined,"trade-table"),head=node("tr");
    for(const name of ["종목 / 계좌","현재가 · 수익률 / 예상 수익률","공통 매매 수량","자동매수 · 추가매수","자동매도 · 일부매도","매매 실행 / 예약"])head.append(node("th",name));
    table.append(head);for(const plan of metadata.items){const row=node("tr"),name=node("td"),link=node("button",`${plan.name} · ${plan.code}`,"instrument-name");
    row.dataset.tradeRow=plan.id;
    link.addEventListener("click",()=>openInstrument({code:plan.code,name:plan.name,market:"KRX",asset_type:"stock"}));name.append(link,node("small",`${plan.broker==='namuh'?'나무':'영웅문'} · ${plan.account_last4||'API 연결 계좌'}`));row.append(name);const performance=attachTradingPerformance(name,plan);
      const analysis=node("div"),saved=predictions.items[plan.code],forecasts=saved?.forecasts||[];if(!saved)analysis.append(node("p","저장 예측 없음 · 종목상세에서 분석하세요.","helper"));else{analysis.append(node("small",`예측 ${saved.target} · 자료 ${saved.cutoff}${saved.target<seoulDate()?' · 이전 예측':''}`));for(const [label,value] of [["일봉",saved.daily],["주봉 마감",saved.weekly],["기간통합",forecasts[0]]])analysis.append(node("p",`${label}: ${value?.predicted_return==null?'추정 불가':movementText(value.predicted_return*100,'%')+' · '+priceText(Math.round(value.predicted_close))}`,movementClass(value?.predicted_return)));}
      const news=node("p","뉴스 평가 확인 중…","helper");analysis.append(news);api(`/news/today?code=${plan.code}&levels=5`).then(d=>{if(news.isConnected){news.textContent=d.total_sum==null?'뉴스 미평가':`뉴스 ${d.total_sum>0?'+':''}${d.total_sum}점`;news.className=movementClass(d.total_sum);}}).catch(()=>news.textContent="뉴스 평가 조회 불가");const forecastBox=tradeSettingBox("예상 수익률·가격 / 뉴스 평가",analysis);forecastBox.classList.add("trade-forecast-box");
      appendAutomaticActivity(name,plan,metadata);
      const remove=node("button","매매 목록에서 제거","secondary");remove.setAttribute("aria-label",`${plan.name} 매매 목록에서 제거`);remove.disabled=state.user.role==="viewer";remove.addEventListener("click",()=>changeListing(plan,"archive",remove));name.append(remove);
      const priceCell=node("td",undefined,"trade-current-price"),price=node("div","저장 시세 확인 중…","trade-quote");priceCell.append(tradeSettingBox("현재가 · 등락률",price),forecastBox);row.append(priceCell);
      let lastQuoteTime=-Infinity;
      const paintQuote=(snapshot)=>{
        if(!price.isConnected)return;
        const received=Date.parse(snapshot?.collected_at);if(Number.isFinite(received)&&received<lastQuoteTime)return;if(Number.isFinite(received))lastQuoteTime=received;
        const chart=snapshot?.chart||[],last=[...chart].filter(p=>!p.provisional).sort((a,b)=>a.date.localeCompare(b.date)).at(-1),current=snapshot?.price;
        performance.setQuote({...snapshot,price:current??last?.close});
        price.replaceChildren(node("strong",priceText(current??last?.close)),node("small",snapshot?.collected_at?`조회 ${formatTime(snapshot.collected_at)}`:"시세 없음"));
        const change=tradeDayChange(snapshot);
        if(change)price.append(node("p",`전일 대비 ${movementText(change.amount,"원")} (${movementText(change.rate,"%")})`,movementClass(change.amount)),node("small",`전일 종가 ${priceText(change.reference)}`));
        else price.append(node("small","전일 종가 대비 자료 없음"));
        const peak=threeMonthPeak(chart,current??last?.close,seoulDate());
        price.append(node("p",peak?`3개월 고가 대비 ${peak.change>0?"+":""}${peak.change.toFixed(2)}%`:"3개월 고가 미수집",peak?movementClass(peak.change):"helper"));
        price.append(node("small",`시세: ${plan.broker==='namuh'?'나무증권':'영웅문'} · ${snapshot?.stale?'지연 · 주문 판단 제외':'자동 갱신'}`));
      };
      api(`/trading/plans/${plan.id}/quote`).then(d=>paintQuote(d.snapshot)).catch(e=>price.replaceChildren(node("small",`시세 조회 불가 · ${e.message}`)));
      bindLivePrice(price,plan.code,paintQuote,plan.id);
      const modeCell=node("td"),fraction=choice(`${plan.name} 매매 비중`,[[100,"100%"],[50,"50%"],[20,"20%"],[10,"10%"]],plan.fraction,modeCell,plan.name);row.append(modeCell);
      const autoMax=node("input"),quantityLabel=node("label","매매 수량 (주) ");autoMax.type="number";autoMax.min="1";autoMax.max="10000000";autoMax.step="1";autoMax.value=plan.auto_max_quantity===1&&!plan.quantity_limit_explicit?"":String(plan.auto_max_quantity||"");autoMax.placeholder="비우면 비중 적용";autoMax.setAttribute("aria-label",`${plan.name} 매매 수량`);autoMax.disabled=state.user.role==="viewer";quantityLabel.append(autoMax);modeCell.append(quantityLabel,node("small","자동 신규매수·추가매수·일부매도에 이 수량을 사용합니다. 비우면 매수는 주문가능 현금, 매도는 매도가능 수량의 선택 비중을 적용합니다. 자동 익절·손절은 남은 보유분 전량을 매도합니다."));
      const conditionSupported=metadata.buy_condition_version===1;
      const buyCondition=choice(`${plan.name} 자동매수 가격 조건`,[["at_or_below","설정가 이하 · 지정가"],["at_or_above","설정가 이상 · 시장가"]],plan.buy_condition||"at_or_below",modeCell,plan.name);buyCondition.classList.add("trade-buy-condition");buyCondition.disabled=state.user.role==="viewer"||!conditionSupported;
      const priceLabel=node("label"),priceCaption=node("span"),priceInput=node("input"),buyHint=node("small");setupPriceInput(priceInput);priceInput.value=plan.buy_price?formatPriceInput(plan.buy_price):"";priceInput.setAttribute("aria-label",`${plan.name} 기준 매수가격`);priceInput.disabled=state.user.role==="viewer";priceLabel.append(priceCaption,priceInput);
      function buyConditionHint(){const above=buyCondition.value==="at_or_above";priceCaption.textContent=above?"매수 기준가 (원 · 이상)":"매수 지정가 (원 · 이하)";priceInput.placeholder=above?"예: 50,000원 이상 시 시장가":"매수 최대가격 필수";buyHint.textContent=above?"현재가가 기준가격 이상(같을 때 포함)이면 시장가로 매수합니다. 실제 체결가격 상한은 없습니다. 조건 방향 변경 시 자동매매가 정지되며 시작·예약을 다시 승인해야 합니다.":"현재가가 기준가격 이하(같을 때 포함)이면 기준가격 지정가로 매수합니다. 조건 방향 변경 시 시작·예약 재승인이 필요합니다.";}
      buyCondition.addEventListener("change",buyConditionHint);buyConditionHint();
      const sellPriceLabel=node("label","일부매도 지정가 (원)"),sellPrice=node("input");setupPriceInput(sellPrice);sellPrice.value=plan.manual_sell_price?formatPriceInput(plan.manual_sell_price):"";sellPrice.placeholder="비우면 시장가";sellPrice.setAttribute("aria-label",`${plan.name} 일부매도 지정가격`);sellPrice.disabled=state.user.role==="viewer";sellPriceLabel.append(sellPrice);
      const buyCell=node("td"),buyEnabled=node("input"),buyLabel=node("label","매수 사용","repeat-trading-option");buyEnabled.type="checkbox";buyEnabled.checked=plan.buy_enabled!==false;buyEnabled.disabled=state.user.role==="viewer";buyEnabled.setAttribute("aria-label",`${plan.name} 매수 사용`);buyLabel.prepend(buyEnabled);buyCell.append(buyLabel,node("small","정지 상태에서 새로 시작하면 보유 중에도 가격 조건에 따라 자동 매수할 수 있습니다. 전체 일시정지 후 재개에는 보유종목 추가매수를 하지 않습니다."));row.append(buyCell);buyCell.append(buyCondition.parentElement,priceLabel,buyHint);
      const manualBuyLabel=node("label","추가매수 지정가 (원)"),manualBuy=node("input");setupPriceInput(manualBuy);manualBuy.value=formatPriceInput(plan.manual_buy_price??plan.buy_price??"");manualBuy.placeholder="추가매수의 최대 체결가격";manualBuy.setAttribute("aria-label",`${plan.name} 추가매수 지정가격`);manualBuy.disabled=state.user.role==="viewer"||!conditionSupported;manualBuyLabel.append(manualBuy);buyCell.append(node("h4","수동 추가매수 · 별도 주문"),manualBuyLabel,node("small","아래 추가매수 버튼에만 적용되는 매수 상한입니다. 자동매수 기준가격과 별도이며 최종 승인 후 지정가 주문합니다."));
      const buy=node("button","추가매수","trade-buy");buy.setAttribute("aria-label",`${plan.name} 추가매수`);buy.disabled=state.user.role==="viewer";buyCell.append(buy);
      const sellCell=node("td"),profit=choice(`${plan.name} 자동 매도 이익률`,[[3,"3%"],[5,"5%"],[7,"7%"],[10,"10%"],[15,"15%"]],plan.take_profit,sellCell,plan.name);sellCell.append(node("small","보유 수익률이 선택값 이상이면 전량 매도"));row.append(sellCell);
      const takeProfit=node("input"),takeLabel=node("label","익절 사용","repeat-trading-option");takeProfit.type="checkbox";takeProfit.checked=plan.take_profit_enabled!==false;takeProfit.disabled=state.user.role==="viewer";takeProfit.setAttribute("aria-label",`${plan.name} 익절 사용`);takeLabel.prepend(takeProfit);sellCell.prepend(takeLabel);
      function profitFields(){profit.disabled=!takeProfit.checked||state.user.role==="viewer";}takeProfit.addEventListener("change",profitFields);profitFields();watchTradeSelection(plan,"take_profit_enabled",takeProfit,()=>takeProfit.checked?1:0);
      const stopLoss=node("input"),stopLabel=node("label","손절 사용","repeat-trading-option"),stopPrice=node("input");stopLoss.type="checkbox";stopLoss.checked=!!plan.stop_loss_enabled;stopLoss.setAttribute("aria-label",`${plan.name} 손절 사용`);stopLabel.prepend(stopLoss);sellCell.append(stopLabel);
      const stopRate=choice(`${plan.name} 손절 손실률`,[5,6,7,8,9,10,15,20].map(v=>[v,`${v}%`]),plan.stop_loss_rate||5,sellCell,plan.name),stopPriceLabel=node("label","손절 가격 (원 · 선택)");setupPriceInput(stopPrice);stopPrice.value=plan.stop_loss_price?formatPriceInput(plan.stop_loss_price):"";stopPrice.placeholder="비우면 손실률만 적용";stopPrice.setAttribute("aria-label",`${plan.name} 손절 가격`);stopPriceLabel.append(stopPrice);sellCell.append(stopPriceLabel,node("small","손절 조건 충족 시 남은 보유분 전량을 시장가 매도합니다. 실제 체결가는 달라질 수 있습니다."));
      function stopFields(){stopRate.disabled=stopPrice.disabled=!stopLoss.checked||state.user.role==="viewer";}stopLoss.disabled=state.user.role==="viewer";stopLoss.addEventListener("change",stopFields);stopFields();
      const sell=node("button","일부매도","trade-sell");sell.setAttribute("aria-label",`${plan.name} 일부매도`);sell.disabled=state.user.role==="viewer";sellCell.append(node("h4","수동 일부매도 · 별도 주문"),sellPriceLabel,node("small","지정가격 이상에서만 체결됩니다. 비우면 시장가입니다. 자동 익절·손절 조건과는 별개입니다."),sell,node("small","공통 수량·비중과 일부매도 지정가격을 사용합니다. 전량 청산하면 자동매매를 종료하며 재매수하지 않습니다."));
      const autoBuyBox=tradeSettingBox("자동매수",buyLabel,buyCondition.parentElement,priceLabel,buyHint,node("small","정지 후 새 시작에는 보유 상태에도 적용됩니다. 예약·일시정지 후 재개에는 보유종목 추가매수를 하지 않습니다."));
      if(!conditionSupported)autoBuyBox.append(node("p","서버 코드 반영 전 · 새 매수 조건은 아직 사용할 수 없습니다.","warning"));
      buyCell.replaceChildren(autoBuyBox,tradeSettingBox("수동 추가매수",manualBuyLabel,node("small","추가매수 버튼에만 사용하는 지정가 상한입니다. 자동매수의 기준가격·이상/이하 조건과 별개이며 최종 확인 후 주문합니다."),buy));
      sellCell.replaceChildren(tradeSettingBox("자동 익절",takeLabel,profit.parentElement,node("small","보유 수익률이 선택값 이상이면 남은 보유분 전량을 시장가 매도합니다.")),tradeSettingBox("자동 손절",stopLabel,stopRate.parentElement,stopPriceLabel,node("small","손절 조건 충족 시 남은 보유분 전량을 시장가 매도합니다. 일부매도 지정가와 무관합니다.")),tradeSettingBox("수동 일부매도",sellPriceLabel,node("small","이 버튼의 주문은 지정가격 이상에서만 체결됩니다. 비우면 시장가입니다. 공통 수량·비중을 사용하며 전량 청산 시 자동매매를 종료합니다."),sell));
      const quantityBox=tradeSettingBox("공통 수량·비중",...modeCell.childNodes);modeCell.replaceChildren(quantityBox);
      for(const [side,button,input] of [["buy",buy,manualBuy],["sell",sell,sellPrice]])button.addEventListener("click",()=>{
        if(!autoMax.checkValidity()||!input.checkValidity()){autoMax.reportValidity();input.reportValidity();return;}
        let limit;try{limit=manualLimitPrice(side==="buy"&&!conditionSupported?priceInput.value:input.value);if(side==="buy"&&!limit)throw Error("매수 지정가격을 입력하세요.");}catch(error){message.textContent=error.message;return;}
        adjustPosition(plan,side,autoMax,button,limit);
      });
      for(const [field,input,read] of [...(conditionSupported?[["buy_condition",buyCondition,()=>buyCondition.value],["manual_buy_price",manualBuy,()=>manualLimitPrice(manualBuy.value)]]:[]),["fraction",fraction,()=>Number(fraction.value)],["auto_max_quantity",autoMax,()=>autoMax.value?Number(autoMax.value):null],["buy_price",priceInput,()=>manualLimitPrice(priceInput.value)],["manual_sell_price",sellPrice,()=>manualLimitPrice(sellPrice.value)],["take_profit",profit,()=>Number(profit.value)],["buy_enabled",buyEnabled,()=>buyEnabled.checked?1:0],["stop_loss_enabled",stopLoss,()=>stopLoss.checked?1:0],["stop_loss_rate",stopRate,()=>Number(stopRate.value)],["stop_loss_price",stopPrice,()=>manualLimitPrice(stopPrice.value)]])watchTradeSelection(plan,field,input,read);
      const action=node("td");action.append(node("small","설정 자동저장 · 시작/예약은 별도 승인. 전량 매도 후 정지 · 기존 주문 유지."));
      appendAutomaticControls(action,plan,metadata,load,message,()=>{if(!autoMax.checkValidity())throw Error("매매 수량은 1주 이상의 정수로 입력하세요.");const buy_price=manualLimitPrice(priceInput.value);if(buyEnabled.checked&&!buy_price)throw Error("매수 지정가격을 입력하세요.");return {code:plan.code,broker:plan.broker,version:plan.version,mode:"automatic",fraction:Number(fraction.value),auto_max_quantity:autoMax.value?Number(autoMax.value):null,buy_enabled:buyEnabled.checked,buy_rule:"price",...(conditionSupported?{buy_condition:buyCondition.value}:{}),buy_price,take_profit:Number(profit.value),take_profit_enabled:takeProfit.checked,stop_loss_enabled:stopLoss.checked,stop_loss_rate:Number(stopRate.value),stop_loss_price:manualLimitPrice(stopPrice.value)};});
      row.append(action);prepareMobileTradeCard(row);table.append(row);}
    wrap.append(table);tableArea.append(wrap);if(!metadata.items.length)tableArea.append(node("p","상단에서 종목을 선택해 매매 목록에 추가하세요.","helper"));orderArea.replaceChildren();cancelArea.replaceChildren();for(const order of metadata.orders)orderArea.append(node("p",`${formatTime(order.created_at)} · ${order.name} · ${order.side==='buy'?'매수':'매도'} ${order.quantity}주 · ${order.status} · ${order.note||'확인 대기'} · 알림 ${order.notification}${orderRealizedText(order)}`));if(!metadata.orders.length)orderArea.append(node("p","주문 이력이 없습니다.","helper"));
    for(const o of metadata.orders.filter(o=>o.status==="filled"))orderArea.append(node("p",`${o.name} · 체결 완료 ${o.filled_quantity}주 · 체결가 ${priceText(o.fill_price)} · 미체결 ${o.remaining_quantity}주 · 이메일 ${(o.email_notification||[]).join(", ")} · 텔레그램 ${o.notification}`,"helper"));
    head.lastElementChild.textContent="매매 실행 / 예약";
    for(const o of metadata.orders.filter(o=>["accepted","partial"].includes(o.status)&&["kiwoom","namuh"].includes(o.broker))){
      const cancel=node("button",`${o.name} 주문 잔량 취소 · 최종 확인`,"secondary");cancel.disabled=state.user.role==="viewer"||metadata.safety.server_locked;
      cancel.addEventListener("click",()=>{const dialog=node("dialog",undefined,"trade-confirm-dialog"),yes=node("button","실전 취소 전송","primary"),no=node("button","닫기","secondary");
        dialog.append(node("h3","미체결 잔량 취소 최종 확인"),node("p",`${o.name}(${o.code}) · 원주문 ${o.broker_order_no} · ${o.side==='buy'?'매수':'매도'} · 주문 ${o.quantity}주 · 지정가 ${priceText(o.limit_price)}`),node("p","전송 직전 잔량을 재조회합니다. 이미 체결된 수량은 취소할 수 없습니다. 취소 접수는 취소 완료가 아닙니다.","warning"),yes,no);document.body.append(dialog);
        no.addEventListener("click",()=>{dialog.close();dialog.remove();});yes.addEventListener("click",async()=>{yes.disabled=no.disabled=true;try{const result=await api(`/trading/orders/${o.id}/cancel`,{method:"POST",body:JSON.stringify({confirmation:"실전 취소 확인"})});message.textContent=result.note;await load();}catch(e){message.textContent=e.message;}finally{dialog.close();dialog.remove();}});dialog.showModal();});cancelArea.append(cancel);
    }
    }finally{loading=false;refresh.disabled=false;drawSafety();if(section.isConnected&&owner===state.user?.id){requestAnimationFrame(()=>requestAnimationFrame(()=>{if(owner===state.user?.id)restoreTradeViewport(section,anchor);}));if(reloadAgain){reloadAgain=false;await load(anchor);}}}}
  await load();monitorTradingWorker(section,metadata);installTradeScrollSnap(section);
  const fillCheck=node("button","증권사 체결 확인 · 주문 전송 없음","secondary");
  fillCheck.disabled=state.user.role==="viewer";
  fillCheck.addEventListener("click",async()=>{fillCheck.disabled=true;try{
    for(const order of metadata.orders.filter(o=>["accepted","partial","cancel_pending","cancel_unknown"].includes(o.status)))
      await api(`/trading/orders/${order.id}/reconcile`,{method:"POST"});
    await load();monitorTradingWorker(section,metadata);message.textContent="체결 조회 완료 · 전량 체결만 매수/매도 완료로 처리합니다.";
  }catch(e){message.textContent=e.message;}finally{fillCheck.disabled=state.user.role==="viewer";}});
  orderPanel.before(fillCheck);
  const fillTimer=setInterval(async()=>{
    if(!section.isConnected||state.view!=="orders"){clearInterval(fillTimer);return;}
    if(document.hidden||loading||fillCheck.disabled||state.user.role==="viewer")return;
    if(metadata.orders.some(o=>["accepted","partial","cancel_pending","cancel_unknown"].includes(o.status)))fillCheck.click();
  },10000);
  $("#liveInspector").replaceChildren(node("p","TRADING","eyebrow"),node("h2","종목 매매"),node("p","현금·KRX·시장가/지정가를 지원합니다. 신용·미수·공매도는 사용하지 않습니다.","helper"),node("p","주문 응답 불명 시 재전송하지 않습니다. 접수와 체결을 동일하게 표시하지 않습니다.","warning"));
}
async function renderRealizedSales(area){
  const box=panel("매도 체결 실현손익 · 수수료·세금 제외"),controls=node("div",undefined,"source-row"),output=node("div"),note=node("p","","helper");area.append(box);box.append(controls,note,output);
  const start=node("input"),end=node("input"),period=node("select"),broker=node("select"),code=node("input"),reload=node("button","조회","secondary"),sync=node("button","과거 체결 반영","secondary");
  start.type=end.type="date";end.value=seoulDate();start.value=`${end.value.slice(0,4)}-01-01`;start.setAttribute("aria-label","실현손익 시작일");end.setAttribute("aria-label","실현손익 종료일");
  period.setAttribute("aria-label","실현손익 집계 단위");for(const [key,label] of [["day","일일"],["week","주간 (월요일 시작)"],["month","월간"],["year","연간"]]){const option=node("option",label);option.value=key;period.append(option);}
  broker.setAttribute("aria-label","실현손익 증권사");for(const [key,label] of [["all","전체 증권사"],["namuh","나무증권"],["kiwoom","영웅문"]]){const option=node("option",label);option.value=key;broker.append(option);}
  code.placeholder="종목코드 (선택)";code.setAttribute("aria-label","실현손익 종목코드");code.maxLength=6;controls.append(start,end,period,broker,code,reload,sync);sync.disabled=state.user.role==="viewer";
  const owner=state.user.id;let version=0;const valid=()=>box.isConnected&&state.user?.id===owner;
  function stockBreakdown(data){
    const section=node("section"),select=node("select"),result=node("div");select.setAttribute("aria-label","종목별 실현손익 선택");
    const placeholder=node("option","종목을 선택하세요");placeholder.value="";select.append(placeholder);
    for(const stock of data.stocks||[]){const option=node("option",`${stock.name} · ${stock.code} (${stock.sales}건)`);option.value=stock.code;select.append(option);}
    select.value=(data.stocks||[]).some(s=>s.code===state.realizedCode)?state.realizedCode:"";
    section.append(node("h3","종목별 실현손익 합산"),node("p","위 조회 기간·증권사·종목코드 조건에 해당하는 전체 기록을 합산합니다. 목록에서 제거·재추가한 기록도 포함하며 수수료·세금은 제외합니다. 수익률은 손익 합계 ÷ 매입원가 합계입니다.","helper"),select,result);output.append(section);
    function draw(){
      if(!valid())return;result.replaceChildren();const stock=(data.stocks||[]).find(s=>s.code===select.value);if(!stock){result.append(node("p","종목을 선택하면 개별 실현손익과 합계를 표시합니다.","helper"));return;}
      result.append(node("h4",`${stock.name} · ${stock.code} · ${stock.sales}건`));
      if(stock.missing)result.append(node("p",`원가 미확인 ${stock.missing}건 · 확인된 기록 소계 ${priceText(stock.known_profit)} · 전체 손익 미확정`,"warning"));
      const table=node("table"),head=node("thead"),header=node("tr");table.setAttribute("aria-label","선택 종목 실현손익 기록 및 합계");for(const text of ["귀속일 · 주문일","증권사","매도 사유","체결 수량","매입원가","매도금액","실현손익","수익률"])header.append(node("th",text));head.append(header);table.append(head);
      const body=node("tbody");for(const item of data.items.filter(item=>item.code===stock.code)){
        const row=node("tr"),reason={manual:"수동 매도",take_profit:"자동 익절",stop_loss:"자동 손절",automatic:"자동 매도"}[item.reason]||item.reason;
        for(const text of [item.day,item.broker==="namuh"?"나무":"영웅문",reason,`${item.quantity.toLocaleString()}주`,item.cost==null?"확인 필요":priceText(item.cost),priceText(item.proceeds),item.profit==null?"확인 필요":priceText(item.profit),item.return_rate==null?"—":movementText(item.return_rate,"%")])row.append(node("td",text));body.append(row);
      }table.append(body);
      const foot=node("tfoot"),total=node("tr");for(const text of ["합계",`${stock.sales}건`,"수수료·세금 제외",`${stock.quantity.toLocaleString()}주`,stock.cost==null?"확인 필요":priceText(stock.cost),priceText(stock.proceeds),stock.profit==null?"확인 필요":priceText(stock.profit),stock.return_rate==null?"—":movementText(stock.return_rate,"%")])total.append(node("th",text));foot.append(total);table.append(foot);const wrap=node("div",undefined,"table-wrap");wrap.append(table);result.append(wrap);
    }
    select.addEventListener("change",()=>{if(!valid())return;state.realizedCode=select.value;persistScreenState();draw();});draw();
  }
  async function load(){const ticket=++version;try{const query=new URLSearchParams({start:start.value,end:end.value,period:period.value,broker:broker.value});if(code.value.trim())query.set("code",code.value.trim());const data=await api(`/trading/realized?${query}`);if(!valid()||ticket!==version)return;note.textContent=data.note;output.replaceChildren();const summary=data.summary;output.append(node("h3",`실현손익 ${summary.profit==null?"확인 필요":priceText(summary.profit)} · 수익률 ${summary.return_rate==null?"—":movementText(summary.return_rate,"%")}`));if(summary.missing)output.append(node("p",`원가 미확인 ${summary.missing}건 · 확인된 기록 소계 ${priceText(summary.known_profit)} · 전체 손익 미확정`,"warning"));
      const table=node("table"),header=node("tr");for(const label of ["기간","매도 건수","실현손익","수익률","확인 필요"])header.append(node("th",label));table.append(header);for(const group of data.groups){const row=node("tr");for(const text of [group.period,group.sales,group.profit==null?"확인 필요":priceText(group.profit),group.return_rate==null?"—":movementText(group.return_rate,"%"),group.missing])row.append(node("td",String(text)));table.append(row);}const summaryWrap=node("div",undefined,"table-wrap");summaryWrap.append(table);output.append(summaryWrap,node("h3","매도 체결 원장 · 최근 200건"));
      const details=node("table"),head=node("tr");for(const label of ["귀속일 · 주문일","종목 / 증권사","매도 사유","체결 수량","평균매입가","평균체결가","실현손익","수익률"])head.append(node("th",label));details.append(head);for(const item of data.items.slice(0,200)){const row=node("tr");const reason={manual:"수동 매도",take_profit:"자동 익절",stop_loss:"자동 손절",automatic:"자동 매도"}[item.reason]||item.reason;for(const text of [item.day,`${item.name} · ${item.code} / ${item.broker==="namuh"?"나무":"영웅문"}`,reason,`${item.quantity.toLocaleString()}주`,priceText(item.purchase_price),priceText(item.fill_price),item.profit==null?"확인 필요":priceText(item.profit),item.return_rate==null?"—":movementText(item.return_rate,"%")])row.append(node("td",text));details.append(row);}const detailWrap=node("div",undefined,"table-wrap");detailWrap.append(details);output.append(detailWrap);if(!data.items.length)output.append(node("p","기간 내 저장된 매도 체결이 없습니다. 기존 주문 기록은 ‘과거 체결 반영’으로 추가할 수 있습니다.","helper"));
      stockBreakdown(data);
    }catch(error){if(valid()&&ticket===version)note.textContent=error.message;}}
  reload.addEventListener("click",load);for(const input of [start,end,period,broker,code])input.addEventListener("change",load);
  sync.addEventListener("click",async()=>{sync.disabled=true;try{await api("/trading/realized/sync",{method:"POST"});if(valid())await load();}catch(error){if(valid())note.textContent=error.message;}finally{sync.disabled=state.user?.role==="viewer";}});await load();
}
async function renderTradingSettings(area) {
  await renderTradeNotificationChannels(area);
  area.append(node("h2", "매매 · 텔레그램 알림"));
  const status = node("p", "설정 조회 중…", "helper");
  const chat = node("input"); chat.type = "text"; chat.setAttribute("aria-label", "텔레그램 수신 채팅 ID");
  const enabled = node("input"); enabled.type = "checkbox";
  const chatLabel = node("label", "수신 채팅 ID"); chatLabel.append(chat);
  const enabledLabel = node("label", "매매 알림 사용"); enabledLabel.prepend(enabled);
  const save = node("button", "알림 설정 저장", "secondary");
  const verify = node("button", "봇 연결 확인 · 메시지 미전송", "secondary");
  const test = node("button", "테스트 알림 전송", "secondary");
  const message = node("p", "", "helper"); let policy;
  area.append(status, node("p", "위 ‘내 외부 서비스 연결’의 본인 텔레그램 설정만 사용합니다. 관리자도 동일하며 서버 .env 설정은 사용하지 않습니다. 봇 webhook은 변경하지 않습니다.", "helper"), chatLabel, enabledLabel, save, verify, test, message);
  function draw(value) {
    policy = value; chat.value = value.chat_id; enabled.checked = value.enabled;
    chat.readOnly = value.chat_id_source === "env" || value.personal;
    enabled.disabled = value.personal || state.user.role==='viewer';
    status.textContent = `봇 토큰 ${value.token_configured ? '설정됨' : '미설정'} · 실전 주문 ${value.live_orders_enabled ? '연결 허용' : '잠금'} · 자동 실전 실행 잠금`;
    save.disabled = !value.owner_matches || value.personal || state.user.role==='viewer';verify.disabled=!value.owner_matches||state.user.role==='viewer'; test.disabled = !value.owner_matches || !value.enabled || !value.chat_id || state.user.role==='viewer';
  }
  save.addEventListener("click", async () => {
    save.disabled = true;
    try { draw(await api("/admin/trading/notifications", {method:"PUT", body:JSON.stringify({version:policy.version, enabled:enabled.checked, chat_id:chat.value.trim()})})); message.textContent = "알림 설정 저장 완료"; }
    catch(error) { message.textContent = error.message; } finally { save.disabled = !policy.owner_matches; }
  });
  verify.addEventListener("click", async () => {
    verify.disabled = true;
    try { const value = await api("/admin/trading/notifications/verify", {method:"POST"}); message.textContent = `봇 연결 확인: @${value.bot_username || '연결됨'} · 메시지를 전송하지 않았습니다.`; }
    catch(error) { message.textContent = error.message; } finally { verify.disabled = !policy.owner_matches; }
  });
  test.addEventListener("click", async () => {
    if (!window.confirm("저장된 수신 채팅으로 테스트 알림을 전송할까요?")) return;
    test.disabled = true;
    try { await api("/admin/trading/notifications/test", {method:"POST"}); message.textContent = "테스트 알림 전송 완료 · 실제 주문 없음"; }
    catch(error) { message.textContent = error.message; } finally { test.disabled = !policy.enabled || !policy.chat_id; }
  });
  area.append(node("p", "위 개인 연결에서 본인의 봇 토큰·채팅 ID와 사용 여부를 저장하세요. 개인 연결이 없으면 알림을 발송하지 않으며 서버 .env 연결로 대체하지 않습니다.", "helper"));
  draw(await api("/admin/trading/notifications"));
  await renderEmailLists(area);
}

async function renderEmailLists(area) {
  const section=node("section",undefined,"email-lists-card");
  section.append(node("h2", "이메일 수신 목록 · SMTP 발송"));area.append(section);
  const status=node("p","","helper"), select=node("select"), name=node("input"), emails=node("textarea"), enabled=node("input"), subject=node("input"), body=node("textarea"), message=node("p","","helper");
  enabled.type="checkbox";
  let items=[], current=null;
  function label(text,input){const el=node("label",text,"email-list-field");el.append(input);return el;}
  const save=node("button","이메일 목록 저장","secondary"), remove=node("button","이메일 목록 삭제","secondary"), test=node("button","테스트 이메일 전송","secondary"), send=node("button","이메일 발송","secondary");
  const fields=node("div",undefined,"email-list-fields"),listActions=node("div",undefined,"connection-actions"),compose=node("div",undefined,"email-compose"),sendActions=node("div",undefined,"connection-actions");
  emails.rows=4;body.rows=4;
  const notifyLabel=node("label",undefined,"email-list-checkbox");notifyLabel.append(enabled,node("span","이 목록에 매매 알림 전송 (종목·수량 포함)"));
  fields.append(label("수신 목록 선택",select),label("목록 이름",name),label("수신 이메일 · 한 줄에 한 주소 또는 쉼표로 구분",emails),notifyLabel);
  listActions.append(save,remove);
  compose.append(node("h3","선택한 목록에 이메일 보내기"),label("제목",subject),label("본문",body));sendActions.append(test,send);compose.append(sendActions);
  message.setAttribute("role","status");section.append(status,fields,listActions,compose,message);
  function choose(){current=items.find(v=>v.id===select.value)||null;name.value=current?.name||"";emails.value=current?.emails.join("\n")||"";enabled.checked=!!current?.trade_notifications;remove.disabled=test.disabled=send.disabled=!current||state.user.role==='viewer';}
  async function load(id){const value=await api("/admin/email-lists");items=value.items;status.textContent=`SMTP ${value.smtp_configured?'설정됨':'미설정'} · 주소는 수신자별 개별 발송되며 서로 노출되지 않습니다. 목록 삭제는 되돌릴 수 없습니다.`;select.replaceChildren(new Option("새 수신 목록","new"),...items.map(v=>new Option(v.name,v.id)));select.value=id||"new";choose();if(state.user.role==='viewer'){for(const el of [name,emails,enabled,subject,body,save,remove,test,send])el.disabled=true;}}
  select.addEventListener("change",choose);
  save.addEventListener("click",async()=>{save.disabled=true;try{const value=await api(`/admin/email-lists/${current?.id||'new'}`,{method:"PUT",body:JSON.stringify({version:current?.version||0,name:name.value,emails:emails.value.split(/[\n,;]+/).map(v=>v.trim()).filter(Boolean),trade_notifications:enabled.checked})});await load(value.id);message.textContent="이메일 목록 저장 완료";}catch(e){message.textContent=e.message;}finally{save.disabled=false;}});
  remove.addEventListener("click",async()=>{if(!current||!window.confirm("수신 목록을 영구 삭제할까요? 복구할 수 없습니다."))return;try{await api(`/admin/email-lists/${current.id}?version=${current.version}`,{method:"DELETE"});await load();message.textContent="목록 삭제 완료";}catch(e){message.textContent=e.message;}});
  async function dispatch(isTest){if(!current||!window.confirm(`저장된 목록 ${current.name} (${current.emails.length}명)으로 ${isTest?'테스트 메시지':'입력한 내용'}를 전송할까요?`))return;test.disabled=send.disabled=true;try{const value=await api(`/admin/email-lists/${current.id}/send`,{method:"POST",body:JSON.stringify({subject:isTest?'NaStack Trading 이메일 연결 테스트':subject.value,body:isTest?'이메일 연결 테스트입니다. 실제 주문은 실행하지 않았습니다.':body.value})});message.textContent=`${value.recipient_count}명 발송 완료`;}catch(e){message.textContent=e.message;}finally{test.disabled=send.disabled=!current;}}
  test.addEventListener("click",()=>dispatch(true));send.addEventListener("click",()=>dispatch(false));await load();
}
async function renderTradeMonitor(area) {
  const section=panel("매매 동작 모니터링 · 읽기 전용"),status=node("p","저장된 매매 상태 조회 중…","helper"),output=node("div"),refresh=node("button","매매 상태 새로고침","secondary"),go=node("button","종목 매매로 이동","secondary");
  section.append(status,output,refresh,go);area.append(section);go.addEventListener("click",()=>openView("orders"));let busy=false;
  async function load(){if(busy||!section.isConnected||state.view!=="dashboard")return;busy=true;refresh.disabled=true;try{const data=await api("/trading/plans");if(!section.isConnected)return;const unresolved=data.orders.filter(v=>["submitting","accepted","unknown"].includes(v.status));status.textContent=`조회 ${formatTime(new Date().toISOString())} · 15초 간격 저장 상태 확인 · 시세/체결 실시간 스트림 아님`;output.replaceChildren(node("p",`실전 주문 ${data.live_enabled?'연결 허용':'안전잠금 ON'} · 자동 실전 실행 잠금`,"warning"),node("p",`등록 ${data.items.length}종목 · 자동 조건 ${data.items.filter(v=>v.mode==='automatic').length}종목 · 확인 필요 주문 ${unresolved.length}건`));
    for(const plan of data.items)output.append(node("p",`${plan.name} · ${plan.broker==='namuh'?'나무증권':'영웅문'} · ${plan.mode==='automatic'?'자동 조건 저장 (실행 잠금)':'수동매매'} · 비중 ${plan.fraction}%`));
    output.append(node("h3","최근 주문"));if(!data.orders.length)output.append(node("p","주문 이력이 없습니다.","helper"));
    const labels={preview:"미리보기 · 미전송",submitting:"전송 확인 중",accepted:"접수 · 체결 미확인",unknown:"응답 불명 · 재전송 금지"};
    for(const order of data.orders.slice(0,5))output.append(node("p",`${formatTime(order.created_at)} · ${order.name} · ${order.side==='buy'?'매수':'매도'} ${order.quantity}주 · ${labels[order.status]||order.status}`,order.status==='unknown'?'warning':'helper'));
  }catch(error){status.textContent=`상태 조회 실패: ${error.message}`;}finally{busy=false;refresh.disabled=false;}}
  refresh.addEventListener("click",load);await load();const timer=setInterval(()=>{if(!section.isConnected||state.view!=="dashboard"){clearInterval(timer);return;}if(!document.hidden)load();},15000);
}
async function renderPersonalBrokerKeys(area,broker,label) {
  const section=panel(`${label} 연결`),status=node("p","","helper"),key=node("input"),secret=node("input"),save=node("button","키 저장 후 연결·동작 확인","primary"),verify=node("button","저장된 키로 연결·동작 확인","secondary"),message=node("p","","helper"),fields=node("div",undefined,"broker-key-fields"),buttons=node("div",undefined,"broker-key-actions");
  const disconnect=node("button","연결 사용 중지","secondary"),owner=state.user.id;let policy=await api(`/user/brokers/${broker}/keys`),connection=await api(`/user/brokers/${broker}`),busy=false;
  section.classList.add("personal-broker-card");message.setAttribute("role","status");message.setAttribute("aria-live","polite");
  key.type=secret.type="password";key.autocomplete=secret.autocomplete="new-password";key.setAttribute("aria-label",`${label} 앱키`);secret.setAttribute("aria-label",`${label} 시크릿키`);
  key.placeholder=secret.placeholder="등록·변경할 때만 입력";
  for(const [text,input] of [["앱키",key],["시크릿키",secret]]){const field=node("label",text);field.append(input);fields.append(field);}
  function draw(){
    status.textContent=`개인 키 ${policy.configured?'등록됨':'미등록'} · 연결 ${connection.enabled?'허용':'사용 안 함'}${connection.token_expires_at?' · 토큰 만료 '+formatTime(connection.token_expires_at):' · 인증 확인 필요'}`;
    save.disabled=verify.disabled=disconnect.disabled=key.disabled=secret.disabled=busy||state.user.role==='viewer';verify.disabled ||= !policy.configured&&!policy.legacy_owner_only;disconnect.disabled ||= !connection.enabled;
    section.setAttribute("aria-busy",String(busy));
  }
  function currentOwner(){if(state.user?.id!==owner||!section.isConnected)throw new Error("사용자 또는 화면이 변경되어 확인을 중단했습니다.");}
  async function connectAndCheck(replaceKeys){
    if(replaceKeys&&(!key.value.trim()||!secret.value.trim())){message.textContent="앱키와 시크릿키를 모두 입력하세요.";return;}
    if(!replaceKeys&&(key.value||secret.value)){message.textContent="입력한 새 키를 사용하려면 ‘키 저장 후 연결·동작 확인’을 누르세요.";return;}
    busy=true;draw();let stage="키 확인";
    try{
      currentOwner();
      if(replaceKeys){stage="키 저장";message.textContent="1/3 · 개인 키 암호화 저장 중…";policy=await api(`/user/brokers/${broker}/keys`,{method:"PUT",body:JSON.stringify({version:policy.version,app_key:key.value.trim(),app_secret:secret.value.trim()})});key.value=secret.value="";}
      currentOwner();stage="연결 활성화";message.textContent="2/3 · 본인 실전 API 연결 확인 중…";
      connection=await api(`/user/brokers/${broker}`);currentOwner();
      if(!connection.enabled||connection.environment!=="live")connection=await api(`/user/brokers/${broker}`,{method:"PUT",body:JSON.stringify({version:connection.version,enabled:true,environment:"live"})});
      currentOwner();stage="인증";message.textContent="3/3 · 인증 토큰 확인 중…";
      connection=await api(`/user/brokers/${broker}/verify`,{method:"POST",body:JSON.stringify({version:connection.version})});currentOwner();
      if(broker==="namuh"?connection.last_probe?.status!=="completed":!connection.token_valid)throw new Error(connection.last_probe?.message||connection.last_error||"인증 상태를 확인하세요.");
      message.textContent="인증 성공 · 주문은 전송하지 않았습니다.";
      if(broker==="namuh"){
        stage="계좌 조회";const accounts=await api("/portfolio/accounts");currentOwner();
        message.textContent=`인증 성공 · 계좌 조회 성공 (${(accounts.items||accounts.accounts||[]).length}개). 현재가·잔고는 해당 화면에서 새로 조회하세요. 주문은 전송하지 않았습니다.`;
      }
    }catch(error){if(state.user?.id===owner&&section.isConnected)message.textContent=`${stage} 단계 실패 · ${error.message}${stage==='계좌 조회'?' · 인증은 성공했으나 계좌 조회는 실패했습니다.':''}`;}
    finally{busy=false;if(state.user?.id===owner&&section.isConnected)draw();}
  }
  buttons.append(save,verify,disconnect);section.append(status,node("p","새 키 입력 → 암호화 저장 → 본인 실전 연결 활성화 → 인증 확인을 이어서 진행합니다. 이미 저장한 키는 다시 입력하지 않아도 됩니다. 이 확인은 자동매매를 시작하거나 주문차단을 해제하지 않습니다. 연결 사용 중지는 조회·신규 주문 연결을 차단하지만 기존 주문을 취소하지 않습니다.","helper"),fields,buttons,message);area.append(section);draw();
  save.addEventListener("click",()=>connectAndCheck(true));verify.addEventListener("click",()=>connectAndCheck(false));
  disconnect.addEventListener("click",async()=>{busy=true;draw();try{currentOwner();connection=await api(`/user/brokers/${broker}`);currentOwner();connection=await api(`/user/brokers/${broker}`,{method:"PUT",body:JSON.stringify({version:connection.version,enabled:false,environment:connection.environment})});message.textContent="연결 사용 중지 완료 · 기존 주문은 취소하지 않았습니다.";}catch(error){if(owner===state.user?.id)message.textContent=error.message;}finally{busy=false;if(owner===state.user?.id&&section.isConnected)draw();}});
}
let screenStateSignature="",screenStateTimer=null,screenStateQueue=Promise.resolve();
function persistScreenState(){
  if(!state.user)return;
  const value={view:state.view,analysis_tab:state.analysisTab,data_tab:state.dataCollectionTab||"status",recommendations_market:state.recommendationsMarket||"000",holdings_tab:state.holdingsTab,return_period:state.returnPeriod,code:state.selectedInstrument?.code||"",news_code:state.newsCode||"",trade_broker:state.tradeBroker||"namuh",trade_code:state.tradeCode||"",realized_code:state.realizedCode||"",dimension:{...dimensionDraft},chart_start:Object.fromEntries(state.chartStartByCode),chart_end:Object.fromEntries(state.chartEndByCode),report:state.reportFilters||{}},signature=JSON.stringify(value);
  if(signature===screenStateSignature)return;clearTimeout(screenStateTimer);const owner=state.user.id;
  screenStateTimer=setTimeout(()=>{screenStateQueue=screenStateQueue.catch(()=>{}).then(async()=>{if(state.user?.id!==owner)return;await api("/settings/screen-state",{method:"PUT",body:signature});screenStateSignature=signature;}).catch(error=>{if(state.user?.id===owner)showToast(`화면 설정을 저장하지 못했습니다. ${error.message}`);});},600);
}
async function renderPersonalConnections(area){
  const owner=state.user?.id,tenant=state.user?.tenant_id;
  const valid=()=>state.user?.id===owner&&state.user?.tenant_id===tenant&&area.isConnected;
  const group=panel("내 외부 서비스 연결 · 암호화 저장");area.append(group);
  group.append(node("p",`현재 사용자: ${state.user.email} · 관리자도 본인의 연결과 알림 수신 목록을 별도로 설정합니다. 서버 로그인 인증메일 설정은 매매 알림에 사용하지 않습니다.`,"helper"));
  for(const [service,title,fields] of [["krx","KRX Open API · 지수 데이터",[["api_key","API 키",true]]],["ecos","한국은행 ECOS · 환율",[["api_key","API 키",true]]],["opendart","OpenDART · 공시·재무자료",[["api_key","API 키",true]]],["openai","OpenAI",[["api_key","API 키",true]]],["telegram","텔레그램",[["bot_token","봇 토큰",true],["chat_id","수신 채팅 ID",false]]],["smtp","이메일 SMTP",[["host","SMTP 서버",false],["port","TLS 포트",false],["username","SMTP 사용자명",false],["password","SMTP 비밀번호",true],["from_email","발신 이메일",false]]]]){
    let policy=await api(`/user/connections/${service}`);if(!valid())return;const section=node("details",undefined,"service-connection"),status=node("p","","helper"),enabled=node("input"),save=node("button","개인 연결 저장","secondary"),message=node("p","","helper"),inputs={},fieldGrid=node("div",undefined,"connection-fields"),actions=node("div",undefined,"connection-actions");section.dataset.service=service;enabled.type="checkbox";section.append(node("summary",title),status);const toggle=node("label","개인 연결 사용","connection-enabled");toggle.prepend(enabled);section.append(toggle,fieldGrid);
    for(const [name,label,secret] of fields){let input;if(name==='host'||name==='port'){input=node("select");const choices=name==='host'?policy.smtp_hosts:['587','465'];for(const value of choices)input.append(new Option(value,value));}else{input=node("input");input.type=secret?'password':'text';input.autocomplete=secret?'new-password':'off';if(secret)input.placeholder="저장된 비밀값 유지 시 비워두세요.";}input.setAttribute("aria-label",`${title} ${label}`);input.value=policy.fields[name]??(name==='port'?'587':name==='host'?policy.smtp_hosts[0]||'':'');inputs[name]=input;const field=node("label",label,"connection-field");field.append(input);fieldGrid.append(field);input.disabled=state.user.role==='viewer';}
    if(service==='smtp'){
      if(!policy.configured){inputs.username.value=inputs.from_email.value=state.user.email;const domain=state.user.email.split('@')[1]?.toLowerCase(),host={'gmail.com':'smtp.gmail.com','outlook.com':'smtp.office365.com','hotmail.com':'smtp.office365.com','naver.com':'smtp.naver.com','daum.net':'smtp.daum.net','hanmail.net':'smtp.daum.net'}[domain];if(host)inputs.host.value=host;}
      section.append(node("p","본인이 사용하는 이메일 계정의 공식 SMTP 안내를 확인해 서버·TLS 포트·사용자명·발신주소를 설정하세요. 기본 입력은 제안일 뿐이며 실제 발신 계정으로 수정할 수 있습니다. 서비스에서 앱 비밀번호를 요구하면 앱 비밀번호를 사용하세요. 현재 목록에 없는 SMTP 서버는 지원하지 않습니다. 수신 목록의 ‘매매 알림’을 켜야 주문 알림을 발송합니다.","helper"));
    }
    if(service==='krx'){const help=node("p","본인에게 발급된 KRX Open API 인증키를 등록하세요. 키 발급과 ‘파생상품지수 시세정보’ 등 개별 서비스 이용 승인은 별개입니다. 저장 후 ‘저장된 키로 연결 테스트’를 눌러 실제 VKOSPI 행을 확인하세요. 성공하면 다차원 데이터 국가·시장 목록의 KOSPI 위에 표시됩니다. 과거 자료는 수동 수집하며 1회 최대 5거래일씩 조회합니다. 저장만으로 외부 호출·주문·LLM 분석을 하지 않습니다. ","helper"),link=node("a","KRX Open API 서비스 신청·이용 안내");link.href="https://openapi.krx.co.kr/";link.target="_blank";link.rel="noopener noreferrer";help.append(link);section.append(help);inputs.api_key.maxLength=512;}
    if(service==='ecos'){const help=node("p","본인의 정식 ECOS 인증키를 등록하세요. 원/달러 매매기준율을 수집하며 FRED 뉴욕 정오 환율과 섞지 않습니다. 키 저장만으로 과거 자료 수집이나 LLM 분석을 실행하지 않습니다. ","helper"),link=node("a","ECOS 인증키 발급 안내");link.href="https://ecos.bok.or.kr/api/";link.target="_blank";link.rel="noopener noreferrer";help.append(link);section.append(help);}
    if(service==='opendart'){const help=node("p","발급 승인 후 본인의 40자리 인증키를 저장하세요. 저장만으로 외부 호출하지 않습니다. 연결 테스트와 종목별 재무자료 제공 확인은 별도입니다. 자료 확인은 선택한 보고서의 계정 응답만 확인하며 재무 시계열 저장·그래프·LLM 분석은 아직 실행하지 않습니다. ","helper"),link=node("a","OpenDART 인증키 신청·가이드");link.href="https://opendart.fss.or.kr/intro/main.do";link.target="_blank";link.rel="noopener noreferrer";help.append(link);section.append(help);}
    const test=['ecos','opendart','krx'].includes(service)?node("button","저장된 키로 연결 테스트","primary"):null;
    const financial=service==='opendart'?node("button","재무자료 제공 확인","primary"):null,financialInputs={};
    if(financial){const controls=node("div",undefined,"vector-controls");for(const [name,label,choices] of [["code","재무 확인 종목코드",null],["year","사업연도",null],["report","재무 보고서",[["11011","사업보고서"],["11013","1분기"],["11012","반기"],["11014","3분기"]]],["fs_div","재무제표 구분",[["CFS","연결"],["OFS","별도"]]]]){const input=node(choices?'select':'input');input.setAttribute('aria-label',label);if(choices){for(const [value,text] of choices)input.append(new Option(text,value));}else if(name==='code'){input.placeholder='6자리 종목코드';input.maxLength=6;input.inputMode='numeric';}else{input.type='number';input.min='2015';input.max=String(new Date().getFullYear());input.value=String(new Date().getFullYear()-1);}const field=node('label',label);field.append(input);controls.append(field);financialInputs[name]=input;}section.append(controls);}
    function draw(){enabled.checked=policy.enabled;status.textContent=`개인 연결 ${policy.configured?'등록됨':'미등록'} · 비밀값 ${policy.secret_configured?'저장됨':'미등록'} · 접속 성공 여부는 별도 확인`;save.disabled=enabled.disabled=state.user.role==='viewer';if(test)test.disabled=state.user.role==='viewer'||!policy.enabled||!policy.secret_configured;if(financial){financial.disabled=state.user.role==='viewer'||!policy.enabled||!policy.secret_configured;for(const input of Object.values(financialInputs))input.disabled=state.user.role==='viewer';}}
    if(service==='krx'&&policy.checks?.krx?.status==='connected')message.textContent=`이 키의 KRX 연결 확인 성공 · VKOSPI ${policy.checks.krx.latest_value} · 기준일 ${policy.checks.krx.latest_date}`;
    actions.append(save);section.append(node("p","설정 저장만으로 외부 호출·메시지 발송을 하지 않습니다. 비밀값은 본인 DB에 암호화하며 원문 재조회하지 않습니다. SMTP는 표시된 서버와 TLS 포트만 지원합니다. 개인 연결 사용을 끄고 저장하면 해당 서비스는 사용하지 않습니다.","helper"),actions,message);group.append(section);draw();
    if(test){actions.append(test);message.setAttribute("role","status");message.setAttribute("aria-live","polite");test.addEventListener("click",async()=>{if(!valid())return;if(inputs.api_key.value.trim()||enabled.checked!==policy.enabled){message.textContent="변경한 연결 설정을 먼저 저장한 뒤 테스트하세요.";return;}save.disabled=true;enabled.disabled=true;inputs.api_key.disabled=true;if(financial)financial.disabled=true;try{const result=await buttonTask(test,`${service==='krx'?'KRX':service==='ecos'?'ECOS':'OpenDART'} 연결 확인 중…`,()=>api(`/user/connections/${service}/test`,{method:"POST"}));if(!valid())return;message.textContent=service==='krx'?`KRX 연결 성공 · VKOSPI ${result.latest_value} · 기준일 ${result.latest_date} · 자료 저장 없음 · LLM 토큰 0개`:service==='ecos'?`연결 성공 · 최신 관측일 ${result.latest_date} · 1 USD = ${Number(result.latest_value).toLocaleString('ko-KR')}원 · 확인 ${result.count}건 · 자료 저장 없음 · LLM 토큰 0개`:'OpenDART 연결 성공 · 인증 응답 확인 · 재무자료 수집 성공과는 별도 · LLM 토큰 0개';}catch(error){if(valid())message.textContent=`연결 확인 실패 · ${error.message}`;}finally{if(valid()){inputs.api_key.disabled=state.user.role==='viewer';draw();}}});}
    function showFinancial(result){message.textContent=result.status==='available'?`재무자료 제공 확인 · ${result.company_name} · ${result.year}년 · ${result.fs_div==='CFS'?'연결':'별도'} · ${result.account_count}개 계정 · ${result.statements.join(', ')} · 시계열 저장 없음 · LLM 토큰 0개`:`재무자료 미확인 · ${result.message||'이전 확인 실패'} · LLM 토큰 0개`;}
    if(financial){actions.append(financial);const last=policy.checks?.opendart_financial;if(last){showFinancial(last);financialInputs.code.value=last.code||'';if(last.year)financialInputs.year.value=last.year;if(last.report)financialInputs.report.value=last.report;if(last.fs_div)financialInputs.fs_div.value=last.fs_div;}else if(policy.checks?.opendart?.status==='connected')message.textContent='이 키의 이전 OpenDART 연결 확인 성공 · 종목별 재무자료 제공 여부는 별도 확인하세요.';financial.addEventListener('click',async()=>{if(!valid())return;if(inputs.api_key.value.trim()||enabled.checked!==policy.enabled){message.textContent='변경한 연결 설정을 먼저 저장한 뒤 확인하세요.';return;}const query=Object.fromEntries(Object.entries(financialInputs).map(([name,input])=>[name,name==='year'?Number(input.value):input.value]));if(!/^\d{6}$/.test(query.code)||!Number.isInteger(query.year)||query.year<2015||query.year>new Date().getFullYear()){message.textContent='6자리 종목코드와 2015년 이후 사업연도를 확인하세요.';return;}save.disabled=enabled.disabled=test.disabled=inputs.api_key.disabled=true;for(const input of Object.values(financialInputs))input.disabled=true;try{const result=await buttonTask(financial,'재무자료 확인 중…',()=>api('/user/connections/opendart/financial-check',{method:'POST',body:JSON.stringify(query)}));if(valid())showFinancial(result);}catch(error){if(valid())message.textContent=`재무자료 확인 실패 · ${error.message}`;}finally{if(valid()){inputs.api_key.disabled=state.user.role==='viewer';draw();}}});}
    save.addEventListener("click",async()=>{if(!valid())return;save.disabled=true;try{const values=Object.fromEntries(Object.entries(inputs).map(([key,input])=>[key,input.value.trim()]));const result=await buttonTask(save,"개인 연결 저장 중…",()=>api(`/user/connections/${service}`,{method:'PUT',body:JSON.stringify({version:policy.version,enabled:enabled.checked,fields:values})}));for(const [name,,secret] of fields)if(secret)inputs[name].value='';if(!valid())return;policy=result;draw();message.textContent="암호화 저장 완료 · 설정 화면을 다시 열면 연결 상태에 반영됩니다.";}catch(error){if(valid())message.textContent=error.message;}finally{if(valid())save.disabled=state.user.role==='viewer';}});
  }
}
