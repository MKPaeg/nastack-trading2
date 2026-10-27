/* Stored personal reports. Opening this screen never starts analysis or orders. */
const reportKinds = {vector:"벡터 분석",news:"뉴스 평가",research:"연구 보고서",sale:"실현손익"};
const reportStatuses = {completed:"완료",insufficient_data:"자료 부족",partial:"일부 완료",running:"진행 중",queued:"대기",failed:"실패",model_failed:"모델 평가 실패",policy_changed:"정책 변경",expired_day:"기준일 만료",confirmed:"원가 확인",missing_cost:"원가 미확인"};
function reportNumber(value, digits=2) {
  if(value===null||value===undefined||value===""||!Number.isFinite(Number(value)))return "미확인";
  return Number(value).toLocaleString("ko-KR",{maximumFractionDigits:digits});
}
function reportForecast(value) {
  if(!value)return "추정 불가";
  return value.predicted_return==null?`추정 불가${value.reason?" · "+value.reason:""}`:`${reportNumber(value.predicted_return*100,3)}% · ${reportNumber(value.predicted_close,0)}원`;
}
function reportLink(url,label) {
  const link=node("a",label);
  try{const parsed=new URL(url);if(!["https:","http:"].includes(parsed.protocol)||parsed.username||parsed.password)return node("span",label);link.href=parsed.href;link.target="_blank";link.rel="noopener noreferrer";}catch{return node("span",label);}
  return link;
}
function reportTable(headers,rows) {
  const wrap=node("div",undefined,"table-wrap"),table=node("table"),head=node("thead"),tr=node("tr"),body=node("tbody");
  headers.forEach(text=>tr.append(node("th",text)));head.append(tr);table.append(head,body);
  for(const cells of rows){const row=node("tr");for(const text of cells)row.append(node("td",text??"미확인"));body.append(row);}
  wrap.append(table);return wrap;
}
function renderReportDetail(detail,area) {
  const {item,data}=detail;
  area.replaceChildren(node("h2",`${reportKinds[item.kind]} · ${item.name}`),node("p",`${item.code||"전체 산업"} · 저장 ${formatTime(item.created_at)} · ${reportStatuses[item.status]||item.status}`,"helper"));
  if(item.simulation)area.append(node("p","검증용 기록입니다. 실제 종목 분석·투자 성과가 아닙니다.","warning"));
  if(item.kind==="vector"){
    area.append(node("p",`분석 자료 기준 ${data.data_cutoff||"미확인"} · 예측 대상 ${data.today||"미확인"}`));
    area.append(node("p",`기간 ${data.request?.start||"미확인"} ~ ${data.request?.end||"미확인"} · 표본 ${data.summary?.samples??0}개 · 상관계수 ${reportNumber(data.summary?.correlation,4)}`));
    const rows=[];
    for(const group of data.horizon_results||[]){
      const method=data.request?.method||"pearson";
      const daily=(group.today_comparison||[]).find(v=>v.method===method)?.forecast;
      const bar=(group.forecasts||[]).find(v=>v.method===method);
      const forecast=daily||bar;
      rows.push([`${group.horizon===1?"일봉":group.horizon===5?"주봉":"월봉"} · ${method}`,forecast?.target_date||"미확인",reportForecast(forecast)]);
    }
    const integrated=(data.comparison||[]).find(v=>v.method===(data.request?.method||"pearson"));
    if(integrated)rows.push([`기간 통합 · ${integrated.method}`,integrated.forecast?.target_date||"미확인",reportForecast(integrated.forecast)]);
    if(rows.length)area.append(reportTable(["기간·방법","예측 대상일","저장된 예측"],rows));
    area.append(node("h3","분석 근거 · 사용 차원"));
    for(const dimension of data.dimensions||[]){const line=node("p",`${dimension.name} · 관측 ${dimension.aligned_observations??0}개 · 최종 ${dimension.last_observation||"미확인"} `);if(dimension.source_url)line.append(reportLink(dimension.source_url,dimension.source||"출처"));area.append(line);}
    for(const warning of data.warnings||[])area.append(node("p",warning,"helper"));
    const inputs=node("details");inputs.append(node("summary","당시 분석 조건"),node("pre",JSON.stringify(data.request,null,2),"result-json"));area.append(inputs);
  }else if(item.kind==="news"){
    area.append(node("p","당시 저장된 기사와 평가입니다. 이후 소스·모델·정책 변경에 따른 현재 평가와 다를 수 있습니다.","helper"));
    area.append(node("p",`모델 ${data.provider||"미확인"} / ${data.model||"미확인"} · 프롬프트 ${data.prompt_version||"미확인"}`,"helper"));
    area.append(node("p",`미평가 기사 ${data.pending_articles??"미확인"}개 · 점수는 -4~+4, 미평가는 0점이 아닙니다.`,"helper"));
    if(data.error)area.append(node("p",`실행 오류: ${data.error}`,"warning"));
    const articles=new Map((detail.articles||[]).map(article=>[article.id,article]));
    for(const assessment of data.assessments||[]){
      const article=articles.get(assessment.article_id),section=node("section",undefined,"panel");
      section.append(node("h3",article?.title||"기사"),node("p",`${assessment.dimension_id} · 영향 점수 ${assessment.score9==null?"판단 보류":assessment.score9}`),node("p",assessment.reason),node("blockquote",assessment.evidence),node("p",`발행 ${article?.published_at||"미확인"}`,"helper"));
      if(article?.url)section.append(reportLink(article.url,"기사 원문"));area.append(section);
    }
    if(!(data.assessments||[]).length)area.append(node("p","저장된 평가가 없습니다. 수집 전용·평가 실패·자료 부족 여부를 확인하세요.","helper"));
    const evidence=node("details");evidence.append(node("summary",`수집 기사 ${articles.size}개`));
    for(const article of articles.values())evidence.append(node("p",article.title),node("p",article.excerpt||""));area.append(evidence);
  }else if(item.kind==="sale"){
    area.append(node("p",`거래일 ${data.trade_day} · ${data.broker==="namuh"?"나무증권":"키움증권"} · 매도 체결 ${data.quantity}주`));
    area.append(reportTable(["항목","금액"],[["매도금액",`${reportNumber(data.proceeds)}원`],["확인된 매입원가",data.cost==null?"미확인":`${reportNumber(data.cost)}원`],["실현손익",data.profit==null?"산출 불가 · 매입원가 미확인":`${reportNumber(data.profit)}원`]]));
    area.append(node("p",`매도 사유: ${{take_profit:"익절",stop_loss:"손절",manual:"수동",automatic:"자동"}[data.reason]||"미확인"}`));
    area.append(node("p","앱에서 확인한 체결 기준입니다. 수수료·세금은 제외하며, 증권사 전체 거래의 순손익과 다를 수 있습니다.","helper"));
  }else{
    area.append(node("p",data.summary||"아직 저장된 결과가 없습니다."),node("p",`당시 설정 버전 ${data.settings_version} · 시도 ${data.attempts}회`,"helper"));
    if(data.model)area.append(node("p",`모델 ${data.model.provider||"미확인"} / ${data.model.model_id||"미확인"}`,"helper"));
    if(data.outlook)area.append(node("p",`동향: ${{positive:"긍정",negative:"부정",neutral:"중립",uncertain:"불확실"}[data.outlook]||data.outlook}`));
    for(const risk of data.risks||[])area.append(node("p",risk,"warning"));
    if(data.error_code)area.append(node("p",`오류: ${data.error_code}`,"warning"));
    for(const evidence of data.evidence||[]){area.append(node("h3",evidence.title),node("p",evidence.snippet),node("p",`발행 ${evidence.published_at||"미확인"} · 수집 ${evidence.retrieved_at||"미확인"}`,"helper"));if(evidence.url)area.append(reportLink(evidence.url,"원문 보기"));}
  }
}
async function renderReports() {
  const owner=state.user.id,section=panel("분석 리포트"),area=$("#liveContent");area.replaceChildren(section);
  section.append(node("p","언제 어떤 근거로 분석했는지, 실제로 얼마의 손익이 확인됐는지 되짚어보는 저장 기록입니다. 조회는 새 분석이나 주문을 실행하지 않습니다.","helper"));
  const draft=state.reportFilters||{},today=new Date().toLocaleDateString("en-CA",{timeZone:"Asia/Seoul"});
  const form=node("form",undefined,"source-row"),start=node("input"),end=node("input"),code=node("input"),kind=node("select"),demo=node("input"),button=node("button","리포트 조회","primary");
  start.type=end.type="date";start.value=draft.start||`${today.slice(0,4)}-01-01`;end.value=draft.end||today;start.required=end.required=true;
  code.placeholder="전체 또는 6자리 코드";code.pattern="[0-9]{6}";code.value=draft.code||"";
  for(const [key,label] of Object.entries({all:"전체 종류",...reportKinds})){const option=node("option",label);option.value=key;kind.append(option);}kind.value=draft.kind||"all";
  demo.type="checkbox";demo.checked=!!draft.include_demo;
  for(const [label,field] of [["시작일",start],["종료일",end],["종목 코드",code],["리포트 종류",kind],["검증용 기록 포함",demo]]){const wrap=node("label",label);field.setAttribute("aria-label",label);wrap.append(field);form.append(wrap);}button.type="submit";form.append(button);section.append(form);
  const message=node("p","","helper"),counts=node("p","","helper"),list=node("div"),details=panel("리포트 상세");message.setAttribute("role","status");section.append(message,counts,list);area.append(details);
  details.append(node("p","목록에서 ‘상세 보기’를 선택하면 분석 조건·예측·기사 근거 또는 실제 매도 손익을 확인할 수 있습니다.","helper"));
  let generation=0;
  const valid=()=>state.user?.id===owner&&state.view==="reports"&&section.isConnected;
  async function load(page=1){
    const request=++generation;button.disabled=true;message.textContent="저장 기록 조회 중…";
    const filters={start:start.value,end:end.value,code:code.value,kind:kind.value,include_demo:demo.checked};
    try{
      const params=new URLSearchParams({...filters,page,page_size:10}),data=await api(`/reports?${params}`);
      if(!valid()||request!==generation)return;
      state.reportFilters=filters;persistScreenState();
      counts.textContent=Object.entries(data.counts).map(([key,count])=>`${reportKinds[key]} ${count}건`).join(" · ");message.textContent=`총 ${data.total}건 · 날짜는 한국 시간 기준, 실현손익은 거래일 기준입니다.`;
      list.replaceChildren();details.replaceChildren(node("h2","리포트 상세"),node("p","목록의 상세 보기를 선택하세요.","helper"));
      if(!data.items.length)list.append(node("p","선택한 조건의 저장 기록이 없습니다. 종목분석에서 벡터·뉴스 분석을 저장하면 이곳에 나타납니다. 실현손익은 확인된 매도 체결이 있어야 표시됩니다.","helper"));
      const table=node("table"),head=node("thead"),header=node("tr"),body=node("tbody");for(const title of ["저장 시각 / 거래일","종목","종류·상태","내용","상세"])header.append(node("th",title));head.append(header);table.append(head,body);
      for(const item of data.items){const row=node("tr"),cell=node("td"),open=node("button","상세 보기","secondary");row.append(node("td",item.trade_day||formatTime(item.created_at)),node("td",`${item.name} ${item.code}`),node("td",`${item.simulation?"검증용 · ":""}${reportKinds[item.kind]} · ${reportStatuses[item.status]||item.status}`),node("td",item.kind==="sale"?`${item.summary} · 손익 ${item.profit==null?"미확인":reportNumber(item.profit)+"원"}`:item.summary));
        open.addEventListener("click",async()=>{const selected=++generation;open.disabled=true;try{const detail=await api(`/reports/${item.kind}/${item.id}`);if(!valid()||selected!==generation)return;renderReportDetail(detail,details);details.scrollIntoView({block:"start",behavior:"smooth"});}catch(error){if(valid())message.textContent=error.message;}finally{open.disabled=false;}});cell.append(open);row.append(cell);body.append(row);}
      const wrap=node("div",undefined,"table-wrap");wrap.append(table);list.append(wrap);
      const pager=node("div",undefined,"pagination");for(const [label,next] of [["이전",page-1],["다음",page+1]]){const control=node("button",label);control.disabled=next<1||(next>page&&page*10>=data.total);control.addEventListener("click",()=>load(next));pager.append(control);}pager.append(node("span",`${page} / ${Math.max(1,Math.ceil(data.total/10))}`));list.append(pager);
    }catch(error){if(valid())message.textContent=error.message;}finally{if(valid())button.disabled=false;}
  }
  form.addEventListener("submit",event=>{event.preventDefault();load();});await load();
}
