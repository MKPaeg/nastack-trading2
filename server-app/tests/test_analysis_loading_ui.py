import subprocess
from pathlib import Path


def test_saved_vector_is_visible_before_verification_and_late_owner_result_is_ignored():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
class Element {
 constructor(text=''){this.textContent=text;this.children=[];this.dataset={};this.isConnected=true;}
 append(...v){this.children.push(...v);} prepend(...v){this.children.unshift(...v);}
 replaceChildren(...v){this.children=v;this.textContent='';} setAttribute(){}
}
const node=(tag,text)=>new Element(text), panel=()=>node('section');
const state={user:{id:'A',role:'trader'}}, calls=[];
let finish;
const saved={persistence:{preview:true,reused:true},comparison:[{method:'pearson',forecast:{predicted_return:.01,predicted_close:123,base_close:100}}]};
const api=async(path,opts)=>{
 calls.push(path);
 if(path==='/vectors/forecast-clock')return {available_cutoff:'2026-10-23'};
 if(path.startsWith('/vectors/defaults/'))return {profile:{selected_dimensions:['sector-01']}};
 if(path==='/vectors/preview')return saved;
 if(path==='/vectors/analyze')return new Promise(resolve=>{finish=resolve;});
 if(path.startsWith('/news/workflow/'))return {status:'completed'};
 if(path.startsWith('/news/today'))return {material_counts:{reviewed:1},analysis_pool_count:1};
 throw Error(path);
};
const ctx={node,panel,state,api,requestIdentifier:()=> 'ticket',seoulDate:()=> '2026-10-24',
 vectorAnalysisRequest:()=>({code:'123456'}),priceText:String,collectionWorkflowView:()=>node('details'),
 renderSavedBusiness:()=>{},
 setInterval,clearInterval,Date,Math,Promise};
vm.createContext(ctx);
const source=fs.readFileSync('app.js','utf8');
vm.runInContext('function renderDetailAnalysis'+source.split('function renderDetailAnalysis')[1].split('async function renderStockDetail')[0],ctx);
const text=e=>e.textContent+' '+e.children.map(text).join(' '),box=node('section');
ctx.renderDetailAnalysis(box,{code:'123456',name:'test'});
(async()=>{
 await new Promise(resolve=>setImmediate(resolve));
 assert.match(text(box),/저장 결과 우선 표시/);
 assert.match(text(box),/123/);
 assert(!calls.some(path=>path.endsWith('/collect')||path.endsWith('/refresh')));
 const before=text(box);state.user={id:'B'};
 finish({...saved,persistence:{reused:true},comparison:[{method:'pearson',forecast:{predicted_return:.1,predicted_close:999999}}]});
 await new Promise(resolve=>setImmediate(resolve));
 assert(!text(box).includes('999999'));assert.equal(text(box),before);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    subprocess.run(
        ["node", "-e", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )


def test_collection_status_is_not_mislabelled_as_analysis_or_raw_server_error():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const ctx={formatTime:v=>v};vm.createContext(ctx);
vm.runInContext(fs.readFileSync('data-collection-ui.js','utf8'),ctx);
assert.match(ctx.collectionResourceStatus({enabled:true,status:'failed',error:'REQUEST_REJECTED_502',due_at:'tomorrow'}),/원천 조회·응답 검증 실패/);
assert.match(ctx.collectionResourceStatus({enabled:true,status:'failed',error:'REQUEST_REJECTED_502',due_at:'tomorrow'}),/이전 자료 보존/);
assert.match(ctx.collectionResourceStatus({enabled:true,due:true}),/順次|순차 갱신 대기/);
assert.equal(ctx.collectionResourceStatus({enabled:false,status:'running'}),'자동 갱신 꺼짐');
"""
    subprocess.run(
        ["node", "-e", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )


def test_async_button_loading_lock_failure_restore_and_owner_switch():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const state={user:{id:'A'}},ctx={state};vm.createContext(ctx);
const source=fs.readFileSync('app.js','utf8');
vm.runInContext('async function buttonTask'+source.split('async function buttonTask')[1].split('async function renderDimensionData')[0],ctx);
function button(){return {dataset:{},disabled:false,textContent:'original',attrs:{},
 classList:{add(){},remove(){}},setAttribute(k,v){this.attrs[k]=v;}};}
(async()=>{
 const b=button();let finish,calls=0;
 const first=ctx.buttonTask(b,'working',()=>{calls++;return new Promise(resolve=>finish=resolve);});
 assert(b.disabled);assert.equal(b.attrs['aria-busy'],'true');assert.equal(b.textContent,'working');
 await ctx.buttonTask(b,'duplicate',()=>calls++);assert.equal(calls,1);
 finish();await first;assert(!b.disabled);assert.equal(b.textContent,'original');assert.equal(b.attrs['aria-busy'],'false');
 await assert.rejects(ctx.buttonTask(b,'failure',async()=>{throw Error('failure');}));
 assert(!b.disabled);assert.equal(b.textContent,'original');
 const late=ctx.buttonTask(b,'switching',()=>new Promise(resolve=>finish=resolve));
 state.user={id:'B'};finish();await late;assert(b.disabled);assert.equal(b.attrs['aria-busy'],'false');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    subprocess.run(
        ["node", "-e", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
