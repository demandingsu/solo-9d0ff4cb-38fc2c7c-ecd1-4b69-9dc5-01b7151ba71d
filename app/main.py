"""FastAPI 入口：公告采集 API + 开标排期核查 API。

端点：
* GET  /                          演示页（可直接在浏览器发起采集/排期核查）
* POST /api/v1/collect            传入列表页 URL 与项目编号，返回合并后的投标状态
* GET  /api/v1/collect            同上的查询参数形式，便于浏览器/curl 快速演示
* POST /api/v1/schedule-check     传入开标日期与排期目录 URL，返回同室时间交叠冲突
* GET  /api/v1/schedule-check     同上的查询参数形式
* GET  /healthz                   健康检查
"""

from __future__ import annotations

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

from app.collector import collect
from app.models import FetchRequest, FetchResponse, ScheduleCheckRequest, ScheduleCheckResponse
from app.schedule_collector import check_schedule

app = FastAPI(
    title="采购公告采集与排期核查 API",
    description=(
        "抓取同站点原公告与更正公告，按发布时间合并，输出投标状态与有效截止时间；"
        "并支持沿分页目录抓取开标场次，检测同一会议室的时间交叠冲突。"
    ),
    version="1.1.0",
)


_DEMO_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>采购公告采集 / 排期核查 API 演示</title>
<style>
body { font-family: system-ui, "PingFang SC", "Microsoft YaHei", sans-serif; margin: 2rem; max-width: 960px; }
input { width: 32rem; max-width: 80vw; padding: .4rem; }
button { padding: .45rem 1.2rem; cursor: pointer; }
pre { background: #f4f4f6; padding: 1rem; border-radius: 8px; overflow:auto; max-height: 60vh; }
.hint { color: #666; font-size: .9rem; }
section { border: 1px solid #e3e6ea; border-radius: 10px; padding: 1rem 1.4rem; margin-bottom: 1.6rem; }
h2 { margin-top: .2rem; }
</style>
</head>
<body>
<h1>采购公告采集 / 排期核查 API 演示</h1>
<p class="hint">示例站点由 docker compose 中的 nginx 提供，公告列表默认 <code>http://localhost:8080/list.html</code>，
排期目录默认 <code>http://localhost:8080/schedule/catalog-1.html</code></p>

<section>
<h2>① 投标状态采集</h2>
<form id="f1">
  <p><label>列表页 URL <input id="list_url" value="http://localhost:8080/list.html"></label></p>
  <p><label>项目编号 <input id="project_code" value="GSB-2026-001"></label></p>
  <button type="submit">采集并合并</button>
</form>
<pre id="out1">响应将显示在这里……</pre>
</section>

<section>
<h2>② 开标排期核查（同室时间交叠）</h2>
<form id="f2">
  <p><label>开标日期 <input id="bid_date" type="date" value="2026-11-20"></label></p>
  <p><label>排期目录 URL <input id="catalog_url" value="http://localhost:8080/schedule/catalog-1.html"></label></p>
  <button type="submit">核查排期冲突</button>
</form>
<pre id="out2">响应将显示在这里……</pre>
</section>

<script>
async function postJson(formId, outId, endpoint, payload) {
  const out = document.getElementById(outId);
  out.textContent = '请求中……';
  try {
    const resp = await fetch(endpoint, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    out.textContent = JSON.stringify(await resp.json(), null, 2);
  } catch (err) {
    out.textContent = '请求失败：' + err;
  }
}
document.getElementById('f1').addEventListener('submit', (e) => {
  e.preventDefault();
  postJson('f1', 'out1', '/api/v1/collect', {
    list_url: document.getElementById('list_url').value,
    project_code: document.getElementById('project_code').value
  });
});
document.getElementById('f2').addEventListener('submit', (e) => {
  e.preventDefault();
  postJson('f2', 'out2', '/api/v1/schedule-check', {
    bid_date: document.getElementById('bid_date').value,
    catalog_url: document.getElementById('catalog_url').value
  });
});
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def demo_page() -> str:
    return _DEMO_PAGE


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/collect", response_model=FetchResponse, tags=["collect"])
async def collect_post(req: FetchRequest) -> FetchResponse:
    return await collect(req)


@app.get("/api/v1/collect", response_model=FetchResponse, tags=["collect"])
async def collect_get(
    list_url: str = Query(..., description="采购公告列表页 URL"),
    project_code: str = Query(..., description="项目编号"),
    timeout: float = Query(10.0, ge=1.0, le=60.0),
) -> FetchResponse:
    return await collect(FetchRequest(list_url=list_url, project_code=project_code, timeout=timeout))


@app.post("/api/v1/schedule-check", response_model=ScheduleCheckResponse, tags=["schedule"])
async def schedule_check_post(req: ScheduleCheckRequest) -> ScheduleCheckResponse:
    return await check_schedule(req)


@app.get("/api/v1/schedule-check", response_model=ScheduleCheckResponse, tags=["schedule"])
async def schedule_check_get(
    bid_date: str = Query(..., description="开标日期，格式 YYYY-MM-DD"),
    catalog_url: str = Query(..., description="排期目录首页 URL"),
    timeout: float = Query(10.0, ge=1.0, le=60.0),
) -> ScheduleCheckResponse:
    return await check_schedule(
        ScheduleCheckRequest(bid_date=bid_date, catalog_url=catalog_url, timeout=timeout)
    )
