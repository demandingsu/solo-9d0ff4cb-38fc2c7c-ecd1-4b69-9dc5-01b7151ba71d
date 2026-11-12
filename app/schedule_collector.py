"""排期核查采集编排。

流程：
1. 抓目录首页，读取发布批次；沿同站“下一页”链接分页抓取，每页批次须一致；
2. 汇总各页同站场次详情链接（去重），并发抓取详情页并解析；
   详情页批次须与目录批次一致，抓取/解析失败一律记为待核实；
3. 抓完后重新读取目录首页，确认批次未变化（复查失败也按待核实处理）；
4. 改期归并（旧版替换 / 双后继待核实）→ 按开标日期过滤有效场次 →
   同室时间交叠检测。

任何页面读取失败或批次变化都只会把结果标记为 ``pending``，
``conflict_free`` 恒为 ``false``，绝不返回“无冲突”的错误结论。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException

from app.models import (
    PendingSession,
    ScheduleCheckRequest,
    ScheduleCheckResponse,
    ScheduleCheckStatus,
    SessionRecord,
    SourceLink,
)
from app.parser import (
    ParseError,
    SessionDetailData,
    extract_catalog_batch,
    extract_next_page,
    extract_session_links,
    parse_session_detail,
)
from app.scheduler import PendingItem, filter_by_bid_date, find_conflicts, resolve_lineage

_MAX_CONCURRENCY = 5
_MAX_CATALOG_PAGES = 50
_USER_AGENT = "TenderScheduleChecker/1.0 (+local procurement schedule API)"


async def _fetch(client: httpx.AsyncClient, url: str) -> str:
    response = await client.get(url)
    response.raise_for_status()
    return response.text


def _to_record(data: SessionDetailData, url: str) -> SessionRecord:
    return SessionRecord(
        notice_no=data.notice_no,
        session_no=data.session_no,
        project_code=data.project_code,
        meeting_room=data.meeting_room,
        starts_at=data.starts_at,
        ends_at=data.ends_at,
        batch=data.batch,
        supersedes=data.supersedes,
        url=url,
    )


async def check_schedule(req: ScheduleCheckRequest) -> ScheduleCheckResponse:
    catalog_url = str(req.catalog_url)
    warnings: list[str] = []
    sources: list[SourceLink] = []
    pending: list[PendingSession] = []
    sessions: list[SessionRecord] = []
    failures: list[str] = []
    integrity_problem = False  # 任一页面失败/批次缺失或变化时置位，禁止报告“无冲突”

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(req.timeout),
        headers={"User-Agent": _USER_AGENT},
        follow_redirects=True,
    ) as client:
        # 1) 目录首页（失败直接 502，与公告采集行为一致）
        try:
            first_html = await _fetch(client, catalog_url)
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"排期目录首页抓取失败：{catalog_url}（{exc.__class__.__name__}）",
            ) from exc

        home_batch = extract_catalog_batch(first_html)
        if not home_batch:
            # 批次是后续一致性校验的基准，首页缺失批次无法保证数据同批 → 待核实
            integrity_problem = True
            warnings.append("目录首页未能解析发布批次标识，无法校验批次一致性")

        sources.append(SourceLink(role="catalog", url=catalog_url, title="排期目录首页"))

        # 2) 沿同站分页抓取，逐页校验批次并收集详情链接
        catalog_pages: list[tuple[str, str]] = [(catalog_url, first_html)]
        seen_pages: set[str] = {catalog_url}
        current_url, current_html = catalog_url, first_html

        for _ in range(_MAX_CATALOG_PAGES):
            next_url = extract_next_page(current_html, current_url)
            if not next_url or next_url in seen_pages:
                break
            seen_pages.add(next_url)
            try:
                next_html = await _fetch(client, next_url)
            except httpx.HTTPError as exc:
                integrity_problem = True
                failures.append(next_url)
                warnings.append(f"目录分页抓取失败：{next_url}（{exc.__class__.__name__}）")
                break
            page_batch = extract_catalog_batch(next_html)
            if home_batch and page_batch != home_batch:
                integrity_problem = True
                warnings.append(
                    f"目录分页批次与首页不一致：{next_url} 批次={page_batch!r}，首页批次={home_batch!r}"
                )
            sources.append(SourceLink(role="catalog", url=next_url, title="排期目录分页"))
            catalog_pages.append((next_url, next_html))
            current_url, current_html = next_url, next_html
        else:  # 达到页数上限仍存在下一页
            integrity_problem = True
            warnings.append(f"目录页数超过上限 {_MAX_CATALOG_PAGES} 页，未抓取的分页可能遗漏场次")

        link_seen: set[str] = set()
        detail_links: list[tuple[str, str, str]] = []
        for page_url, page_html in catalog_pages:
            for url, text, context in extract_session_links(page_html, page_url):
                if url not in link_seen:
                    link_seen.add(url)
                    detail_links.append((url, text, context))

        # 3) 并发抓取并解析场次详情页
        semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

        async def fetch_one(url: str) -> tuple[str, str]:
            async with semaphore:
                try:
                    return url, await _fetch(client, url)
                except httpx.HTTPError:
                    return url, ""

        results = await asyncio.gather(*(fetch_one(u) for u, _, _ in detail_links))

        for url, html in results:
            if not html:
                integrity_problem = True
                failures.append(url)
                warnings.append(f"场次详情页抓取失败：{url}")
                pending.append(
                    PendingSession(url=url, reason="详情页抓取失败（HTTP 错误或超时），场次信息待核实")
                )
                sources.append(SourceLink(role="failed", url=url, title="场次抓取失败-待核实"))
                continue
            try:
                data = parse_session_detail(html, url)
            except ParseError as exc:
                integrity_problem = True
                failures.append(url)
                warnings.append(f"场次详情页解析失败：{url}（{exc}）")
                pending.append(PendingSession(url=url, reason=f"详情页解析失败：{exc}"))
                sources.append(SourceLink(role="failed", url=url, title="场次解析失败-待核实"))
                continue

            if home_batch and data.batch != home_batch:
                integrity_problem = True
                failures.append(url)
                warnings.append(
                    f"场次详情页批次与目录不一致：{url} 批次={data.batch!r}，目录批次={home_batch!r}"
                )
                pending.append(
                    PendingSession(
                        notice_no=data.notice_no,
                        session_no=data.session_no,
                        project_code=data.project_code,
                        meeting_room=data.meeting_room,
                        starts_at=data.starts_at,
                        ends_at=data.ends_at,
                        batch=data.batch,
                        supersedes=data.supersedes,
                        url=url,
                        reason="详情页发布批次与目录批次不一致，不能与同批场次一起核查",
                    )
                )
                sources.append(SourceLink(role="failed", url=url, title="批次不一致-待核实"))
                continue

            sessions.append(_to_record(data, url))
            sources.append(SourceLink(role="session", url=url, title=data.title))

        # 4) 抓完后重读目录首页，确认批次未变
        try:
            recheck_html = await _fetch(client, catalog_url)
        except httpx.HTTPError as exc:
            integrity_problem = True
            failures.append(catalog_url)
            warnings.append(f"收尾复查目录首页失败：{catalog_url}（{exc.__class__.__name__}）")
        else:
            recheck_batch = extract_catalog_batch(recheck_html)
            if home_batch and recheck_batch != home_batch:
                integrity_problem = True
                warnings.append(
                    f"收尾复查发现目录批次已变化：复查批次={recheck_batch!r}，抓取时批次={home_batch!r}"
                )
            elif not recheck_batch:
                integrity_problem = True
                warnings.append("收尾复查时目录首页已无法读取发布批次，批次一致性无法确认")

    # 5) 改期归并：替换旧版、双后继/悬空引用待核实
    lineage = resolve_lineage(sessions)

    for item in lineage.pending:
        warnings.append(f"场次待核实：{item.record.notice_no}（{item.reason}）")
        pending.append(_pending_from_item(item))

    # 6) 按开标日期过滤有效场次，再做同室交叠检测
    dated_effective = filter_by_bid_date(lineage.effective, req.bid_date)
    dated_effective.sort(key=lambda s: (s.starts_at, s.meeting_room, s.notice_no))
    conflicts = find_conflicts(dated_effective)

    # 待核实场次同样按开标日期过滤（开始时间已知时），便于人工核实当天安排
    dated_pending = [
        p
        for p in pending
        if p.starts_at is None or p.starts_at.date() == req.bid_date
    ]
    dated_pending.sort(key=lambda p: (p.url or ""))

    is_pending = integrity_problem or bool(lineage.pending)
    status = ScheduleCheckStatus.PENDING if is_pending else ScheduleCheckStatus.OK

    return ScheduleCheckResponse(
        bid_date=req.bid_date,
        catalog_url=req.catalog_url,
        status=status,
        conflict_free=(status == ScheduleCheckStatus.OK and not conflicts),
        batch=home_batch,
        conflicts=conflicts,
        valid_sessions=dated_effective,
        pending_sessions=dated_pending,
        sources=sources,
        warnings=warnings,
        fetched_at=datetime.now(timezone.utc),
    )


def _pending_from_item(item: PendingItem) -> PendingSession:
    record = item.record
    return PendingSession(
        notice_no=record.notice_no,
        session_no=record.session_no,
        project_code=record.project_code,
        meeting_room=record.meeting_room,
        starts_at=record.starts_at,
        ends_at=record.ends_at,
        batch=record.batch,
        supersedes=record.supersedes,
        url=record.url,
        reason=item.reason,
    )
