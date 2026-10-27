function automaticActivity(plan,worker,safety={},at=Date.now()) {
  const status=plan.status||plan.worker_status||{},message=status.message||"";
  if(!plan.active)return {kind:"stopped",label:"자동매매 정지",animate:false,message};
  if(safety.automatic_paused)return {kind:"waiting",label:"자동매매 일시정지",animate:false,message:"회차 유지 · 매수·익절·손절 전송 대기 · 기존 주문 체결 확인 계속"};
  if(safety.new_orders_locked||safety.server_locked||safety.automatic_locked)return {kind:"blocked",label:"자동매매 주문차단",animate:false,message:"설정 활성 · 신규 매수·매도 전송 차단"};
  const tick=Date.parse(worker?.last_tick),fresh=Number.isFinite(tick)&&at-tick<=Math.max(20000,(worker?.poll_seconds||5)*4000)&&at-tick>=-5000;
  if(!worker?.running||!fresh)return {kind:"unknown",label:"워커 연결 확인 필요",animate:false,message:"실행 상태를 확인하지 못했습니다. 주문 진행을 의미하지 않습니다."};
  if(status.state==="blocked"||status.state==="error")return {kind:status.state,label:status.state==="error"?"자동매매 오류":"자동매매 차단",animate:false,message};
  if(status.state==="waiting"&&message.includes("조회 호출 제한"))return {kind:"waiting",label:"자동매매 조회 대기",animate:false,message};
  if(status.state==="waiting"&&message.includes("조회 순서 대기"))return {kind:"waiting",label:"자동매매 조회 대기",animate:false,message};
  if(message.includes("예약 시간"))return {kind:"waiting",label:"자동매매 예약 대기",animate:false,message};
  if(["accepted","partial","submitting","cancel_pending"].includes(status.state))return {kind:"checking",label:"자동 주문 체결 확인 중",animate:true,message};
  return {kind:"running",label:"자동매매 감시 중",animate:true,message:message||"매수·매도 조건 확인 중 · 주문 체결과는 별도"};
}

function paintAutomaticActivity(badge,plan,worker,safety,unavailable=false) {
  const view=unavailable?{kind:"unknown",label:"자동매매 상태 확인 지연",animate:false,message:"서버 연결을 확인하세요."}:automaticActivity(plan,worker,safety);
  const sameAnimation=badge.dataset.animated===String(view.animate)&&badge.children.length===2;
  const icon=sameAnimation?badge.children[0]:node("span",view.animate?"":"●",view.animate?"spinner":"auto-activity-dot");icon.setAttribute("aria-hidden","true");
  badge.className=`auto-activity auto-activity-${view.kind}`;badge.dataset.animated=String(view.animate);badge.title=view.message;
  if(sameAnimation)badge.children[1].textContent=view.label;
  else badge.replaceChildren(icon,node("span",view.label));
  badge.setAttribute("aria-label",`${view.label} · ${view.message}`);
}

function appendAutomaticActivity(cell,plan,metadata) {
  const badge=node("div");badge.dataset.autoActivity=plan.id;badge.setAttribute("role","status");badge.setAttribute("aria-live","polite");
  paintAutomaticActivity(badge,plan,metadata.worker,metadata.safety);cell.insertBefore(badge,cell.children[2]||null);
}

