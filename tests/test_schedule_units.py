"""排期解析器与归并/冲突逻辑的单元测试（不依赖网络）。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.models import SessionRecord
from app.parser import (
    ParseError,
    extract_catalog_batch,
    extract_next_page,
    extract_session_links,
    parse_datetime_tz,
    parse_session_detail,
)
from app.scheduler import filter_by_bid_date, find_conflicts, resolve_lineage

_CST = timezone(timedelta(hours=8))
_BID_DAY = date(2026, 11, 20)


# --------------------------------------------------------------------------- #
# parse_datetime_tz
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "2026-11-20T09:00:00+08:00",
            datetime(2026, 11, 20, 9, 0, tzinfo=_CST),
        ),
        (
            "2026-11-20T01:00:00Z",
            datetime(2026, 11, 20, 1, 0, tzinfo=timezone.utc),
        ),
        (
            "2026-11-20 09:00:00+0800",
            datetime(2026, 11, 20, 9, 0, tzinfo=_CST),
        ),
        (
            "2026年11月20日 09时00分 UTC+08:00",
            datetime(2026, 11, 20, 9, 0, tzinfo=_CST),
        ),
        (
            "2026-11-20T09:00 GMT-05:00",
            datetime(2026, 11, 20, 9, 0, tzinfo=timezone(timedelta(hours=-5))),
        ),
        ("2026-11-20T09:00:00", None),  # 无时区 → 拒绝，不退化为 naive
        ("2026年11月20日 09时00分", None),
        ("不含时间", None),
    ],
)
def test_parse_datetime_tz(text, expected):
    assert parse_datetime_tz(text) == expected


def test_parse_datetime_tz_utc_alias():
    assert parse_datetime_tz("2026-11-20T09:00:00 UTC") == datetime(
        2026, 11, 20, 9, 0, tzinfo=timezone.utc
    )


# --------------------------------------------------------------------------- #
# 目录页解析
# --------------------------------------------------------------------------- #
CATALOG_HTML = """
<html><body>
<p>发布批次：<span data-field="batch" data-value="PLAN-X-1">PLAN-X-1</span></p>
<a href="/login.html">登录</a>
<a href="https://example.gov/x.html">外站</a>
<a href="/schedule/detail/a.html">场次A</a>
<a href="/schedule/detail/b.html">场次B</a>
<a href="/schedule/detail/a.html">场次A重复</a>
<a href="/schedule/catalog-2.html" rel="next">下一页</a>
</body></html>
"""


def test_extract_catalog_batch():
    assert extract_catalog_batch(CATALOG_HTML) == "PLAN-X-1"
    assert extract_catalog_batch("<html><body>无批次</body></html>") is None


def test_extract_next_page_same_site_only():
    url = "http://localhost:8080/schedule/catalog-1.html"
    assert extract_next_page(CATALOG_HTML, url) == (
        "http://localhost:8080/schedule/catalog-2.html"
    )
    assert extract_next_page("<html><body>没有分页</body></html>", url) is None


def test_extract_session_links_excludes_nav_and_pagination():
    url = "http://localhost:8080/schedule/catalog-1.html"
    links = extract_session_links(CATALOG_HTML, url)
    urls = [u for u, _, _ in links]
    assert urls == [
        "http://localhost:8080/schedule/detail/a.html",
        "http://localhost:8080/schedule/detail/b.html",
    ]


def test_extract_session_links_excludes_previous_page():
    html = (
        '<html><body><a href="/schedule/detail/a.html">场次A</a>'
        '<a href="/schedule/catalog-1.html">上一页</a></body></html>'
    )
    url = "http://localhost:8080/schedule/catalog-2.html"
    links = extract_session_links(html, url)
    assert [u for u, _, _ in links] == [
        "http://localhost:8080/schedule/detail/a.html"
    ]


# --------------------------------------------------------------------------- #
# 场次详情页解析
# --------------------------------------------------------------------------- #
SESSION_HTML = """
<html><head><title>开标场次公告 GG-1</title></head><body>
<h1>开标场次公告 GG-1</h1>
<table>
  <tr><th>公告编号</th><td data-field="notice-no" data-value="GG-1">GG-1</td></tr>
  <tr><th>场次编号</th><td data-field="session-no" data-value="KC-1">KC-1</td></tr>
  <tr><th>项目编号</th><td data-field="project-code" data-value="P-1">P-1</td></tr>
  <tr><th>会议室</th><td data-field="meeting-room" data-value="第一开标室">第一开标室</td></tr>
  <tr><th>开标开始时间</th><td data-field="starts-at" data-value="2026-11-20T09:00:00+08:00">2026年11月20日 09时00分 UTC+08:00</td></tr>
  <tr><th>开标结束时间</th><td data-field="ends-at" data-value="2026-11-20T11:00:00+08:00">2026年11月20日 11时00分 UTC+08:00</td></tr>
  <tr><th>发布批次</th><td data-field="batch" data-value="PLAN-X-1">PLAN-X-1</td></tr>
  <tr><th>改期引用</th><td data-field="supersedes" data-value="GG-0">GG-0</td></tr>
