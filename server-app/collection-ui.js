function llmOwnerGuard(){
  const owner=state.user?.id,tenant=state.user?.tenant_id,epoch=state.sessionEpoch||0;
  return ()=>state.user?.id===owner&&state.user?.tenant_id===tenant&&(state.sessionEpoch||0)===epoch;
}

function llmUsageText(usage){
  if(!usage)return "사용량 미보고 · 토큰·비용 확인 불가 (0원으로 간주하지 않음)";
  const amount=value=>Number(value||0).toLocaleString("ko-KR");
  const cost=!usage.cost_known&&!usage.priced_calls?"비용 미확인":`${usage.cost_known?"예상 비용":"확인된 비용 소계"} US$${Number(usage.estimated_usd||0).toFixed(6)}`;
  const models=(usage.model_usage||[]).map(item=>`${item.model} ${amount(item.llm_calls)}회 ${item.cost_known?`US$${Number(item.estimated_usd||0).toFixed(6)}`:"비용 미확인"}`).join(" / ");
  return `LLM ${amount(usage.llm_calls)}회 · ${usage.known?"사용":"확인된"} 토큰 ${amount(usage.total_tokens)}개 (입력 ${amount(usage.input_tokens)} / 출력 ${amount(usage.output_tokens)} / 캐시 입력 ${amount(usage.cached_input_tokens)} / 캐시 쓰기 ${usage.cache_write_known===true?amount(usage.cache_write_tokens):"미보고"}) · ${cost}${usage.cost_known?"":" · 미보고 사용량·단가 미확인 비용 별도"}${models?` · 모델별 ${models}`:""}${usage.pricing_as_of?` · 단가 확인 ${usage.pricing_as_of}`:""} ${usage.allocation?" · 초기화 묶음 전체 합계 (종목별 비용 아님)":""} · USD 기준 추정, 세금·환율·검색·도구·서버 비용 제외`;
}

function combineLLMUsage(items){
  if(!items.length)return null;
  const result={known:true,cost_known:true};
  for(const key of ["llm_calls","total_tokens","input_tokens","output_tokens","cached_input_tokens","cache_write_tokens","estimated_usd","priced_calls"])result[key]=items.reduce((sum,item)=>sum+(Number(item[key])||0),0);
  const models=new Map();for(const item of items){for(const usage of item.model_usage||[]){const key=`${usage.provider}:${usage.model}`,prior=models.get(key)||{provider:usage.provider,model:usage.model,llm_calls:0,estimated_usd:0,cost_known:true};prior.llm_calls+=Number(usage.llm_calls)||0;prior.estimated_usd+=Number(usage.estimated_usd)||0;prior.cost_known=prior.cost_known&&Boolean(usage.cost_known);models.set(key,prior);}}result.model_usage=[...models.values()];
  const dates=[...new Set(items.map(item=>item.pricing_as_of).filter(Boolean))];result.pricing_as_of=dates.length===1?dates[0]:null;
  result.known=items.every(item=>item.known);result.cache_write_known=items.every(item=>item.cache_write_known===true);result.cost_known=items.every(item=>item.cost_known);return result;
}

// Opt-in for LLM buttons only; never intercept order/approval actions globally.
async function llmButtonTask(button,label,valid,task,progress=null){
  if(button.dataset.working==="true"||state.user.role==="viewer"||!valid())return;
  const ownerCurrent=llmOwnerGuard(),alive=()=>ownerCurrent()&&valid()&&button.isConnected;
  let output=progress||button.nextElementSibling;if(!progress&&!output?.classList.contains("llm-action-status")){output=node("div",undefined,"evaluation-progress llm-action-status");button.after(output);}
  output.hidden=false;
  const stage=node("span",label,"task-busy"),elapsed=node("span"),cost=node("span","사용량 집계 대기 · 응답 수신 후 표시","helper");output.replaceChildren(stage,elapsed,cost);output.setAttribute("role","status");output.setAttribute("aria-busy","true");
  const start=performance.now(),items=[],seen=new Set();let received=false;
  function add(item){if(!alive()||!item)return;if(item.execution_id){if(seen.has(item.execution_id))return;seen.add(item.execution_id);}items.push(item);received=true;cost.textContent=llmUsageText(combineLLMUsage(items));}
  const call=async(path,options={})=>{try{return await api(path,{...options,onUsage:add});}catch(error){throw error;}};
  function clock(done=false){const seconds=Math.floor((performance.now()-start)/1000);elapsed.textContent=`${done?"소요":"경과"} ${Math.floor(seconds/60)}분 ${String(seconds%60).padStart(2,"0")}초`;}
  clock();const timer=setInterval(()=>{if(alive())clock();else clearInterval(timer);},1000);
  try{return await buttonTask(button,label,async()=>{const result=await task(call,add);if(alive()){if(result?.execution_usage&&!received)add(result.execution_usage);stage.textContent=result?.status==="failed"?"작업 실패 · 결과를 확인하세요.":result?.status==="pending"||result?.pending?"백그라운드 작업 대기 · 완료 후 저장 결과에서 사용량을 확인하세요.":"작업 처리 종료 · 결과를 확인하세요.";}return result;});}
  catch(error){if(alive())stage.textContent=`요청 실패 · ${error.message}`;throw error;}
  finally{clearInterval(timer);if(alive()){clock(true);stage.classList.remove("task-busy");output.setAttribute("aria-busy","false");if(!received)cost.textContent=llmUsageText(null);}}
}

