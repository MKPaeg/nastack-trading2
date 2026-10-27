/* Owner-scoped control plane: viewing never starts collection or an LLM. */
function collectionTokenText(results){
  let total=0,unknown=0;
  for(const result of results){
    if(result?.reused)continue;
    const usage=result?.execution_usage||result?.collection_usage;
    if(usage?.known===true&&Number.isInteger(usage.total_tokens)&&usage.total_tokens>=0)total+=usage.total_tokens;
    else unknown++;
  }
  return `LLM 사용 토큰 ${total.toLocaleString("ko-KR")}개${unknown?` · 확인 불가 ${unknown}건 (합계 제외)`:""} · 검색 API 비용 별도${typeof llmUsageText==="function"?" · "+llmUsageText(combineLLMUsage(results.filter(result=>!result?.reused).map(result=>result?.execution_usage).filter(Boolean))):""}`;
}
function collectionResourceStatus(item){
  if(!item.enabled)return "자동 갱신 꺼짐";
  if(item.status==="failed"){
    if(item.error==="SOURCE_NOT_PUBLISHED")return `원천 발표 대기 · 연결 오류 아님 · 이전 자료 보존 · 다음 점검 ${formatTime(item.due_at)}`;
    const reason=item.error==="REQUEST_REJECTED_502"?"원천 조회·응답 검증 실패 (502)":item.error==="REQUEST_REJECTED_429"?"호출 한도·간격 제한":item.error==="REQUEST_REJECTED_403"?"수집 권한 확인 필요":"수집 실패 · 연결·출처 설정 확인 필요";
    return `${reason} · 이전 자료 보존 · 재점검 예정 ${formatTime(item.due_at)}`;
  }
  if(item.status==="running")return "독립 수집기가 갱신 중 · 이 화면 진입으로 시작한 작업이 아닙니다.";
  if(item.due)return "점검 주기 경과 · 한도 내에서 순차 갱신 대기";
  return `${item.status==="completed"?"최근 점검 완료":"대기"} · 다음 점검 ${formatTime(item.due_at)}`;
}