</table>
</body></html>
"""


def test_parse_session_detail_full_fields():
    d = parse_session_detail(SESSION_HTML, "http://x/schedule/detail/a.html")
    assert d.notice_no == "GG-1"
    assert d.session_no == "KC-1"
    assert d.project_code == "P-1"
    assert d.meeting_room == "第一开标室"
    assert d.starts_at == datetime(2026, 11, 20, 9, 0, tzinfo=_CST)
    assert d.ends_at == datetime(2026, 11, 20, 11, 0, tzinfo=_CST)
    assert d.batch == "PLAN-X-1"
    assert d.supersedes == "GG-0"
    assert d.starts_at.tzinfo is not None  # 起止时间必须带时区


def test_parse_session_detail_naive_time_rejected():
    html = SESSION_HTML.replace("2026-11-20T09:00:00+08:00", "2026-11-20T09:00:00")
    with pytest.raises(ParseError):
        parse_session_detail(html, "http://x/b.html")


def test_parse_session_detail_missing_field_rejected():
    html = SESSION_HTML.replace(
        '<tr><th>会议室</th><td data-field="meeting-room" data-value="第一开标室">第一开标室</td></tr>',
        "",
    )
    with pytest.raises(ParseError):
        parse_session_detail(html, "http://x/c.html")


def test_parse_session_detail_end_before_start_rejected():
    html = SESSION_HTML.replace("2026-11-20T11:00:00+08:00", "2026-11-20T08:00:00+08:00")
    with pytest.raises(ParseError):
        parse_session_detail(html, "http://x/d.html")


# --------------------------------------------------------------------------- #
# 改期归并
# --------------------------------------------------------------------------- #
def _session(
    notice_no,
    room,
    start_hour,
    end_hour,
    end_minute=0,
    *,
    start_minute=0,
    session_no=None,
    project=None,
    supersedes=None,
    day=_BID_DAY,
):
    return SessionRecord(
        notice_no=notice_no,
        session_no=session_no or f"KC-{notice_no}",
        project_code=project or f"P-{notice_no}",
        meeting_room=room,
        starts_at=datetime(day.year, day.month, day.day, start_hour, start_minute, tzinfo=_CST),
        ends_at=datetime(day.year, day.month, day.day, end_hour, end_minute, tzinfo=_CST),
        batch="PLAN-X-1",
        supersedes=supersedes,
        url=f"http://x/{notice_no}.html",
    )


def test_reschedule_replaces_only_referenced_old_version():
    sessions = [
        _session("GG-OLD", "第二开标室", 9, 10),
        _session("GG-NEW", "第二开标室", 14, 15, supersedes="GG-OLD"),
        _session("GG-OTHER", "第二开标室", 9, 12, project="P-OTHER"),
    ]
    result = resolve_lineage(sessions)
    effective_notices = {s.notice_no for s in result.effective}
    assert effective_notices == {"GG-NEW", "GG-OTHER"}
    assert result.pending == []


def test_two_successors_for_same_old_version_all_pending():
    sessions = [
        _session("GG-OLD", "第一开标室", 9, 10),
        _session("GG-NEW1", "第一开标室", 11, 12, supersedes="GG-OLD"),
        _session("GG-NEW2", "第一开标室", 14, 15, supersedes="GG-OLD"),
        _session("GG-SAFE", "第二开标室", 9, 10),
    ]
    result = resolve_lineage(sessions)
    pending_notices = {p.record.notice_no for p in result.pending}
    assert pending_notices == {"GG-OLD", "GG-NEW1", "GG-NEW2"}
    assert {s.notice_no for s in result.effective} == {"GG-SAFE"}
    assert all("多条后继" in p.reason for p in result.pending)


def test_chained_reschedule_keeps_only_newest_version():
    # A -> B -> C：B 替换 A，C 再替换 B，最终只有最新版 C 有效
    sessions = [
        _session("GG-A", "第一开标室", 9, 10),
        _session("GG-B", "第一开标室", 11, 12, supersedes="GG-A"),
        _session("GG-C", "第一开标室", 14, 15, supersedes="GG-B"),
    ]
    result = resolve_lineage(sessions)
    assert result.pending == []
    assert [s.notice_no for s in result.effective] == ["GG-C"]


def test_dangling_reschedule_reference_is_pending():
    sessions = [
        _session("GG-NEW", "第一开标室", 9, 10, supersedes="GG-MISSING"),
        _session("GG-SAFE", "第二开标室", 9, 10),
    ]
    result = resolve_lineage(sessions)
    assert {p.record.notice_no for p in result.pending} == {"GG-NEW"}
    assert "GG-MISSING" in result.pending[0].reason
    assert {s.notice_no for s in result.effective} == {"GG-SAFE"}


def test_duplicate_notice_number_is_pending():
    sessions = [
        _session("GG-DUP", "第一开标室", 9, 10),
        _session("GG-DUP", "第一开标室", 9, 10, session_no="KC-DUP2", project="P-2"),
    ]
    result = resolve_lineage(sessions)
    assert {p.record.notice_no for p in result.pending} == {"GG-DUP"}
    assert result.effective == []


# --------------------------------------------------------------------------- #
# 日期过滤与交叠检测
# --------------------------------------------------------------------------- #
def test_filter_by_bid_date_uses_local_date_of_aware_time():
    other_day = date(2026, 11, 19)
    sessions = [
        _session("GG-1", "第一开标室", 9, 10),
        _session("GG-2", "第一开标室", 9, 10, day=other_day),
    ]
    kept = filter_by_bid_date(sessions, _BID_DAY)
    assert [s.notice_no for s in kept] == ["GG-1"]


def test_find_conflicts_detects_overlap_and_ignores_touch():
    sessions = [
        _session("GG-1", "第一开标室", 9, 11, project="P-1"),
        _session("GG-2", "第一开标室", 10, 11, 30, project="P-2"),
        _session("GG-3", "第一开标室", 11, 12, start_minute=30, project="P-3"),  # 11:30 开始，与 GG-2 首尾相接
        _session("GG-4", "第二开标室", 10, 11, project="P-4"),  # 不同会议室
    ]
    conflicts = find_conflicts(sessions)
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.meeting_room == "第一开标室"
    assert conflict.project_codes == ["P-1", "P-2"]
    assert conflict.overlap_starts_at == datetime(2026, 11, 20, 10, 0, tzinfo=_CST)
    assert conflict.overlap_ends_at == datetime(2026, 11, 20, 11, 0, tzinfo=_CST)
    assert [str(u) for u in conflict.sources] == [
        "http://x/GG-1.html",
        "http://x/GG-2.html",
    ]


def test_find_conflicts_compares_instant_across_timezones():
    # 同一时刻的不同时区表达：09:00+08:00 与 02:00+01:00 开始时间相同，
    # 跨时区场次仍按真实时刻判定交叠
    east = SessionRecord(
        notice_no="GG-E",
        session_no="KC-E",
        project_code="P-E",
        meeting_room="线上开标厅",
        starts_at=datetime(2026, 11, 20, 9, 0, tzinfo=_CST),
        ends_at=datetime(2026, 11, 20, 10, 0, tzinfo=_CST),
        batch="PLAN-X-1",
        supersedes=None,
        url="http://x/e.html",
    )
    west = SessionRecord(
        notice_no="GG-W",
        session_no="KC-W",
        project_code="P-W",
        meeting_room="线上开标厅",
        starts_at=datetime(2026, 11, 20, 2, 0, tzinfo=timezone(timedelta(hours=1))),
        ends_at=datetime(2026, 11, 20, 3, 30, tzinfo=timezone(timedelta(hours=1))),
        batch="PLAN-X-1",
        supersedes=None,
        url="http://x/w.html",
    )
    conflicts = find_conflicts([east, west])
    assert len(conflicts) == 1
    assert conflicts[0].project_codes == ["P-E", "P-W"]


def test_find_conflicts_empty_when_no_overlap():
    sessions = [
        _session("GG-1", "第一开标室", 9, 10),
        _session("GG-2", "第一开标室", 10, 11),
    ]
    assert find_conflicts(sessions) == []