async function runDefaultInitialization(button,valid,path){
  return llmButtonTask(button,"초기 속성 생성 중…",valid,async(call,add)=>{
    const ownerCurrent=llmOwnerGuard(),alive=()=>ownerCurrent()&&valid();
    const single=path.match(/\/defaults\/(\d{6})$/)?.[1];
    const codes=single?[single]:(await call("/vectors/instruments")).items.map(item=>item.code);
    const before=new Set();
    for(const code of codes){if(!alive())throw Error("화면·계정 변경");const old=await call(`/vectors/defaults/${code}`);if(old.profile?.execution_usage?.execution_id)before.add(old.profile.execution_usage.execution_id);}
    const result=await call(path,{method:"POST"});let pending=false;
    for(let attempt=0;attempt<150;attempt++){
      pending=false;
      for(const code of codes){if(!alive())throw Error("화면·계정 변경");const current=await call(`/vectors/defaults/${code}`),profile=current.profile;pending ||= profile?.status==="pending";if(profile?.execution_usage&&!before.has(profile.execution_usage.execution_id))add(profile.execution_usage);}
      if(!pending)break;await new Promise(resolve=>setTimeout(resolve,2000));
    }
    return {...result,status:pending?"pending":"completed",pending:pending?1:0};
  });
}

// Local feedback only: no requests, retries, or persisted cross-account timers.
async function newsEvaluationTask(button,progress,valid,task){
  if(button.dataset.working==="true"||!valid()||state.user.role==="viewer")return;
  const ownerCurrent=llmOwnerGuard(),alive=()=>ownerCurrent()&&valid()&&progress.isConnected;
  const started=performance.now(),stage=node("span","평가 요청 준비 중…","task-busy"),elapsed=node("span",undefined,"evaluation-elapsed"),cost=node("span","사용량 집계 대기 · 응답 수신 후 표시","helper");
  elapsed.setAttribute("aria-live","off");progress.hidden=false;progress.setAttribute("aria-busy","true");progress.replaceChildren(stage,elapsed,cost);
  function clock(running=true){const seconds=Math.floor((performance.now()-started)/1000);elapsed.textContent=`${running?"경과":"소요"} ${Math.floor(seconds/60)}분 ${String(seconds%60).padStart(2,"0")}초`;}
  clock();const timer=setInterval(()=>{if(alive())clock();else clearInterval(timer);},1000);
  try{
    const result=await buttonTask(button,"저장 자료 평가 중…",()=>task(text=>{if(alive())stage.textContent=text;}));
    if(alive()){
      cost.textContent=result?.reused?"추가 LLM 호출 없음 · 저장 결과 재사용 · 이번 추가 비용 US$0":llmUsageText(result?.execution_usage);
      const status=result?.status;
      stage.textContent=status==="completed"?"저장 자료 평가 완료":status==="partial"?"저장 자료 평가 일부 완료 · 미평가·실패 항목을 확인하세요.":status==="failed"?"저장 자료 평가 실패 · 오류 내용을 확인하세요.":"서버 작업이 아직 진행·대기 중입니다. 저장 결과 새로고침으로 확인하세요.";
      if(result?.error)stage.textContent+=` · ${result.error}`;
    }
    return result;
  }catch(error){if(alive())stage.textContent=`저장 자료 평가 요청 실패 · ${error.message}`;throw error;}
  finally{clearInterval(timer);if(alive()){clock(false);stage.classList.remove("task-busy");progress.setAttribute("aria-busy","false");}}
}

