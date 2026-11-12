"""排期业务逻辑：改期归并、待核实判定与同室时间交叠检测。

规则（对应需求）：
* 改期公告（``supersedes`` 指向旧版公告编号）只替换**所引场次的旧版**，
  原版本不再视为有效场次；
* 同一旧版存在两条后继改期时，无法判断哪条生效，相关场次全部标记“待核实”，
  不参与交叠判定；
* 改期引用了目录中不存在的旧版（悬空引用），同样标记“待核实”；
* 有效场次中，同一会议室时间区间交叠（首尾相接不算交叠）的一对项目构成冲突，
  并给出交叠时段与双方来源链接；
* 全部判定基于带时区时间，比较前统一换算到 UTC。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.models import ScheduleConflict, SessionRecord


@dataclass
class PendingItem:
    """待核实场次（解析成功但信息链不完整）。"""

    record: SessionRecord
    reason: str


@dataclass
class LineageResult:
    """改期归并结果。"""

    effective: list[SessionRecord] = field(default_factory=list)
    pending: list[PendingItem] = field(default_factory=list)


def _starts_on(session: SessionRecord, bid_date: date) -> bool:
    """场次开始时间（按其自身时区的本地日历日期）是否落在开标日期当天。"""
    return session.starts_at.date() == bid_date


def filter_by_bid_date(sessions: list[SessionRecord], bid_date: date) -> list[SessionRecord]:
    return [s for s in sessions if _starts_on(s, bid_date)]


def resolve_lineage(sessions: list[SessionRecord]) -> LineageResult:
    """按改期引用归并场次，输出有效版本与待核实场次。"""
    by_notice: dict[str, SessionRecord] = {}
    duplicate_notices: set[str] = set()
    for session in sessions:
        if session.notice_no in by_notice:
            duplicate_notices.add(session.notice_no)
        else:
            by_notice[session.notice_no] = session

    # 后继映射：旧版公告编号 -> 后继改期列表
    successors: dict[str, list[SessionRecord]] = {}
    for session in sessions:
        if session.supersedes:
            successors.setdefault(session.supersedes, []).append(session)

    # 同一旧版出现两条（含）以上后继 → 旧版与全部后继均待核实
    disputed_notices: set[str] = set()
    pending: list[PendingItem] = []
    for old_notice, chain in successors.items():
        if len(chain) > 1:
            disputed_notices.add(old_notice)
            disputed_notices.update(s.notice_no for s in chain)

    # 悬空引用：改期指向目录中不存在的旧版
    dangling_sessions = [
        s
        for s in sessions
        if s.supersedes and s.supersedes not in by_notice and s.notice_no not in disputed_notices
    ]

    for notice_no in sorted(duplicate_notices):
        for session in sessions:
            if session.notice_no == notice_no:
                pending.append(PendingItem(session, f"公告编号重复出现：{notice_no}"))

    for session in sorted(sessions, key=lambda s: s.notice_no):
        if session.notice_no in disputed_notices:
            pending.append(
                PendingItem(
                    session,
                    f"同一旧版（{session.supersedes or session.notice_no}）存在多条后继改期，无法确定生效版本",
                )
            )

    for session in dangling_sessions:
        pending.append(
            PendingItem(
                session,
                f"改期引用的旧版公告不存在：{session.supersedes}",
            )
        )

    pending_notices = {p.record.notice_no for p in pending}

    # 有效版本：未被待核实、且未被任一后继改期替换的场次
    superseded_notices = {
        old
        for old, chain in successors.items()
        if len(chain) == 1 and old not in disputed_notices
    }
    effective = [
        session
        for session in sessions
        if session.notice_no not in pending_notices
        and session.notice_no not in superseded_notices
    ]
    return LineageResult(effective=effective, pending=pending)


def find_conflicts(sessions: list[SessionRecord]) -> list[ScheduleConflict]:
    """在同会议室有效场次中找出时间交叠的场次对。

    首尾相接（一场结束时间等于另一场开始时间）视为可连排，不算交叠。
    """
    grouped: dict[str, list[SessionRecord]] = {}
    for session in sessions:
        grouped.setdefault(session.meeting_room, []).append(session)

    conflicts: list[ScheduleConflict] = []
    for room in sorted(grouped):
        room_sessions = sorted(grouped[room], key=lambda s: (s.starts_at, s.notice_no))
        for i, left in enumerate(room_sessions):
            for right in room_sessions[i + 1 :]:
                if right.starts_at >= left.ends_at:
                    break  # 已按开始时间排序，之后的场次更不可能与 left 交叠
                overlap_start = max(left.starts_at, right.starts_at)
                overlap_end = min(left.ends_at, right.ends_at)
                if overlap_end > overlap_start:
                    pair = sorted((left, right), key=lambda s: s.notice_no)
                    conflicts.append(
                        ScheduleConflict(
                            meeting_room=room,
                            project_codes=[pair[0].project_code, pair[1].project_code],
                            session_nos=[pair[0].session_no, pair[1].session_no],
                            overlap_starts_at=overlap_start,
                            overlap_ends_at=overlap_end,
                            sources=[pair[0].url, pair[1].url],
                        )
                    )
    conflicts.sort(key=lambda c: (c.meeting_room, c.overlap_starts_at))
    return conflicts
