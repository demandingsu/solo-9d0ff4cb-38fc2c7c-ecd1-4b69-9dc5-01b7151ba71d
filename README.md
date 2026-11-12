# 采购公告采集 API / 开标排期核查 API

服务提供两组能力：

1. **投标状态采集**：传入**采购公告列表页 URL** 与**项目编号**，自动抓取该页指向的同站点
   原公告与更正公告，从详情页提取发布时间、投标截止时间与取消采购信息，按发布时间合并后返回：
   当前是否可投标、有效截止时间、项目状态以及全部来源链接。
2. **开标排期核查**：传入**开标日期**与**排期目录 URL**，沿同站分页抓取开标场次详情，
   读取公告编号、场次编号、项目编号、会议室、带时区的起止时间、发布批次及改期引用，
   归并改期版本后返回同一会议室时间交叠的项目、交叠时段与来源链接。

## 业务规则

| 规则 | 实现位置 |
| --- | --- |
| 只采集列表页指向的**同站点**详情链接，外站链接、登录/导航链接自动忽略 | `app/parser.py::extract_detail_links` |
| 按**发布时间**从早到晚合并原公告与更正公告 | `app/merger.py::merge_notices` |
| 更正公告**未提及截止时间**时，沿用此前已生效的截止时间 | `app/merger.py` |
| 更正公告给出新截止时间（如延期）时，以新时间覆盖 | `app/merger.py` |
| 公告明确**取消采购** → 状态 `closed`、不可投标，不再返回有效截止时间（旧时间仅在公告明细中保留） | `app/merger.py` |
| **任一关联详情页**抓取/解析失败（404、超时、结构异常等）→ 状态 `pending` 待核实，`effective_deadline` 强制为 `null`，**绝不把旧时间当作最终期限** | `app/merger.py` + `app/collector.py` |
| 发布时间缺失、缺少原公告、全部公告均无截止时间等信息链不完整情况，同样标记 `pending` | `app/merger.py` |

### 项目状态（`status`）

- `open`：可投标，`effective_deadline` 为合并后的有效截止时间；
- `closed`：采购已取消，`can_bid=false`，无有效截止时间；
- `pending`：待核实，存在关联页面失败或信息不完整，`effective_deadline=null`；
- `not_found`：列表页未发现与该项目编号相关的公告。

## 开标排期核查（`/api/v1/schedule-check`）

采购代理人安排开标场地时，用该接口查出**同一会议室被不同项目同时占用**的场次。

### 业务规则

| 规则 | 实现位置 |
| --- | --- |
| 沿目录首页的同站“下一页”链接**分页抓取**，登录/外站/分页导航链接自动忽略 | `app/parser.py::extract_next_page / extract_session_links` |
| 详情页读取公告编号、场次编号、项目编号、会议室、**带时区起止时间**、发布批次、改期引用 | `app/parser.py::parse_session_detail` |
| 起止时间必须解析为**带时区时间**（ISO 8601 偏移量或 `UTC/GMT±HH:MM`），缺时区/缺必填字段按抓取失败处理 | `app/parser.py::parse_datetime_tz` |
| 目录各分页与全部详情页必须属于**同一发布批次**；抓完后重读首页确认批次未变化 | `app/schedule_collector.py` |
| 改期公告（`supersedes` 指向旧版公告编号）**只替换所引场次的旧版**，旧版不再视为有效场次 | `app/scheduler.py::resolve_lineage` |
| 同一旧版存在两条后继改期（或改期引用的旧版不存在）→ 相关场次标记**待核实**，不参与交叠判定 | `app/scheduler.py` |
| 有效场次按开标日期（场次开始时间的本地日历日期）过滤后，同会议室时间区间**交叠**才报冲突，首尾相接可连排不报 | `app/scheduler.py::find_conflicts` |
| 任一页面读取失败、批次缺失/不一致/复查变化 → 状态 `pending`，`conflict_free` 恒为 `false`，**绝不报告“无冲突”** | `app/schedule_collector.py` |
| 时间比较基于带时区时间的真实时刻（UTC），跨时区场次也能正确判定 | `app/scheduler.py` |

### 核查状态（`status`）

- `ok`：全部页面抓取成功、批次一致且复查通过，`conflicts` 与 `conflict_free` 可信；
- `pending`：存在页面失败、批次问题或改期信息链不完整（双后继/悬空引用），
  `conflict_free=false`，问题场次列入 `pending_sessions` 供人工核实；已确认的交叠仍会照常返回。