function appendAutomaticControls(action,plan,metadata,reload,message,readSettings) {
  const autoButtons=node("div",undefined,"auto-trade-controls"),start=node("button","자동매매 시작","secondary"),stop=node("button","정지","secondary"),reserve=node("button","예약","secondary");
  const autoNote=node("small",`${plan.active?"자동매매 활성":"자동매매 정지"} · ${plan.worker_status?.message||"실행 대기"}`);
  autoNote.dataset.autoPlan=plan.id;
  start.disabled=reserve.disabled=state.user.role==="viewer"||!metadata.automatic_enabled;
  start.title=reserve.title=metadata.automatic_enabled?"저장된 조건을 확인하고 승인합니다.":"서버 실전·자동매매 잠금 및 이전 테스트 승인 범위를 확인하세요.";
  stop.disabled=state.user.role==="viewer";
  stop.addEventListener("click",async()=>{stop.disabled=true;try{await plan._settingsPending;await api(`/trading/plans/${plan.id}/control`,{method:"POST",body:JSON.stringify({version:plan.version,action:"stop"})});message.textContent="자동매매 정지 저장 · 기존 주문의 체결 확인은 계속하며 자동 취소하지 않습니다.";await reload();}catch(e){autoNote.textContent=e.message;}finally{stop.disabled=state.user.role==="viewer";}});
  async function approve(scheduled){
    try{await plan._settingsPending;}catch(e){autoNote.textContent=`설정 저장을 먼저 확인하세요: ${e.message}`;return;}
    let settings;try{settings=readSettings?.();}catch(e){autoNote.textContent=e.message;return;}
    const entered={...plan,...settings};
    const dialog=node("dialog",undefined,"trade-confirm-dialog"),confirm=node("button",scheduled?"예약 실전 자동매매 승인":"실전 자동매매 시작 승인","primary"),close=node("button","닫기","secondary"),note=node("p","","helper");
    const time=node("input"),label=node("label","예약 시간 · 한국시간");time.type="time";time.min="09:00";time.max="15:19";time.step="60";time.value=plan.trade_time||"09:00";time.setAttribute("aria-label",`${plan.name} 예약 시간`);label.append(time);
    dialog.append(node("h3",`${plan.name} · 입력된 자동매매 조건 확인`),
      node("p",`${plan.broker==="namuh"?"나무증권":"영웅문"} · 계좌 끝자리 ${plan.account_last4||"API 연결 계좌"}`),
      node("p",`매매 비중 ${entered.fraction}% · 신규 매수수량 ${entered.auto_max_quantity?priceText(entered.auto_max_quantity,"주"):"별도 제한 없음 · 비중 적용"} · 목표수익 ${entered.take_profit}% 이상`),
      node("p",entered.buy_enabled===false?"매수 사용 안 함 · 기존 보유분의 익절·손절만 감시":entered.buy_condition==="at_or_above"?`현재가 ${priceText(entered.buy_price)} 이상이면 시장가 매수 · 실제 체결가는 기준가격보다 높을 수 있으며 가격 상한이 없습니다.`:`현재가 ${priceText(entered.buy_price)} 이하에서 지정가 매수`),
      node("p",scheduled?"예약은 기존 보유분에 자동 추가매수하지 않습니다. 보유분의 익절·손절은 계속 감시합니다.":plan.active?"이미 활성 상태인 설정을 다시 시작해도 기존 보유분에 자동 추가매수하지 않습니다.":"정지 상태에서 새로 시작하면 '매수 사용'이 켜진 보유종목도 가격 조건 충족 시 자동 추가매수합니다. 전체 일시정지 후 재개하면 보유종목 자동 추가매수는 취소됩니다.","warning"),
      node("p","보유분의 익절·손절 조건은 매수보다 우선합니다. 자동 매도 시 남은 보유분 전량을 매도합니다. 수동 추가매수·일부매도는 별도 승인합니다.","warning"),
      node("p","자동 매도 완료 또는 일부매도 버튼으로 전량 청산하면 자동 정지합니다. 반복매매는 하지 않습니다. 자동 매도에는 공통 수량·비중 상한을 적용하지 않습니다.","warning"),
      node("p",`실제 주문이 전송될 수 있습니다. ${scheduled?"예약 시간 이후 감시합니다.":"시작 직후 감시합니다."} 신규주문 차단은 자동·추가 주문 모두 차단합니다. 전체 자동매매 일시정지는 자동 주문만 대기시키며, 개별 승인하는 추가매수·일부매도는 별개입니다. 브라우저를 닫아도 서버 워커는 계속 동작합니다.` ,"warning"));
    if(scheduled)dialog.append(label);
    dialog.append(node("p",entered.take_profit_enabled!==false?`익절 사용 · 수익률 ${entered.take_profit}% 이상이면 전량 시장가 매도`:"익절 사용 안 함 · 목표수익률에 도달해도 자동 익절하지 않습니다.","warning"));
    if(entered.take_profit_enabled===false&&!entered.stop_loss_enabled)dialog.append(node("p","익절·손절 모두 꺼져 있습니다. 자동 매도 조건이 없으며 직접 매도해야 합니다.","warning"));
    dialog.append(node("p",entered.stop_loss_enabled?`손절 사용 · 손실률 ${entered.stop_loss_rate}% 또는 가격 ${entered.stop_loss_price?priceText(entered.stop_loss_price):"미지정"} 이하 · 기존 보유분 포함 전량 시장가 매도`:"손절 사용 안 함",entered.stop_loss_enabled?"warning":"helper"));
    dialog.append(confirm,close,note);document.body.append(dialog);
    close.addEventListener("click",()=>{dialog.close();dialog.remove();});
    confirm.addEventListener("click",async()=>{if(scheduled&&!time.checkValidity()){time.reportValidity();return;}confirm.disabled=true;try{
      await api(`/trading/plans/${plan.id}/control`,{method:"POST",body:JSON.stringify({version:plan.version,action:scheduled?"schedule":"start",...(scheduled?{time:time.value}:{}),settings,confirmation:"실전 자동매매 시작 확인"})});
      message.textContent="자동매매 실행 승인 저장 · 주문 직전 신규주문 차단 및 최신 설정을 다시 확인합니다.";dialog.close();dialog.remove();await reload();
    }catch(e){note.textContent=e.message;}finally{confirm.disabled=false;}});dialog.showModal();
  }
  start.addEventListener("click",()=>approve(false));reserve.addEventListener("click",()=>approve(true));
  autoButtons.append(start,stop,reserve);action.append(autoButtons,autoNote);
  if(!metadata.automatic_enabled)action.append(node("small","서버 실전·자동매매 잠금 또는 한정 테스트 승인 범위가 있어 실행 불가","warning"));
}

