function watchTradeSelection(plan,field,input,read=()=>input.value) {
  if(state.user.role==="viewer")return;
  const owner=state.user.id;
  input.addEventListener("change",()=>{
    if(owner!==state.user?.id||!input.isConnected||!input.checkValidity())return;
    let value;try{value=read();}catch(error){showToast(error.message);return;}
    plan._settingsPending=(plan._settingsPending||Promise.resolve()).catch(()=>{}).then(async()=>{
      if(owner!==state.user?.id||!input.isConnected)throw new Error("화면이 변경되어 저장하지 않았습니다.");
      const saved=await api(`/trading/plans/${plan.id}/settings`,{method:"PATCH",body:JSON.stringify({field,value,version:plan.version})});
      if(owner===state.user?.id){Object.assign(plan,saved);showToast(field==="buy_condition"?"매수 조건 저장 · 자동매매 정지 · 시작·예약을 다시 승인하세요.":"설정 자동 저장 완료 · 실행 중이면 다음 판단부터 적용됩니다.");}
    });
    plan._settingsPending.catch(error=>{if(input.isConnected&&owner===state.user?.id)showToast(`설정 저장 실패 · ${error.message} · 다시 입력하거나 목록을 새로 조회하세요.`);});
  });
}

async function renderTradeJournal(parent) {
  const box=panel("내 매매 선택·주문 감사 이력"),output=node("div"),refresh=node("button","매매 로그 새로고침","secondary");
  box.append(node("p","선택 변경(입력 완료 시), 조건 저장·시작·정지·예약, 주문 미리보기·전송 요청·차단·접수·체결·취소를 DB에 보존합니다. 일반 운영 로그 정리로 삭제되지 않습니다.","helper"),refresh,output);parent.prepend(box);
  const names={selection_changed:"선택 변경 (저장 전)",plan_saved:"조건 저장",auto_start:"자동매매 시작 승인",auto_stop:"자동매매 정지",auto_schedule:"자동매매 예약 승인",order_preview:"주문 미리보기 · 전송 전",order_preview_dismissed:"미리보기 취소 · 전송 없음",order_execution_requested:"주문 실행 요청",order_submitting:"주문 전송 준비",order_accepted:"주문 접수 · 체결 아님",order_unknown:"주문 결과 불명 · 재전송 차단",order_blocked:"주문 차단",preview_blocked:"미리보기 차단",fill_checked:"체결·취소 상태 확인",cancel_requested:"미체결 취소 요청",cancel_pending:"취소 접수 · 완료 아님",cancel_unknown:"취소 결과 불명",safety_saved:"신규 주문 차단 변경",worker_state:"자동매매 워커 상태 변경",notification_completed:"완료 알림 처리"};
  Object.assign(names,{automatic_paused:"내 자동매매 전체 일시정지",automatic_resumed:"내 자동매매 재개",auto_control_requested:"자동매매 제어 요청",auto_control_blocked:"자동매매 제어 차단",cancel_blocked:"취소 요청 차단"});
  function describe(value,depth=0){if(value==null)return "—";if(typeof value==="number")return value.toLocaleString("ko-KR");if(typeof value==="object")return Object.entries(value).map(([k,v])=>`${"  ".repeat(depth)}${k}: ${describe(v,depth+1)}`).join("\n");return String(value);}
  async function load(page=1){refresh.disabled=true;try{
    const data=await api(`/trading/events?page=${page}`);if(!box.isConnected)return;output.replaceChildren();
    for(const event of data.items){const row=node("details"),d=event.detail;row.append(node("summary",`${formatTime(event.created_at)} · ${names[event.action.replace("trading.","")]||event.action} · ${d.name||d.code||"설정"}`),node("p",`연결 ID ${event.target_id} · 기록 ${event.id}`,"helper"),node("pre",describe(d),"result-json"));output.append(row);}
    if(!data.total)output.append(node("p","매매 감사 이력이 없습니다.","helper"));listPager(output,data,load);
  }catch(error){if(box.isConnected)output.textContent=`매매 감사 이력 조회 실패: ${error.message}`;}finally{refresh.disabled=false;}}
  refresh.addEventListener("click",()=>load());await load();
}