### 请求与响应

```bash
curl -X POST http://localhost:8000/api/v1/schedule-check \
  -H 'Content-Type: application/json' \
  -d '{"bid_date":"2026-11-20","catalog_url":"http://localhost:8080/schedule/catalog-1.html"}'

# 也支持 GET
curl "http://localhost:8000/api/v1/schedule-check?bid_date=2026-11-20&catalog_url=http://localhost:8080/schedule/catalog-1.html"
```

响应关键字段：

```json
{
  "bid_date": "2026-11-20",
  "status": "ok",
  "conflict_free": false,
  "batch": "PLAN-20261120-A",
  "conflicts": [
    {
      "meeting_room": "第一开标室",
      "project_codes": ["GSB-2026-001", "GSB-2026-004"],
      "session_nos": ["KC-20261120-01", "KC-20261120-02"],
      "overlap_starts_at": "2026-11-20T10:00:00+08:00",
      "overlap_ends_at": "2026-11-20T11:00:00+08:00",
      "sources": ["http://.../s-001.html", "http://.../s-002.html"]
    }
  ],
  "valid_sessions": [ { "notice_no": "...", "meeting_room": "...", "starts_at": "...+08:00" } ],
  "pending_sessions": [],
  "sources": [ { "role": "catalog", "url": "..." }, { "role": "session", "url": "..." } ],
  "warnings": []
}
```

### 示例排期站点（`sample-site/schedule/`，批次 `PLAN-20261120-A`）

目录分两页（`catalog-1.html` → `catalog-2.html`），共 10 条场次，覆盖：

| 场景 | 场次 | 预期 |
| --- | --- | --- |
| 同室时间交叠 | 第一开标室 `001`(09:00-11:00) 与 `002`(10:00-11:30) | 报冲突，交叠 10:00-11:00 |
| 首尾相接可连排 | 第一开标室 `002` 与 `003`(11:30-12:30) | 不报冲突 |
| 改期替换旧版 | 第二开标室 `004` 被 `005`（`supersedes=GG-...-004`）替换 | 有效场次只保留 `005`，旧版排除 |
| 不同会议室 | 第二/第三开标室、电子评标室各自安排 | 互不构成冲突 |
| 非开标日期 | `010` 安排在 2026-11-19 | 按 `bid_date=2026-11-20` 核查时被日期过滤 |

边界场景（双后继改期、悬空改期引用、详情批次不一致、详情页 404、复查批次变化）
由 `tests/fixtures/` 下的独立静态目录驱动，全部断言为 `pending` 且 `conflict_free=false`。

## 目录结构

