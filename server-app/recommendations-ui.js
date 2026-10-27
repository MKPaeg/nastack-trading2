/* Daily movers, not personalized advice. Never register instruments or place orders. */
async function renderRecommendations(){
  const owner=state.user.id,area=$("#liveContent");area.replaceChildren();let timer=null,opening=false;
  const watchMembership=loadWatchMembership();
  const p=panel("추천종목 · 급등·급락 순위"),controls=node("div",undefined,"source-row"),market=node("select"),refresh=node("button","현재 순위 조회","primary"),note=node("p","저장 순위 조회 중…","helper"),output=node("div");
  market.setAttribute("aria-label","추천종목 시장");
  for(const [value,label]of [["000","전체"],["001","코스피"],["101","코스닥"]]){const o=node("option",label);o.value=value;market.append(o);}
  market.value=state.recommendationsMarket||"000";
  const valid=()=>state.user?.id===owner&&state.view==="recommendations"&&p.isConnected;
  p.append(node("p","정규장에 화면이 보이는 동안 약 3초마다 갱신합니다. 장외·휴장일에는 저장 결과를 표시하며 수동 조회만 가능합니다. 키움 KRX 전일 종가 대비 상위 각 20종목이며 수분 내 급변 감지나 매수 추천은 아닙니다. 종목 클릭 시 관심 등록 없이 상세로 이동하고, 최초 분석에는 사업설명·차원 초기화·뉴스·통계의 LLM·검색 API 비용이 발생할 수 있습니다. 개인 연결·모델·비용 한도 설정을 유지합니다.","helper"));
  const sessionNote=node("p","장 운영 여부 확인 중…","helper");
  controls.append(market,refresh);p.append(controls,sessionNote,note,output);area.append(p);
  const source=node("a","키움 공식 등락률 순위 API 안내");source.href="https://openapi.kiwoom.com/m/guide/apiguide/05/ka10027";source.target="_blank";source.rel="noopener noreferrer";p.append(source);
  let busy=false,manualBusy=false,available=false;
  function buttons(){const disabled=manualBusy||!available||state.user.role==="viewer";if(market.disabled!==manualBusy)market.disabled=manualBusy;if(refresh.disabled!==disabled)refresh.disabled=disabled;}
  function draw(data){
    output.replaceChildren();available=data.available!==false;const snapshot=data.snapshot;
    note.textContent=data.note||(!snapshot?"저장된 순위가 없습니다. 정규장 자동 조회 또는 수동 조회를 이용하세요.":`${snapshot.environment==="mock"?"모의투자 자료":"실전 시세"} · 조회 ${formatTime(data.collected_at)}${data.stale?" · 이전 저장 결과":""}${data.reused?" · 동일 구간 저장 결과 재사용":""}`);
    if(state.user.role==="viewer")note.append(node("span"," · 조회 전용: 본인의 저장 결과만 표시합니다."));
    if(!snapshot)return;
    output.append(node("p",`${snapshot.source} · ${snapshot.filters} · 첫 응답 페이지에서 각 최대 20종목 · 연속 조회 없음`,"helper"));
    for(const [key,title]of [["up","급등 · 상승률 상위"],["down","급락 · 하락률 상위"]]){
      const group=panel(title),table=node("table"),head=node("tr");for(const label of ["순위","종목","현재가","전일 대비","등락률","거래량","관심종목"])head.append(node("th",label));table.append(head);
      for(const [index,item]of (snapshot[key]||[]).entries()){
        const row=node("tr"),name=node("td"),open=node("button",`${item.name} · ${item.code}`,"instrument-name");open.disabled=state.user.role==="viewer";name.append(open);
        open.addEventListener("click",async()=>{
          if(opening||!valid())return;opening=true;clearTimeout(timer);
          try{await buttonTask(open,"상세 분석 준비 중…",async()=>{
            const entry=await api(`/recommendations/${item.code}/open`,{method:"POST"});if(!valid())return;
            const initialize=entry.initialize&&confirm("처음 조회하는 종목은 사업설명·다차원 초기값·트레이딩벡터를 준비하고 뉴스·통계를 수집·평가합니다. LLM·검색 비용이 발생할 수 있습니다. 개인 허용 설정과 한도는 유지하며 관심종목 추가·주문은 하지 않습니다. 초기 분석을 진행할까요?");
            openInstrument({...entry.instrument,recommendation_initialize:initialize},{chartEnd:"today"});
          });}catch(error){if(valid())note.textContent=error.message;}finally{opening=false;if(valid())schedule(3000);}
        });
        row.append(node("td",String(index+1)),name,node("td",priceText(item.price)),node("td",movementText(item.change,"원"),movementClass(item.change)),node("td",movementText(item.rate,"%"),movementClass(item.rate)),node("td",`${item.volume.toLocaleString("ko-KR")}주`));const watchCell=node("td");watchCell.append(watchlistAddControl(item,watchMembership,valid));row.append(watchCell);table.append(row);
      }
      if(!(snapshot[key]||[]).length)group.append(node("p","조건에 맞는 종목이 없습니다.","helper"));
      const wrap=node("div",undefined,"table-wrap");wrap.append(table);group.append(wrap);output.append(group);
    }
  }
  function schedule(delay){clearTimeout(timer);if(valid()&&!opening&&available&&state.user.role!=="viewer")timer=setTimeout(()=>{if(!valid())return;if(document.hidden){schedule(3000);return;}load(true);},delay);}
  async function load(collect=false,manual=false){
    if(busy||opening||!valid())return;clearTimeout(timer);let delay=3000;const requestedMarket=market.value;busy=true;manualBusy=manual;buttons();
    const request=async()=>{
      const session=await api("/market/session");if(!valid()||market.value!==requestedMarket)return;
      sessionNote.textContent=session.is_open?"정규장 · 약 3초마다 백그라운드 자동 갱신":"자동 갱신 중지 · "+(session.reason||"장 운영 여부 확인 불가")+" · 저장 결과 표시 · 수동 조회 가능";
      if(!session.is_open){delay=30000;if(collect&&!manual)return;}
      const data=await api(`/recommendations${collect?"/refresh":""}?market=${requestedMarket}${collect&&!manual?"&auto=true":""}`,collect?{method:"POST"}:{});if(valid()&&market.value===requestedMarket)draw(data);
    };
    try{if(manual)await buttonTask(refresh,"급등·급락 순위 조회 중…",request);else await request();
    }catch(error){delay=Math.max(3000,(error.retryAfter||30)*1000);if(valid()&&market.value===requestedMarket)note.textContent=`조회 실패: ${error.message} · ${Math.ceil(delay/1000)}초 후 재점검 · 이전 표의 조회 시각을 확인하세요.`;}finally{busy=false;manualBusy=false;if(valid()){buttons();if(market.value!==requestedMarket)load();else schedule(delay);}}
  }
  refresh.addEventListener("click",()=>load(true,true));market.addEventListener("change",()=>{state.recommendationsMarket=market.value;persistScreenState();load();});await load();
}

