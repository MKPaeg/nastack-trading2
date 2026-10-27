"""Stored-only, synthetic inference; never a real search/model invocation."""
import json
from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from nastack_trading.db import now
from nastack_trading.models import NewsArticle, User
from nastack_trading.news import NewsRequest, collect_news
from test_collection_pipeline import workflow_client


@pytest.mark.parametrize("failure", ["quote", "schema", "whole_batch"])
def test_later_batches_continue_valid_siblings_survive_and_retry_only_pending(workflow_client, setup, failure):
    _, _, control, uid, tid, sid = workflow_client
    factory=setup[1];ids=[]
    with factory.begin() as db:
        for i in range(12):
            row=NewsArticle(owner_id=uid,tenant_id=tid,source_id=sid,fingerprint=f"batch-isolation-{i}",
                published_at=now()-timedelta(minutes=i),news_date=now().astimezone(ZoneInfo('Asia/Seoul')).date(),
                value={"title":f"반도체 장비 수요 증가 {i}","excerpt":f"제공된 사업 근거와 투자 발표 {i}","url":f"https://trendforce.com/news/batch-{i}","content_type":"news"})
            db.add(row);db.flush();ids.append(row.id)
    calls=[];fail=True
    def respond(request):
        body=json.loads(json.loads(request.content)["messages"][1]["content"])
        articles=body["articles"];calls.append([a["id"] for a in articles])
        values=[{"article_id":a["id"],"dimension_id":"stock-123456","score9":1,"reason":"원문 사실에 근거한 참고 평가","evidence":a["title"],"relevance":"industry"} for a in articles]
        if fail and ids[0] in calls[-1]:
            if failure=="whole_batch":
                return httpx.Response(200,json={"choices":[{"message":{"content":"not valid JSON"}}]})
            values[0].update({"evidence":"제공 원문에 존재하지 않는 허위 인용"} if failure=="quote" else {"score9":"bad"})
        return httpx.Response(200,json={"choices":[{"message":{"content":json.dumps({"assessments":values},ensure_ascii=False)}}]})
    transport=httpx.MockTransport(respond)
    payload=NewsRequest(code="123456",source_ids=[sid],stored_only=True,max_articles=30)
    def execute():
        with factory() as db:
            result=collect_news(db,db.get(User,uid),payload,control["config"],transport,article_ids=ids,incremental=True)
            return result.status,dict(result.value)
    status,result=execute()
    missing=5 if failure=="whole_batch" else 1
    assert len(calls)==3 and sum(map(len,calls))==12
    assert status=="partial" and result["pending_articles"]==missing
    assert len(result["evaluated_article_ids"])==12-missing
    assert ids[0] not in result["evaluated_article_ids"]
    assert ids[-1] in result["evaluated_article_ids"]
    assert result["evaluation_diagnostics"]["remaining_batches_continued"]
    assert "허위 인용" not in json.dumps(result["evaluation_diagnostics"],ensure_ascii=False)
    fail=False;calls.clear()
    status,retried=execute()
    assert status=="completed" and retried["pending_articles"]==0
    assert len(calls)==1 and len(calls[0])==missing
    assert len(retried["evaluated_article_ids"])==12
    calls.clear();status,reused=execute()
    assert status=="completed" and not calls and len(reused["assessments"])==12
