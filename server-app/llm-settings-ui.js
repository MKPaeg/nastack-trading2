/* Task-specific prompt controls and explicit, user-owned business research. */
async function renderLLMTasks(area) {
  const owner=state.user.id,valid=()=>state.user?.id===owner&&area.isConnected;
  area.append(node("p","사업 기반 수집 계획과 통계 해석을 포함한 LLM 작업을 각각 설정합니다. 원문·수치 수집은 서버의 승인된 연결만 사용하며, 벡터·주문·손익 수치 계산과 리포트 조회는 LLM 작업이 아닙니다.","helper"));
  const catalog=await api("/user/llm-tasks");if(!valid())return;
  area.append(node("p",`내 기본 모델: ${catalog.default_provider} / ${catalog.default_model||"미설정"} · 뉴스 전용 선택: ${catalog.news_model}`,"helper"),node("p",catalog.fixed_rules,"helper"));
  for(const task of catalog.items){
    const card=node("details"),summary=node("summary",task.name),editor=node("textarea"),status=node("p","","helper"),save=node("button","프롬프트 저장","primary"),reset=node("button","기본값 불러오기","secondary"),reload=node("button","다시 조회","secondary"),history=node("button","변경 이력","secondary"),historyArea=node("div");
    let policy=task.policy;
    editor.value=policy.text;editor.rows=10;editor.maxLength=12000;editor.style.width="100%";editor.setAttribute("aria-label",`${task.name} 프롬프트`);status.setAttribute("role","status");
    function draw(){editor.value=policy.text;status.textContent=`버전 ${policy.version} · ${policy.updated_at?formatTime(policy.updated_at):"시스템 기본값"} · ${policy.sha256.slice(0,12)}`;}
    card.append(summary,node("p",task.purpose),node("p",`실행 시점: ${task.trigger}`,"helper"),node("p",`사용 모델: ${task.model_source}`,"helper"));
    if(task.id==="research")card.append(node("p",catalog.research_enabled?"서버 연구 실행 허용":"현재 서버 연구 실행 비활성 · 프롬프트 저장은 가능", "helper"));
    card.append(editor,save,reset,reload,history,status,historyArea);area.append(card);draw();
    save.disabled=reset.disabled=state.user.role==="viewer";editor.readOnly=state.user.role==="viewer";
    reset.addEventListener("click",()=>{editor.value=policy.default_text;status.textContent="기본값을 불러왔습니다. 저장하면 다음 실행부터 적용됩니다.";});
    reload.addEventListener("click",async()=>{try{const next=await api("/user/llm-tasks");if(!valid())return;policy=next.items.find(item=>item.id===task.id).policy;draw();}catch(error){if(valid())status.textContent=error.message;}});
    save.addEventListener("click",async()=>{save.disabled=true;try{const next=await api(`/user/llm-tasks/${task.id}`,{method:"PUT",body:JSON.stringify({version:policy.version,text:editor.value})});if(!valid())return;policy=next;draw();status.textContent+=" · 저장 완료 · 과거 결과는 유지됩니다.";const settings=await api("/settings");if(valid())state.settings=settings;}catch(error){if(valid())status.textContent=error.message;}finally{if(valid())save.disabled=false;}});
    history.addEventListener("click",async()=>{try{const data=await api(`/user/llm-tasks/${task.id}/history`);if(!valid())return;historyArea.replaceChildren();for(const item of data.items){const entry=node("details");entry.append(node("summary",`${formatTime(item.created_at)} · 버전 ${item.after.version}`),node("pre",item.after.text,"result-json"));historyArea.append(entry);}if(!data.items.length)historyArea.append(node("p","저장된 변경 이력이 없습니다.","helper"));}catch(error){if(valid())status.textContent=error.message;}});
  }
  area.append(node("p","프롬프트 저장은 모델 호출이나 기존 결과 재평가를 실행하지 않습니다. 작업별 입력·출력 형식과 실행 권한은 서버가 유지합니다.","helper"));
  await renderBusinessResearch(area,catalog);
}