/* Server-owned incremental collection; never reuse a client-only business snapshot. */
async function refreshBusinessCollection(code,notify=()=>{},retryFailed=false,valid=()=>true,mode="evaluate"){
  const owner=state.user.id,alive=()=>state.user?.id===owner&&valid();
  if(state.user.role==="viewer")return api(`/news/workflow/${code}`);
  const models=await api("/news/models"),chosen=models.selected==="default"?models.default:models.items.find(m=>m.id===models.selected);
  if(!alive())throw new Error("화면·계정이 변경되었습니다.");
  if(!chosen||!models.default)throw new Error("수집 계획·평가 모델을 설정하세요.");
  const cloud=chosen.provider==="openai"||models.default.provider==="openai";
  if(cloud&&!models.cloud_enabled)throw new Error("클라우드 사용 미허용 · 저장 결과만 조회합니다.");
  let result=await api(`/news/workflow/${code}/refresh`,{method:"POST",body:JSON.stringify({cloud_confirmed:cloud,retry_failed:retryFailed,mode})});
  for(let attempt=0;attempt<300&&["queued","running","deferred"].includes(result.status);attempt++){
    if(!alive())throw new Error("화면·계정이 변경되었습니다. 작업은 원래 소유자 영역에 보존됩니다.");
    notify(`${result.stage||"대기"} · 저장 계획·동일 자료 재사용`);
    await new Promise(resolve=>setTimeout(resolve,2000));
    if(!alive())throw new Error("화면·계정이 변경되었습니다.");
    result=result.status==="deferred"?await api(`/news/workflow/${code}/refresh`,{method:"POST",body:JSON.stringify({cloud_confirmed:cloud,retry_failed:retryFailed,mode})}):await api(`/news/workflow/${code}`);
  }
  if(alive())notify(result.reused?"변경 없음 · 저장 계획·자료·평가 재사용":`${result.stage||result.status}${result.error?" · "+result.error:""}`);
  return result;
}