function monitorTradingWorker(section,metadata) {
  const status=node("p","자동매매 워커 상태 확인 중…","helper"),owner=state.user.id;status.setAttribute("role","status");section.prepend(status);let busy=false;
  async function poll(){if(busy||!section.isConnected||document.hidden||owner!==state.user?.id)return;busy=true;try{
    const [data,safety]=await Promise.all([api("/trading/worker"),api("/trading/safety")]);if(!section.isConnected||owner!==state.user?.id)return;
    const w=data.worker;status.textContent=`실전 워커 ${w.running?(w.leader?"정상 · 실행 리더":"대기 · 다른 서버가 리더일 수 있음"):"중지"} · 마지막 확인 ${w.last_tick?formatTime(w.last_tick):"—"} · ${w.poll_seconds||5}초 주기${w.error?" · "+w.error:""}`;
    for(const plan of data.plans){const target=section.querySelector(`[data-auto-plan="${plan.id}"]`);if(target)target.textContent=`${plan.active?"활성":"정지"} · ${plan.status?.message||"대기"}${plan.status?.checked_at?" · "+formatTime(plan.status.checked_at):""}`;const badge=section.querySelector(`[data-auto-activity="${plan.id}"]`);if(badge)paintAutomaticActivity(badge,plan,w,safety);}
  }catch(e){if(section.isConnected&&owner===state.user?.id){status.textContent="워커 상태 확인 지연 · 서버 연결을 확인하세요.";for(const badge of section.querySelectorAll("[data-auto-activity]"))paintAutomaticActivity(badge,{},null,{},true);}}finally{busy=false;}}
  poll();const timer=setInterval(()=>{if(!section.isConnected){clearInterval(timer);return;}poll();},5000);
}
