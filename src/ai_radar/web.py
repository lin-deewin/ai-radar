"""Web 仪表盘。FastAPI + 内嵌单文件 HTML，零前端构建。

接口:
  GET /                -> 主页（含前端）
  GET /api/articles    -> JSON 文章列表 (?days=&tag=&limit=&q=&source=)
  GET /api/stats       -> 聚合统计（按源/按天/按 tag）
  GET /api/topics      -> 主题词云
  GET /api/reports     -> 已生成日报列表
  GET /api/reports/{name} -> 单个日报 markdown
  POST /api/summarize  -> 触发后台摘要 {top: N}
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from .storage import Storage

log = logging.getLogger("ai_radar.web")


def create_app(db_path: Path, reports_dir: Path) -> FastAPI:
    app = FastAPI(title="AI Radar", docs_url="/docs")
    store = Storage(db_path)
    reports_dir = Path(reports_dir)

    # ---------- API ----------
    @app.get("/api/articles", response_class=JSONResponse)
    def api_articles(days: int = 3, tag: str | None = None,
                     limit: int = 100, q: str | None = None,
                     source: str | None = None) -> list[dict[str, Any]]:
        arts = store.query_recent(days=days, limit=max(1, min(limit, 1000)),
                                   tag=tag)
        out = []
        for a in arts:
            if source and a.source != source:
                continue
            if q:
                ql = q.lower()
                hay = (a.title + " " + a.summary + " "
                       + " ".join(a.extra.get("topics") or [])).lower()
                if ql not in hay:
                    continue
            out.append(_article_to_dict(a))
        return out

    @app.get("/api/stats", response_class=JSONResponse)
    def api_stats(days: int = 7) -> dict[str, Any]:
        arts = store.query_recent(days=days, limit=5000)
        by_source: dict[str, int] = {}
        by_tag: dict[str, int] = {}
        by_day: dict[str, int] = {}
        by_topic: dict[str, int] = {}
        for a in arts:
            by_source[a.source] = by_source.get(a.source, 0) + 1
            for t in a.tags:
                by_tag[t] = by_tag.get(t, 0) + 1
            for t in (a.extra.get("topics") or []):
                by_topic[t] = by_topic.get(t, 0) + 1
            d = ""
            if a.published:
                try:
                    p = a.published
                    if p.tzinfo is None:
                        p = p.replace(tzinfo=timezone.utc)
                    d = p.astimezone().strftime("%Y-%m-%d")
                except Exception:
                    d = ""
            if d:
                by_day[d] = by_day.get(d, 0) + 1
        return {
            "total": len(arts),
            "by_source": _top(by_source, 20),
            "by_tag": by_tag,
            "by_day": dict(sorted(by_day.items())),
            "by_topic": _top(by_topic, 40),
        }

    @app.get("/api/reports", response_class=JSONResponse)
    def api_reports() -> list[dict[str, str]]:
        if not reports_dir.exists():
            return []
        files = sorted(reports_dir.glob("*.md"), reverse=True)
        return [{"name": f.name, "date": f.stem,
                 "size": str(f.stat().st_size)} for f in files[:50]]

    @app.get("/api/reports/{name}", response_class=PlainTextResponse)
    def api_report(name: str) -> str:
        p = (reports_dir / name).resolve()
        try:
            p.relative_to(reports_dir.resolve())
        except ValueError:
            raise HTTPException(404)
        if not p.exists() or not p.name.endswith(".md"):
            raise HTTPException(404)
        return p.read_text(encoding="utf-8")

    class SummarizeReq(BaseModel):
        top: int = 30
        min_score: float = 0.5

    @app.post("/api/summarize", response_class=JSONResponse)
    def api_summarize(req: SummarizeReq) -> dict[str, Any]:
        from .summarizer import Summarizer, attach_summary
        sm = Summarizer()
        arts = store.query_unsummarized(limit=req.top,
                                         min_score=req.min_score)
        done = 0
        for a in arts:
            try:
                r = sm.summarize(a)
                attach_summary(a, r)
                store.update_summary(a.url_norm, {
                    "summary_zh": r.summary_zh,
                    "key_points": r.key_points,
                    "topics": r.topics,
                    "llm_backend": r.backend,
                    "llm_model": r.model,
                })
                done += 1
            except Exception as e:
                log.warning("summarize fail: %s", e)
        return {"done": done, "total": len(arts), "backend": sm.backend}

    # ---------- 前端 ----------
    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _render_html()

    return app


# ---------- helpers ----------

def _top(d: dict[str, int], n: int) -> dict[str, int]:
    return dict(sorted(d.items(), key=lambda x: -x[1])[:n])


def _article_to_dict(a) -> dict[str, Any]:
    return {
        "title": a.title,
        "url": a.url,
        "source": a.source,
        "source_type": a.source_type,
        "tags": a.tags,
        "lang": a.lang,
        "authority": a.authority,
        "engagement": a.engagement,
        "score": round(a.score, 3),
        "published": a.published.isoformat() if a.published else None,
        "authors": a.authors[:5],
        "summary": (a.summary or "")[:400],
        "summary_zh": a.extra.get("summary_zh") or "",
        "key_points": a.extra.get("key_points") or [],
        "topics": a.extra.get("topics") or [],
        "pdf": a.extra.get("pdf") or "",
        "arxiv_id": a.extra.get("arxiv_id") or "",
        "github": a.extra.get("github") or "",
        "project": a.extra.get("project") or "",
        "llm_backend": a.extra.get("llm_backend") or "",
    }


_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>AI Radar · 前沿情报仪表盘</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#c9d1d9;--muted:#8b949e;--accent:#58a6ff;--ai:#f78166;--hw:#3fb950;--tag:#1f6feb}
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--text);font-size:14px;line-height:1.6}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
header{padding:16px 24px;border-bottom:1px solid var(--border);background:#010409;position:sticky;top:0;z-index:10}
header h1{margin:0;font-size:18px;font-weight:600}header .sub{color:var(--muted);font-size:12px;margin-top:2px}
.layout{display:grid;grid-template-columns:240px 1fr;min-height:calc(100vh - 60px)}
aside{border-right:1px solid var(--border);padding:16px;background:#010409}
aside h3{font-size:12px;text-transform:uppercase;color:var(--muted);margin:14px 0 6px;letter-spacing:.5px}
aside .ctrl{margin-bottom:10px}
aside label{display:block;font-size:12px;color:var(--muted);margin-bottom:4px}
aside select,aside input{width:100%;padding:6px 8px;background:var(--card);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px}
aside button{padding:6px 10px;background:var(--accent);color:#000;border:0;border-radius:6px;cursor:pointer;font-weight:600;width:100%}
aside button.ghost{background:var(--card);color:var(--text);border:1px solid var(--border);margin-top:6px}
.stat{font-size:12px;color:var(--muted);padding:6px 0}
.stat b{color:var(--text)}
.bar{height:4px;background:var(--card);border-radius:2px;overflow:hidden;margin:2px 0 8px}
.bar>div{height:100%;background:var(--accent)}
main{padding:20px 24px;max-width:1200px}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--border);margin-bottom:16px}
.tabs button{background:none;border:0;color:var(--muted);padding:8px 14px;cursor:pointer;font-size:14px;border-bottom:2px solid transparent}
.tabs button.active{color:var(--text);border-bottom-color:var(--accent)}
.card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px 16px;margin-bottom:12px;transition:border-color .15s}
.card:hover{border-color:var(--accent)}
.card .head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}
.card .title{font-size:15px;font-weight:600;flex:1}
.card .score{font-size:12px;color:var(--muted);white-space:nowrap}
.card .meta{margin:6px 0;color:var(--muted);font-size:12px}
.card .meta .badge{display:inline-block;padding:1px 6px;border-radius:4px;background:#21262d;color:var(--text);margin-right:6px}
.card .meta .badge.ai{background:rgba(247,129,102,.15);color:var(--ai)}
.card .meta .badge.hw{background:rgba(63,185,80,.15);color:var(--hw)}
.card .zh{margin:8px 0;padding:8px 10px;background:#0d1117;border-left:3px solid var(--accent);border-radius:4px;font-size:13px}
.card .kp{margin:6px 0 0;padding-left:18px}.card .kp li{margin:2px 0}
.card .topics{margin-top:6px}.card .topics span{display:inline-block;background:rgba(31,111,235,.15);color:var(--accent);padding:1px 8px;border-radius:10px;font-size:11px;margin:2px 4px 2px 0}
.card .links{margin-top:8px;font-size:12px;color:var(--muted)}
.empty{text-align:center;color:var(--muted);padding:60px 0}
.pager{display:flex;justify-content:center;gap:8px;margin:20px 0}
.pager button{padding:6px 12px;background:var(--card);border:1px solid var(--border);border-radius:6px;color:var(--text);cursor:pointer}
.pager button:disabled{opacity:.4;cursor:not-allowed}
.report-view{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:20px 24px;white-space:pre-wrap;font-family:ui-monospace,Menlo,monospace;font-size:13px;max-height:calc(100vh - 200px);overflow:auto}
.topic-cloud{display:flex;flex-wrap:wrap;gap:6px}
.topic-cloud span{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:3px 10px;font-size:12px;color:var(--text)}
.topic-cloud span.big{font-size:16px;color:var(--accent)}
.topic-cloud span.mid{font-size:14px;color:var(--text)}
</style></head><body>
<header><h1>📡 AI Radar · 前沿情报仪表盘</h1>
<div class="sub">AI / 硬件前沿聚合 · 多源去重 · LLM 中文摘要</div></header>
<div class="layout">
<aside>
<h3>筛选</h3>
<div class="ctrl"><label>主题</label><select id="f-tag"><option value="">全部</option><option value="ai">AI / 大模型</option><option value="hardware">硬件 / 芯片</option></select></div>
<div class="ctrl"><label>时间范围</label><select id="f-days"><option>1</option><option selected>3</option><option>7</option><option>14</option></select></div>
<div class="ctrl"><label>来源</label><select id="f-source"><option value="">全部</option></select></div>
<div class="ctrl"><label>关键词</label><input id="f-q" placeholder="搜索标题/摘要/主题"></div>
<div class="ctrl"><label>条数</label><select id="f-limit"><option>50</option><option selected>100</option><option>200</option><option>500</option></select></div>
<button onclick="reload()">刷新</button>
<button class="ghost" onclick="doSummary()">LLM 摘要 Top30</button>
<h3>统计</h3>
<div id="stats" class="stat">加载中…</div>
</aside>
<main>
<div class="tabs">
<button class="active" data-tab="list" onclick="tab('list')">📋 文章列表</button>
<button data-tab="topics" onclick="tab('topics')">☁️ 主题云</button>
<button data-tab="reports" onclick="tab('reports')">📄 日报归档</button>
</div>
<div id="view-list"><div id="cards"></div><div class="pager" id="pager"></div></div>
<div id="view-topics" hidden><div class="topic-cloud" id="cloud"></div></div>
<div id="view-reports" hidden><div id="report-list"></div><div id="report-view" class="report-view" hidden></div></div>
</main>
</div>
<script>
let cur=0, perPage=20, allArts=[];
const $=s=>document.querySelector(s);
async function api(u){const r=await fetch(u);return r.json();}
function tab(t){
 document.querySelectorAll('.tabs button').forEach(b=>b.classList.toggle('active',b.dataset.tab===t));
 ['list','topics','reports'].forEach(v=>$('#view-'+v).hidden=(v!==t));
 if(t==='topics')loadTopics();if(t==='reports')loadReports();
}
async function reload(){
 const p=new URLSearchParams({days:$('#f-days').value,limit:$('#f-limit').value});
 if($('#f-tag').value)p.set('tag',$('#f-tag').value);
 if($('#f-source').value)p.set('source',$('#f-source').value);
 if($('#f-q').value)p.set('q',$('#f-q').value);
 allArts=await api('/api/articles?'+p);
 cur=0;render();
 loadStats();loadSources();
}
function render(){
 const slice=allArts.slice(cur,cur+perPage);
 $('#cards').innerHTML=slice.length?slice.map(card).join(''):'<div class="empty">无数据。试试调整筛选或先 <code>python -m ai_radar run</code>。</div>';
 const total=allArts.length;
 $('#pager').innerHTML=`<button ${cur===0?'disabled':''} onclick="cur-=perPage;render()">上一页</button>
   <span style="color:var(--muted);align-self:center">${cur+1}-${Math.min(cur+perPage,total)} / ${total}</span>
   <button ${cur+perPage>=total?'disabled':''} onclick="cur+=perPage;render()">下一页</button>`;
}
function card(a){
 const tagBadge=a.tags.map(t=>`<span class="badge ${t==='hardware'?'hw':'ai'}">${t==='ai'?'AI':'硬件'}</span>`).join('');
 const zh=a.summary_zh?`<div class="zh"><b>中文摘要</b>${a.llm_backend&&a.llm_backend!=='extractive'?` <small style="color:var(--muted)">(${a.llm_backend})</small>`:''}：${esc(a.summary_zh)}</div>`:'';
 const kp=a.key_points&&a.key_points.length?`<ul class="kp">${a.key_points.map(k=>`<li>${esc(k)}</li>`).join('')}</ul>`:'';
 const topics=a.topics&&a.topics.length?`<div class="topics">${a.topics.map(t=>`<span>${esc(t)}</span>`).join('')}</div>`:'';
 const links=[];
 if(a.pdf)links.push(`<a href="${a.pdf}" target="_blank">PDF</a>`);
 if(a.github)links.push(`<a href="${a.github}" target="_blank">Code</a>`);
 if(a.project)links.push(`<a href="${a.project}" target="_blank">Project</a>`);
 if(a.arxiv_id)links.push(`arXiv: <code>${a.arxiv_id}</code>`);
 const linkStr=links.length?`<div class="links">${links.join(' · ')}</div>`:'';
 const date=a.published?a.published.slice(0,10):'';
 return `<div class="card"><div class="head"><div class="title"><a href="${a.url}" target="_blank">${esc(a.title)}</a></div><div class="score">⭐ ${a.score}</div></div>
  <div class="meta">${tagBadge}<span class="badge">${esc(a.source)}</span> ${date} ${a.engagement?'· 🔥 '+a.engagement:''}</div>
  ${zh}${kp}${topics}${linkStr}</div>`;
}
function esc(s){return (s||'').replace(/[<>&"]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));}
async function loadStats(){
 const s=await api('/api/stats?days='+$('#f-days').value);
 let h=`<div>共 <b>${s.total}</b> 条</div>`;
 h+=`<h3>按源 Top</h3>`+Object.entries(s.by_source).slice(0,8).map(([k,v])=>`<div>${esc(k)} <b>${v}</b></div><div class="bar"><div style="width:${Math.round(v/s.total*100)}%"></div></div>`).join('');
 h+=`<h3>按主题</h3>`+Object.entries(s.by_tag).map(([k,v])=>`<div>${k} <b>${v}</b></div>`).join('');
 $('#stats').innerHTML=h;
}
async function loadSources(){
 const s=await api('/api/stats?days=14');
 const sel=$('#f-source');
 const cur=sel.value;
 sel.innerHTML='<option value="">全部</option>'+Object.keys(s.by_source).map(k=>`<option>${esc(k)}</option>`).join('');
 sel.value=cur;
}
async function loadTopics(){
 const s=await api('/api/stats?days=14');
 const arr=Object.entries(s.by_topic||{});
 if(!arr.length){$('#cloud').innerHTML='<div class="empty">无主题数据。先运行 <code>summarize</code> 生成主题。</div>';return;}
 const max=arr[0][1];
 $('#cloud').innerHTML=arr.map(([k,v])=>`<span class="${v>max*.6?'big':v>max*.3?'mid':''}">${esc(k)} <small style="color:var(--muted)">${v}</small></span>`).join('');
}
async function loadReports(){
 const list=await api('/api/reports');
 $('#report-view').hidden=true;
 $('#report-list').innerHTML=list.length?list.map(r=>`<div class="card" onclick="openReport('${r.name}')"><div class="head"><div class="title">📄 ${r.date}</div><div class="score">${(r.size/1024).toFixed(1)} KB</div></div></div>`).join(''):'<div class="empty">尚无日报。运行 <code>python -m ai_radar run</code> 生成。</div>';
}
async function openReport(name){
 const t=await fetch('/api/reports/'+name).then(r=>r.text());
 $('#report-list').hidden=true;
 $('#report-view').hidden=false;
 $('#report-view').textContent=t;
}
async function doSummary(){
 const r=await fetch('/api/summarize',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({top:30,min_score:0.5})});
 const d=await r.json();
 alert(`完成: ${d.done}/${d.total} (后端: ${d.backend})`);
 reload();
}
reload();
</script></body></html>"""
def _render_html() -> str:
    return _HTML