async function renderDataCollection(){
  const owner=state.user.id,area=$("#liveContent");area.replaceChildren();
  const valid=()=>state.user?.id===owner&&state.view==="data";
  const tabs=node("div",undefined,"admin-tabs"),body=node("div");area.append(tabs,body);
  const choices=[["status","갱신 상태·비용 한도"],["dimensions","다차원 데이터"],["news","뉴스·공시 수집 계획"],["statistics","통계 데이터"]];
  async function show(key){
    state.dataCollectionTab=key;
    persistScreenState();
    $("#liveInspector").replaceChildren();
    body.replaceChildren();for(const b of tabs.children)b.setAttribute("aria-selected",String(b.dataset.tab===key));
    if(key==="dimensions")return renderDimensionData(body);
    if(key==="statistics")return renderStatistics(body);
    if(key==="news"){
      const p=panel("뉴스·공시 수집 · 영향 평가는 종목 분석에서 별도 실행");body.append(p);
      p.append(node("p","백그라운드는 저장된 계획만 실행하며 LLM을 호출하지 않습니다. 새 종목·사업설명·출처 정책이 바뀌면 여기서 계획을 명시적으로 갱신하세요. 수집한 원문은 계정 내에서 재사용하고 영향 평가는 종목별로 분리합니다.","helper"));
      const actions=node("div",undefined,"source-row"),all=node("button","모든 종목 자료 조회","primary"),total=node("span","아직 실행하지 않았습니다.","helper");total.setAttribute("role","status");total.setAttribute("aria-live","polite");actions.append(all,total);p.append(actions,node("p","본인 목록의 모든 종목에서 부족 자료를 순차 수집합니다. 기존 계획·원문은 재사용하고 영향 평가는 실행하지 않습니다. 계획 생성 시 LLM 토큰, 검색 시 API 비용이 발생할 수 있으며 일일 한도를 유지합니다.","helper"));all.disabled=true;
      const loading=node("p","저장 수집 계획 조회 중…","helper task-busy");loading.setAttribute("role","status");p.append(loading);
      const owned=await api("/vectors/instruments");
      if(!valid()||!p.isConnected)return;
      const entries=[],alive=()=>valid()&&p.isConnected;let running=false;
      const enable=()=>{all.disabled=running||state.user.role==="viewer"||!entries.length;for(const entry of entries)entry.button.disabled=running||state.user.role==="viewer";};
      async function collectEntry(entry){
        const {item,row,button,note,detail}=entry;
        try{
          const result=await llmButtonTask(button,"수집 계획 확인·자료 수집 중…",alive,()=>refreshBusinessCollection(item.code,text=>{if(alive())button.textContent=text;},true,alive,"collect"));
          if(!alive())return null;
          note.textContent=`${result.status} · ${collectionTokenText([result])}`;
          detail.replaceChildren(collectionWorkflowView(result,item.code,false));
          return result;
        }catch(error){if(alive())note.textContent=`수집 실패 · ${error.message} · 토큰 확인 불가`;return null;}
      }
      await Promise.all((owned.items||owned).map(async item=>{
        const row=node("section",undefined,"panel"),msg=node("p","저장 계획 확인 중…","helper"),button=node("button","계획 확인·부족 자료 수집 (평가 안 함)","secondary"),note=node("span","아직 실행하지 않았습니다.","helper"),detail=node("div"),controls=node("div",undefined,"source-row");note.setAttribute("role","status");note.setAttribute("aria-live","polite");controls.append(button,note);detail.append(msg);
        row.append(node("h3",`${item.name} · ${item.code}`),detail,controls);p.append(row);button.disabled=true;
        const entry={item,row,button,note,detail};entries.push(entry);
        button.addEventListener("click",async()=>{
          if(running||state.user.role==="viewer"||!confirm("기존 계획·원문은 재사용합니다. 계획이 변경된 경우 LLM 호출, 부족 자료 검색 시 API 비용이 발생할 수 있습니다. 수집만 실행할까요?"))return;
          running=true;enable();try{await collectEntry(entry);}finally{running=false;if(alive())enable();}
        });
        let saved;try{saved=await api(`/news/workflow/${item.code}`);}catch(error){if(valid()&&row.isConnected)msg.textContent=`저장 계획 조회 실패: ${error.message}`;return;}if(!valid()||!row.isConnected)return;
        msg.replaceWith(collectionWorkflowView(saved,item.code,false));
      }));
      if(alive()){loading.remove();enable();}
      all.addEventListener("click",async()=>{
        if(running||state.user.role==="viewer"||!confirm(`본인 목록 ${entries.length}개 종목의 부족 자료를 순차 수집합니다. 계획 변경 시 LLM·검색 API 비용이 발생할 수 있습니다. 전체 수집할까요?`))return;
        running=true;enable();const results=[];let done=0,failed=0;
        try{await llmButtonTask(all,"모든 종목 자료 수집 중…",alive,async(call,add)=>{
          for(const entry of entries){
            if(!alive())break;
            total.textContent=`진행 ${done}/${entries.length} · ${entry.item.name} · ${collectionTokenText(results)}`;
            const result=await collectEntry(entry);if(!alive())break;done++;results.push(result);if(result?.execution_usage&&!result.reused)add(result.execution_usage);
            if(!result||!["collected","completed"].includes(result.status))failed++;
            total.textContent=`처리 ${done}/${entries.length} · 실패·미완료 ${failed}건 · ${collectionTokenText(results)}`;
            if(result&&["queued","running","deferred"].includes(result.status))break;
          }
        });}finally{running=false;if(alive())enable();}
      });
      return;
    }
    const p=panel("화면과 독립적인 원천 갱신");body.append(p);
    p.append(node("p","기본: 기존 수집 차원 일별 점검, 월간 통계 주 1회·연간 통계 월 1회 점검. 뉴스 자동 검색은 기본 꺼짐입니다. 수집·설정 저장은 주문과 LLM 평가를 실행하지 않습니다.","helper"));
    const loading=node("p","저장된 갱신 상태 조회 중 · 외부 수집·LLM 분석을 실행하는 것이 아닙니다.","helper task-busy");loading.setAttribute("role","status");p.append(loading);
    let data;
    try{data=await api("/data-collection");}catch(error){if(valid()&&p.isConnected){loading.className="warning";loading.textContent=`저장 상태 조회 실패: ${error.message}`;const retry=node("button","저장 상태 다시 조회","secondary");retry.addEventListener("click",()=>buttonTask(retry,"저장 상태 조회 중…",()=>show("status")).catch(reportError));p.append(retry);}return;}
    if(!valid()||!p.isConnected)return;loading.remove();
    const info=node("details");info.append(node("summary","호출 한도·공개시각 안내"),node("p",data.note,"helper"));p.append(info);
    const fields={},form=node("div",undefined,"vector-controls");p.append(form);
    const labels={enabled:"자동 수집 사용",dimensions_enabled:"기존 다차원 데이터 갱신",statistics_enabled:"기존 통계 갱신",news_enabled:"뉴스·공시 자동 수집 허용",news_hours:"뉴스 점검 간격 (1~168시간)",dimension_daily_limit:"일일 차원 수집 시도 한도",statistics_daily_limit:"일일 통계 수집 시도 한도",search_daily_limit:"일일 검색 요청 한도"};
    p.append(node("p","옵션을 변경한 뒤 ‘내 수집 설정 저장’을 눌러야 반영됩니다. 저장 전에는 기존 설정으로 동작합니다. 뉴스·공시 자동 수집을 허용하면 검색 API 비용이 발생할 수 있습니다.","warning"));
    p.append(node("p","뉴스 점검 간격은 저장 계획의 뉴스·공시 자동 수집에만 적용됩니다. 자동 수집과 뉴스 검색 허용이 모두 켜져 있어야 하며, 일일 한도·실패 재시도 대기에 따라 늦어질 수 있습니다. 다차원·통계는 원천별 주기로 갱신합니다.","helper"));
    for(const [key,label]of Object.entries(labels)){
      const field=node("label",label),input=node("input");input.setAttribute("aria-label",label);
      if(typeof data.settings[key]==="boolean"){input.type="checkbox";input.checked=data.settings[key];field.className="collection-toggle";field.replaceChildren(input,node("span",label));}else{input.type="number";input.min=key==="news_hours"?1:0;input.max=key==="news_hours"?168:key==="dimension_daily_limit"?200:key==="statistics_daily_limit"?30:100;input.step="1";input.value=data.settings[key];field.append(input);}
      input.disabled=state.user.role==="viewer";fields[key]=input;form.append(field);
    }
    const save=node("button","내 수집 설정 저장","primary"),reload=node("button","저장 상태 다시 조회","secondary"),message=node("p","","helper");p.append(save,reload,message);save.disabled=state.user.role==="viewer";
    save.addEventListener("click",async()=>{if(!fields.news_hours.checkValidity()){message.textContent="뉴스 점검 간격은 1~168시간 사이의 정수로 입력하세요.";fields.news_hours.reportValidity();return;}if(!Object.values(fields).every(f=>f.checkValidity())){message.textContent="한도·주기 범위를 확인하세요.";return;}try{await buttonTask(save,"수집 설정 저장 중…",async()=>{const settings=Object.fromEntries(Object.entries(fields).map(([k,f])=>[k,f.type==="checkbox"?f.checked:Number(f.value)]));reload.disabled=true;for(const f of Object.values(fields))f.disabled=true;await api("/data-collection",{method:"PUT",body:JSON.stringify({version:data.version,...settings})});if(valid()&&p.isConnected)await show("status");});}catch(error){if(valid()&&p.isConnected)message.textContent=error.message;}finally{if(valid()){save.disabled=state.user.role==="viewer";reload.disabled=false;for(const f of Object.values(fields))f.disabled=state.user.role==="viewer";}}});
    reload.addEventListener("click",()=>buttonTask(reload,"저장 상태 조회 중…",()=>show("status")).catch(reportError));
    const usage=data.usage;p.append(node("p",`최근 한도 집계일 ${usage.date||"없음"} · 차원 ${usage.dimension||0} · 통계 ${usage.statistics||0} · 검색 ${usage.search||0}회 (한국시간 자정 초기화, 오늘 집계와 날짜를 비교하세요.)`,"helper"));
    const table=node("table"),head=node("tr");for(const label of ["자료","점검 주기","마지막 수집","다음 점검·상태"])head.append(node("th",label));table.append(head);
    for(const item of data.resources){const tr=node("tr");for(const text of [item.name,item.cadence,formatTime(item.last_at),collectionResourceStatus(item)])tr.append(node("td",text));table.append(tr);}
    const wrap=node("div",undefined,"table-wrap");wrap.append(table);p.append(wrap);
    if(!data.resources.length)p.append(node("p","저장 자료가 없습니다. 다차원·통계 탭에서 필요한 항목만 최초 수집하고, 뉴스 탭에서 종목별 계획을 준비하세요.","warning"));
  }
  for(const [key,label]of choices){const b=node("button",label,"admin-tab");b.dataset.tab=key;b.setAttribute("role","tab");b.addEventListener("click",()=>buttonTask(b,`${label} 조회 중…`,()=>show(key)).catch(reportError));tabs.append(b);}
  await show(["status","dimensions","news","statistics"].includes(state.dataCollectionTab)?state.dataCollectionTab:"status");
}