async function initializeRecommendedAnalysis(code,notify,valid){
  let saved=await api(`/vectors/defaults/${code}`);if(!valid())throw new Error("화면·계정 변경");
  if(!saved.profile||(saved.profile.origin==="llm_default"&&saved.profile.status!=="pending"&&!saved.profile.selected_dimensions?.length)){
    notify("추천종목 1회 승인 · 인터넷 사업조사·다차원 초기값 생성 중…");
    saved=await api(`/recommendations/${code}/initialize`,{method:"POST",body:JSON.stringify({confirmed:true,cloud_confirmed:true})});
  }
  for(let attempt=0;saved.profile?.status==="pending"&&attempt<150;attempt++){
    if(!valid())throw new Error("화면·계정 변경 · 이미 시작한 작업은 원래 소유자 영역에 보존됩니다.");
    notify("사업설명·초기 차원 준비 중 · 기존 작업 재사용");await new Promise(resolve=>setTimeout(resolve,2000));
    if(!valid())throw new Error("화면·계정 변경");saved=await api(`/vectors/defaults/${code}`);
  }
  if(!valid())throw new Error("화면·계정 변경");
  if(!saved.profile?.selected_dimensions?.length)throw new Error(saved.profile?.reason||"초기 차원을 생성하지 못했습니다. 개인 초기화·사업조사·모델 허용 설정을 확인하세요.");
  if(saved.profile.status==="pending")throw new Error("초기 설정 작업이 아직 진행 중입니다. 결과를 확인한 뒤 다시 진입하세요.");
  return saved.profile;
}