```
.
├── app/
│   ├── main.py               # FastAPI 入口、路由、浏览器演示页
│   ├── collector.py          # 公告采集编排：列表发现 → 并发抓取 → 归属判定 → 合并
│   ├── schedule_collector.py # 排期核查编排：分页抓取 → 批次校验/复查 → 改期归并 → 交叠检测
│   ├── parser.py             # HTML 解析：同站链接发现、三层字段提取、取消/时区/批次识别
│   ├── merger.py             # 公告合并规则（时间继承、延期覆盖、取消、待核实）
│   ├── scheduler.py          # 排期规则（改期替换、双后继待核实、同室时间交叠）
│   └── models.py             # 请求/响应 Pydantic 模型
├── sample-site/              # 本地示例站点（nginx 静态托管）
│   ├── list.html             # 公告列表页（含外站/登录干扰链接）
│   ├── detail/               # 3 个项目的原公告/更正/取消详情页
│   └── schedule/             # 排期目录（2 页分页）+ 10 个开标场次详情页
├── tests/
│   ├── fixtures/             # 排期边界场景静态站点（双后继/悬空引用/批次异常/404/批次漂移）
│   ├── test_parser_merger.py # 公告解析/合并单测
│   ├── test_schedule_units.py# 时区解析、目录/场次解析、改期归并、交叠检测单测
│   ├── test_e2e.py           # 公告采集端到端测试
│   └── test_schedule_e2e.py  # 排期核查端到端测试（含全部待核实场景）
├── nginx/default.conf        # 示例站点 nginx 配置（缺失页面返回真实 404）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

启动后：

| 服务 | 地址 | 说明 |
| --- | --- | --- |
| 公告采集 / 排期核查 API | http://localhost:8000 | Swagger 文档：http://localhost:8000/docs |
| 浏览器演示页 | http://localhost:8000/ | 填表即可发起采集或排期核查 |
| 示例公告站点 | http://localhost:8080/list.html | nginx 托管的静态采购公告 |
| 示例排期目录 | http://localhost:8080/schedule/catalog-1.html | nginx 托管的分页开标排期 |

停止：`docker compose down`。

## API 用法

### POST（推荐）

```bash
curl -X POST http://localhost:8000/api/v1/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url":"http://localhost:8080/list.html","project_code":"GSB-2026-001"}'
```

### GET（便于浏览器快速验证）

```bash
curl "http://localhost:8000/api/v1/collect?list_url=http://localhost:8080/list.html&project_code=GSB-2026-001"
```

响应关键字段：

```json
{
  "project_code": "GSB-2026-001",
  "status": "open",
  "can_bid": true,
  "effective_deadline": "2026-11-15T14:00:00",
  "cancelled": false,
  "notices": [ { "kind": "original", "published_at": "...", "deadline": "..." } ],
  "sources": [ { "role": "list", "url": "..." }, { "role": "correction", "url": "..." } ],
  "warnings": []
}
```

## 示例站点内置的三个演示项目

| 项目编号 | 场景 | 预期结果 |
| --- | --- | --- |
| `GSB-2026-001` | 原公告 → 澄清更正（**未提截止时间，沿用原时间**）→ 延期更正（**新时间覆盖**） | `open`，有效截止时间 `2026-11-15 14:00` |
| `GSB-2026-002` | 原公告 → 取消公告（预算收回） | `closed`，不可投标，无有效截止时间 |
| `GSB-2026-003` | 原公告 → 更正页**已下线（404）** | `pending` 待核实，`effective_deadline=null`，来源中列出失败链接 |

另外列表页还包含：外站链接、登录导航链接、其他项目公告（`GSB-2026-099`）、
无关项目的坏链接（`GSB-2025-888`），用于验证同站过滤、项目归属判定与失败隔离。

## 详情页解析策略（三层回退）

1. **结构化标记**：`data-field="published-at|deadline|cancelled|..."` 元素或
   `<meta property="article:published_time">`；
2. **标签匹配**：`发布时间 / 投标截止时间 / 响应文件递交截止时间 / 取消原因` 等
   中文标签，兼容 `th/td`、`dt/dd`、`li/p` 等布局，支持
   `2026-11-10 09:30`、`2026年11月15日 14时00分` 等常见写法；
3. **正文正则兜底**：在含“截止/递交/提交/受理”等时限语义的块级元素中识别日期，
   并排除“发布时间”句、资质业绩说明中的历史日期（如“近三年（2023年1月1日起）”）。

取消采购识别支持 `data-field="cancelled"` 标记、取消原因标签以及
“本项目取消采购 / 终止本次招标”等显式句式。

> 接入真实采购平台时，若页面结构不同，优先在详情页模板中补 `data-field` 标记，
> 或在 `app/parser.py` 顶部的标签/关键词元组中登记该平台的字段叫法。

## 本地开发与测试（不使用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 终端 A：托管示例公告页
cd sample-site && python -m http.server 8080
# 终端 B：启动 API
uvicorn app.main:app --reload --port 8000

# 测试（自带静态服务器，无需外部依赖）
pytest -q
```

## 设计说明

- 列表页抓取失败直接返回 `502`；详情页失败不影响整体响应，而是按业务规则沉淀为
  `pending` + 失败来源链接，便于投标专员人工核实。
- 关联归属判定结合详情页项目编号与列表行上下文（含编号列），
  即使详情页下线（只能看到列表行）也能正确判定失败是否与目标项目相关；
  无关项目的坏链接不会污染查询结果。
- 详情页并发抓取（上限 5），统一超时、自定义 UA、跟随重定向。
- 排期核查把“能不能确认无冲突”作为一等公民：页面失败、批次不一致/复查变化、
  双后继改期等任何不确定因素都只会产生 `pending`，`conflict_free` 永远不会在
  数据不可信时返回 `true`，避免采购代理人据错误结论安排场地。
- 排期目录最多跟随 50 个分页，超出即标记 `pending`，防止异常站点导致无限抓取。
- 排期核查与公告采集共用同站过滤、三层解析回退与并发抓取约定，接入真实平台时
  优先在页面模板补 `data-field` 标记（`notice-no/session-no/meeting-room/starts-at/...`），
  或在 `app/parser.py` 顶部标签元组中登记平台字段叫法。
