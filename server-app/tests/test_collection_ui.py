import subprocess
from pathlib import Path


def test_detail_collection_rechecks_server_and_guards_account_switch():
    script = r"""
const fs=require("fs"), vm=require("vm"), assert=require("assert");
const calls=[];
const ctx={state:{user:{id:"owner-a",role:"trader"}},setTimeout,console};
ctx.api=async(path,options)=>{calls.push([path,options?.method||"GET"]);if(path==="/news/models")return {selected:"default",default:{provider:"vllm",model:"test"}};return {status:"completed",reused:true};};
vm.createContext(ctx);vm.runInContext(fs.readFileSync("collection-ui.js","utf8"),ctx);
(async()=>{
 await ctx.refreshBusinessCollection("123456");await ctx.refreshBusinessCollection("123456");
 assert.equal(calls.filter(c=>c[1]==="POST").length,2); // Only the server decides freshness.
 calls.length=0;ctx.api=async()=>{ctx.state.user={id:"owner-b",role:"trader"};return {selected:"default",default:{provider:"vllm",model:"test"}};};
 await assert.rejects(()=>ctx.refreshBusinessCollection("123456"));
 ctx.state.user={id:"viewer",role:"viewer"};ctx.api=async(path,options)=>{assert.equal(options,undefined);assert.equal(path,"/news/workflow/123456");return {status:"missing"};};
 await ctx.refreshBusinessCollection("123456");
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    subprocess.run(
        ["node", "-e", script],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
