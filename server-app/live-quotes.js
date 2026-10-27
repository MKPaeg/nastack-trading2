// User-scoped quotes: serialized public lane and bounded independent trading lane.
const liveQuotes = (() => {
  const entries = new Map();
  let busy = false, session = null, checkedAt = 0, nextRequest = 0;
  const owner = () => state.user ? `${state.user.tenant_id || ''}:${state.user.id || state.user.email}` : null;
  function subscribe(element, code, paint, planId=null) {
    entries.set(element, {element, code, paint, planId, owner: owner(), due: 0, snapshot: null});
  }
  async function publicTick() {
    if (busy || document.hidden || !owner()) return;
    for (const [element, entry] of entries) if (!element.isConnected || entry.owner !== owner()) entries.delete(element);
    if (![...entries.values()].some(e=>!e.planId) || Date.now() < nextRequest) return;
    busy = true;
    const ticket = owner();
    let requestedEntries=[];
    try {
      if (!session || Date.now() - checkedAt > 30000) {
        session = await api('/market/session'); checkedAt = Date.now();
      }
      if (ticket !== owner()) return;
      const entry = [...entries.values()].find(e => !e.planId && e.due <= Date.now());
      if (!entry) return;
      const matches = [...entries.values()].filter(e => e.code === entry.code && e.planId === entry.planId);
      requestedEntries=matches;
      let snapshot;
      if(entry.planId) {
        snapshot = (await api(`/trading/plans/${entry.planId}/quote`)).snapshot;
      } else if (session.is_open && state.user.role !== 'viewer') {
        snapshot = (await api(`/market/instruments/${entry.code}/refresh?auto=true&quote_only=true`, {method:'POST'})).snapshot;
      } else {
        snapshot = (await api(`/market/instruments/${entry.code}`)).snapshot;
      }
      if (ticket !== owner()) return;
      for (const e of matches) {
        if (!e.element.isConnected || entries.get(e.element) !== e) continue;
        e.due = Date.now() + (session.is_open ? 5000 : 60000);
        if (snapshot?.price != null) {
          e.snapshot = snapshot;
          e.paint(snapshot, session.is_open);
          e.element.querySelector('[data-quote-error]')?.remove();
        }
      }
    } catch (error) {
      // Keep the last successful price; never replace it with zero or a blank.
      for (const e of requestedEntries.length?requestedEntries:entries.values()) if (e.due <= Date.now() && e.owner === ticket) {
        e.due = Date.now() + 30000;
        if(e.snapshot)e.paint({...e.snapshot,stale:true},session?.is_open);
        if (!e.element.querySelector('[data-quote-error]')) {
          const note = document.createElement('small'); note.dataset.quoteError = 'true';
          note.textContent = `갱신 지연 · 마지막 표시 가격 유지 · ${error.message || '연결 확인 필요'}`; e.element.append(note);
        }
      }
      nextRequest = Date.now() + 1000;
    } finally { nextRequest = Math.max(nextRequest, Date.now() + 1000); busy = false; }
  }
  // Separate bounded lane: a slow/expired broker must not block another trading row.
  const tradeInflight=new Set();
  async function tradeTick(){
    if(document.hidden||!owner())return;
    const ticket=owner(),jobs=[];
    // Oldest due row first: fast rows must not repeatedly occupy both slots.
    for(const e of [...entries.values()].sort((a,b)=>a.due-b.due)){
      if(!e.planId||!e.element.isConnected||e.owner!==ticket||e.due>Date.now())continue;
      const key=`${ticket}:${e.planId}`;
      if(tradeInflight.has(key))continue;
      if(tradeInflight.size>=2)break;
      const matches=[...entries.values()].filter(v=>v.owner===ticket&&v.planId===e.planId);
      const requestedAt=Date.now();
      tradeInflight.add(key);
      jobs.push((async()=>{try{
        const snapshot=(await api(`/trading/plans/${e.planId}/quote`)).snapshot;
        if(owner()!==ticket)return;
        for(const row of matches){
          if(!row.element.isConnected||entries.get(row.element)!==row)continue;
          // Measure cadence from request start, not after broker latency.
          row.due=session?.is_open===false?Date.now()+60000:Math.max(requestedAt+(snapshot?.refresh_after_ms||1000),Date.now());
          if(snapshot?.price!=null){row.snapshot=snapshot;row.paint(snapshot,true);row.element.querySelector('[data-quote-error]')?.remove();if(snapshot.refresh_note){const note=document.createElement('small');note.dataset.quoteError='true';note.textContent=`마지막 성공 시세 유지 · ${snapshot.refresh_note}`;row.element.append(note);}}
        }
      }catch(error){if(owner()!==ticket)return;for(const row of matches){
        if(!row.element.isConnected||entries.get(row.element)!==row)continue;
        row.due=Date.now()+30000;
        if(row.snapshot)row.paint({...row.snapshot,stale:true},true);
        if(!row.element.querySelector('[data-quote-error]')){const note=document.createElement('small');note.dataset.quoteError='true';note.textContent=`갱신 지연 · ${error.message||'연결 확인 필요'}`;row.element.append(note);}
      }}finally{tradeInflight.delete(key);}})());
    }
    await Promise.all(jobs);
  }
  async function tick(){await Promise.all([publicTick(),tradeTick()]);}
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { checkedAt = 0; tick(); } });
  setInterval(tick, 1000);
  return {subscribe, tick};
})();

function bindLivePrice(element, code, paint, planId=null) {
  liveQuotes.subscribe(element, code, paint || ((snapshot, open) => {
    element.replaceChildren(node('strong', priceText(snapshot.price)),
      node('small', `${open ? '장중 자동 갱신 · 주기 조회' : '장외 저장 시세'} · ${formatTime(snapshot.collected_at)}`));
    if (Number.isFinite(snapshot.change_rate)) element.append(node('small', movementText(snapshot.change_rate, '%'), movementClass(snapshot.change_rate)));
  }), planId);
}