function collectionWorkflowView(data,code,allowActions=true){
  const area=node("details"),owner=state.user.id,valid=()=>state.user?.id===owner&&area.isConnected;
  area.open=data?.status==="failed"||data?.error==="USER_APPROVED_CALL_LIMIT"||data?.error==="MODEL_INVALID_EVIDENCE_OR_SCORE";
  area.append(node("summary",`사업 기반 수집 계획 · ${data?.status||"미생성"}`));
  if(data?.execution_usage)area.append(node("p",`최근 실행 사용량 · ${llmUsageText(data.execution_usage)}`,"helper"));
  area.append(node("p",`${data?.stage||"상세 진입 또는 증분 갱신 실행 시 계획을 확인합니다."}${data?.error?" · "+data.error:""}`,"helper"));
  if(data?.error==="DAILY_COLLECTION_LIMIT")area.append(node("p",`앱의 일일 ${{search:"검색",dimension:"다차원",statistics:"통계"}[data.limit_kind]||"수집"} 한도를 사용했습니다. 외부 제공자 오류가 아니며 완료 자료는 보존됩니다. 데이터 수집 화면에서 한도·사용량을 확인하세요.`,"warning"));
  if(data?.error==="REQUEST_REJECTED_429")area.append(node("p","수집 요청이 429로 중단된 이전 기록입니다. 앱 내부 일일 한도와 외부 제공자 제한을 구분해 확인하세요. 완료 자료는 보존됩니다.","warning"));
  if(data?.error==="USER_APPROVED_CALL_LIMIT")area.append(node("p","사용자가 승인한 이번 실행의 LLM 호출 한도에 도달해 중단했습니다. 완료된 평가는 저장되어 있으며, 미평가 자료만 추가 승인 후 이어서 처리할 수 있습니다.","warning"));
  if(data?.freshness?.needs_refresh)area.append(node("p","원천 점검 주기 경과·자료 부족 · 데이터 수집 화면에서 갱신 상태를 확인하세요. 이전 평가를 최신 자료 평가로 간주하지 마세요.","warning"));
  if(data?.evaluation_required)area.append(node("p","수집 원문 변경 · 종목 분석에서 변경·미평가 자료의 평가가 필요합니다.","warning"));
  if(data?.evaluation_diagnostics)area.append(node("p",`평가 검증 실패 상세: ${JSON.stringify(data.evaluation_diagnostics)} · ${data.evaluation_diagnostics.remaining_batches_continued?"실패 기사는 미평가로 분리하고 나머지 묶음은 계속 평가했습니다. ":""}검증된 평가는 보존하며 실패·미평가 항목만 다음 명시적 실행에서 처리합니다.`,"warning"));
  if(data?.plan){
    area.append(node("p",`계획 모델: ${data.plan_model?.model||"미확인"} · 계획 프롬프트 버전 ${data.plan_prompt?.version||"미확인"}`,"helper"));
    if(data.counts)area.append(node("p",`수집 자료 ${data.counts.materials}건 · 발행시각·출처·공시 관련성 검증 제외 ${data.counts.rejected||0}건 · 평가 대기 ${data.pending||0}건`,"helper"));
    for(const query of data.plan.queries||[])area.append(node("p",`뉴스·공시 검색: ${query.query} · ${query.reason}`));
    area.append(node("p",`계획에서 선택한 통계: ${(data.plan.statistics||[]).join(" · ")||"없음 · 연결된 지표의 사업 관련성은 수집 제한 사유를 확인하세요."}`,"helper"));
    for(const gap of data.plan.gaps||[])area.append(node("p",`수집 제한·추가 자료: ${gap}`,"warning"));
    for(const search of data.searches||[])area.append(node("p",`실제 수집 질의: ${search.effective_query||"기존 기록"} · 최근 수집 ${formatTime(search.checked_at)} · ${search.shared_search_reused?"계정 내 공통 검색 재사용":"수집 실행"} · 반환 ${search.returned??"미기록"}건 · 종목/주제·중복 후보 제외 ${search.candidate_filtered||0}건 · 제외 사유 ${JSON.stringify(search.rejection_reasons||{})}`,"helper"));
    area.append(node("p","검색 제공자의 게시일은 최초 발행일 또는 수정일의 추정값입니다. 공식 공시 피드의 접수시각과 구분하며, 날짜만으로 새로운 발표라고 단정하지 않습니다.","helper"));
    area.append(node("p",`계획 생성 ${formatTime(data.plan_created_at)} · 같은 계정의 공통 산업 검색 12시간, 월간 통계 7일·연간 통계 30일 이내 재사용. 자동 수집은 저장 계획만 실행하며 회사별 영향 점수는 공유하지 않습니다.`,"helper"));
  }
  for(const stat of Object.values(data?.statistics_assessments||{})){
    area.append(node("h4",`${stat.source.name} · 통계 해석 (뉴스 합계 제외)`));
    for(const point of stat.observations||[])area.append(node("p",`${point.date}: ${point.value} · ${stat.source.unit}`,"helper"));
    for(const item of stat.items||[])area.append(node("p",`${item.score9==null?"판단 보류":`${item.score9} / -4~+4`} · ${item.reason} · 인용: ${item.evidence}`));
    const link=node("a","통계 원천");link.href=stat.source.url;link.target="_blank";link.rel="noopener noreferrer";area.append(link);
  }
  if(allowActions&&state.user.role!=="viewer"&&(data?.status==="failed"||(data?.status==="partial"&&data?.error==="MODEL_INVALID_EVIDENCE_OR_SCORE"))){
    const retry=node("button","실패 단계만 다시 실행","secondary");area.append(retry);retry.addEventListener("click",async()=>{if(!confirm("완료된 단계는 재사용하고 실패·부족 단계만 다시 실행합니다. 외부 검색·LLM 비용이 발생할 수 있습니다. 실행할까요?"))return;retry.disabled=true;try{const next=await llmButtonTask(retry,"실패 단계 재처리 중…",valid,()=>refreshBusinessCollection(code,text=>{if(valid())retry.textContent=text;},true,valid));if(valid())area.replaceWith(collectionWorkflowView(next,code));}catch(error){if(valid())retry.textContent=error.message;}});
  }
  if(allowActions&&state.user.role!=="viewer"&&["queued","running"].includes(data?.status)&&Date.now()-Date.parse(data.updated_at)>300000){
    const abandon=node("button","미완료 작업 종료","secondary");area.append(abandon);abandon.addEventListener("click",async()=>{if(!confirm("외부 요청의 처리 여부가 불명인 작업을 종료 상태로 기록합니다. 비용을 취소하지 않으며 자동 재실행하지 않습니다. 종료할까요?"))return;try{const next=await api(`/news/workflow/${code}/abandon`,{method:"POST"});if(valid())area.replaceWith(collectionWorkflowView(next,code));}catch(error){if(valid())abandon.textContent=error.message;}});
  }
  return area;
}