async function renderBusinessResearch(area,catalog) {
  const owner=state.user.id,valid=()=>state.user?.id===owner&&area.isConnected;
  const section=panel("종목 사업 설명 · 인터넷 조사"),choice=node("select"),run=node("button","인터넷 조사·속성 저장","primary"),reload=node("button","저장 설명·이력 조회","secondary"),message=node("p","","helper"),output=node("div");
  choice.setAttribute("aria-label","사업 설명 조사 종목");message.setAttribute("role","status");
  section.append(node("p","기존의 출처 있는 사업 설명과 새로 인터넷 검색한 공식 공시·회사 자료를 내 기본 LLM이 함께 요약합니다. 인용·불확실성을 포함한 결과는 본인의 종목 속성·다음 뉴스 평가에서 사용합니다. 수동 벡터 속성과 과거 결과는 유지됩니다.","helper"),node("p",`인터넷 검색: ${catalog.search_configured?"공용 Tavily 연결 등록됨":"검색 연결 미설정"} · 모델 ${catalog.default_model||"미설정"}. 키 등록 여부는 실제 접속 성공을 뜻하지 않습니다.`,"helper"),choice,run,reload,message,output);area.append(section);
  const instruments=await api("/vectors/instruments");if(!valid())return;
  for(const item of instruments.items){const option=node("option",`${item.name} · ${item.code}`);option.value=item.code;choice.append(option);}
  run.disabled=state.user.role==="viewer"||!instruments.items.length||!catalog.search_configured||!catalog.default_model;
  if(!instruments.items.length)message.textContent="관심·보유·매매목록에 등록된 종목이 없습니다.";
  let retryRecord=null;
  async function load(){
    if(!choice.value)return;
    const code=choice.value,data=await api(`/business-research?code=${code}`);if(!valid()||choice.value!==code)return;
    const latest=data.items?.[0];retryRecord=latest&&(latest.retryable||(latest.status==="failed"&&["BUSINESS_INVALID_EVIDENCE","BUSINESS_CONTENT_REQUIRED","PROVIDER_HTTP_ERROR"].includes(latest.error))||latest.status==="insufficient"||(latest.status==="completed"&&latest.context?.source_kind==="llm_web_evidence"&&!latest.context.topics?.length))?latest:null;
    run.textContent=retryRecord?"실패한 사업설명 재처리":"인터넷 조사·속성 저장";
    output.replaceChildren();
    if(data.context){const context=data.context;output.append(node("h3","저장된 사업 설명"),node("p",context.description),node("p",context.note,"helper"),node("p",`주제: ${(context.topics||[]).join(" · ")||"미확인"}`,"helper"));for(const uncertainty of context.uncertainties||[])output.append(node("p",uncertainty,"helper"));
      for(const evidence of context.evidence||[]){const detail=node("details");detail.append(node("summary",`${evidence.id} · ${evidence.source_kind==="existing_context"?"기존 자료":evidence.source_kind==="official_issuer_listing"?"KIND 공식 기업정보":"웹검색 자료"} · ${evidence.title}`),node("p",evidence.retrieved_at?`수집: ${formatTime(evidence.retrieved_at)}`:"수집 시점 미확인","helper"),node("p",evidence.snippet),reportLink(evidence.url,"근거 원문"));output.append(detail);}}
    else output.append(node("p","저장된 인터넷 사업 조사가 없습니다. 조사 버튼을 누르면 새로 검색하고 요약합니다.","helper"));
    for(const item of data.items){if(item.execution_usage)output.append(node("p",llmUsageText(item.execution_usage),"helper"));output.append(node("p",`${formatTime(item.created_at)} · ${{completed:"조사 완료",insufficient:"사업 본문 자료 부족",failed:"실패",running:"실행 중 · 불명 시 자동 재호출 금지",abandoned:"중단 기록 정리"}[item.status]||item.status}${item.error?" · "+item.error:""}${item.failure_message?" · "+item.failure_message:""}${item.search_reused?" · 검색 근거 재사용":""}${item.usage?.total_tokens!=null?" · 사용 토큰 "+item.usage.total_tokens.toLocaleString("ko-KR"):""}`,"helper"));
      for(const issue of item.validation_issues||[])output.append(node("p",`검증 항목: ${issue.field} · ${issue.type}${issue.max_length!=null?` · 최대 ${issue.max_length}`:""}${issue.actual_length!=null?` · 실제 ${issue.actual_length}`:""}`,"helper"));
      if(item.broker_metadata?.status==="confirmed")output.append(node("p",`나무 종목 확인 · ${item.broker_metadata.industry} · ${item.broker_metadata.industry_code} · 기본 업종이며 상세 사업설명과 구분`,"helper"));
      if(item.status==="running"&&Date.now()-Date.parse(item.created_at)>300000&&state.user.role!=="viewer"){
        const abandon=node("button","미완료 조사 종료","secondary");output.append(abandon);abandon.addEventListener("click",async()=>{if(!confirm("응답이 확인되지 않은 조사 기록을 종료 상태로 정리합니다. 기존 검색·모델 비용을 취소하거나 조사를 자동 재실행하지 않습니다. 종료할까요?"))return;try{await api(`/business-research/${item.id}/abandon`,{method:"POST",body:JSON.stringify({confirmation:"미완료 조사 종료 확인"})});if(valid())await load();}catch(error){if(valid())message.textContent=error.message;}});
      }
    }
  }
  reload.addEventListener("click",()=>load().catch(error=>{if(valid())message.textContent=error.message;}));choice.addEventListener("change",()=>load().catch(reportError));
  run.addEventListener("click",async()=>{
    const code=choice.value,item=instruments.items.find(v=>v.code===code),cloud=catalog.default_provider==="openai";
    const retry=retryRecord?.code===code?retryRecord:null;
    if(!confirm(`${item.name}(${code})의 공개 종목명·코드를 Tavily 인터넷 검색에 보내고, 기존 사업 설명과 검색 발췌문을 ${catalog.default_model} 모델로 보내 함께 요약합니다. ${retry?"이전 검색 근거가 보존되어 있고 현재 출처·연결 정책과 같으면 재사용합니다. 근거가 없던 과거 실패는 새 검색이 필요합니다. ":""}검색·모델 API 비용이 발생할 수 있습니다. 실행할까요?`))return;
    run.disabled=true;choice.disabled=true;const key=requestIdentifier();
    try{message.textContent="인터넷 검색·사업 설명 작성 중… 창을 다시 눌러 재요청하지 마세요.";const result=await llmButtonTask(run,"사업 조사 중…",()=>valid()&&choice.value===code,call=>call("/business-research",{method:"POST",body:JSON.stringify({code,request_key:key,model:"default",confirmed:true,cloud_confirmed:cloud,...(retry?{retry_of:retry.id}:{})})}));if(!valid())return;message.textContent=result.status==="completed"?"사업 설명을 개인 종목 속성에 저장했습니다. 다음 뉴스 평가부터 참고합니다. 기본 차원 제안은 별도 초기화 버튼으로 갱신할 수 있습니다.":`조사 ${result.status} · ${result.failure_message||result.error||"이력을 확인하세요."}`;await load();}
    catch(error){if(valid())message.textContent=`${error.message} · 저장 이력을 먼저 조회하세요. 자동으로 재호출하지 않습니다.`;}
    finally{if(valid()){run.disabled=false;choice.disabled=false;}}
  });await load();
}
