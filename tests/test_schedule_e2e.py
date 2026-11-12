"""排期核查端到端测试：本地静态目录 + FastAPI 全链路（离线可跑）。"""

from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample-site"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
BID_DATE = "2026-11-20"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class _BatchDriftHandler(_QuietHandler):
    """目录首页首次返回批次 A，收尾复查时返回批次 B，模拟抓取期间批次变化。"""

    def do_GET(self):  # noqa: N802 - http.server 命名约定
        if self.path.rstrip("/").endswith("/catalog.html"):
            if not hasattr(self.server, "catalog_hits"):
                self.server.catalog_hits = 0
            self.server.catalog_hits += 1
            if self.server.catalog_hits >= 2:
                self.path = "/catalog-changed.html"
        super().do_GET()


def _start_static(directory: Path, handler_cls=_QuietHandler):
    handler = functools.partial(handler_cls, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def sample_server():
    server, base = _start_static(SAMPLE_DIR)
    yield base
    server.shutdown()
    server.server_close()


@pytest.fixture
def fixture_server_factory():
    servers = []

    def _make(name: str, handler_cls=_QuietHandler):
        server, base = _start_static(FIXTURE_DIR / name, handler_cls)
        servers.append(server)
        return base

    yield _make
    for server in servers:
        server.shutdown()
        server.server_close()


def _check(client, base, path="/catalog.html", bid_date=BID_DATE):
    resp = client.post(
        "/api/v1/schedule-check",
        json={
            "bid_date": bid_date,
            "catalog_url": f"{base}{path}",
            "timeout": 5,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# 示例站点主链路
# --------------------------------------------------------------------------- #
def test_sample_site_detects_room_one_overlap_and_filters_by_date(client, sample_server):
    data = _check(client, sample_server, path="/schedule/catalog-1.html")

    assert data["status"] == "ok"
    assert data["conflict_free"] is False
    assert data["batch"] == "PLAN-20261120-A"

    # 10 条场次：旧版 004 被 005 替换，010 属于 11-19 被日期过滤 → 8 条有效
    notices = {s["notice_no"] for s in data["valid_sessions"]}
    assert notices == {
        "GG-20261120-001",
        "GG-20261120-002",
        "GG-20261120-003",
        "GG-20261120-005",
        "GG-20261120-006",
        "GG-20261120-007",
        "GG-20261120-008",
        "GG-20261120-009",
    }
    assert "GG-20261120-004" not in notices  # 旧版已被改期替换
    assert data["pending_sessions"] == []

    assert len(data["conflicts"]) == 1
    conflict = data["conflicts"][0]
    assert conflict["meeting_room"] == "第一开标室"
    assert conflict["project_codes"] == ["GSB-2026-001", "GSB-2026-004"]
    assert conflict["overlap_starts_at"].startswith("2026-11-20T10:00:00+08:00")
    assert conflict["overlap_ends_at"].startswith("2026-11-20T11:00:00+08:00")
    assert all(u.endswith(("s-001.html", "s-002.html")) for u in conflict["sources"])

    # 起止时间全部带时区
    for session in data["valid_sessions"]:
        assert session["starts_at"].endswith(("+08:00", "Z")) or "+" in session["starts_at"]

    # 来源覆盖两个目录分页与全部有效场次
    roles = [s["role"] for s in data["sources"]]
    assert roles.count("catalog") == 2
    assert "session" in roles
    assert "failed" not in roles


def test_touching_sessions_are_not_conflicts(client, sample_server):
    data = _check(client, sample_server, path="/schedule/catalog-1.html")
    # 第一开标室 002（10:00-11:30）与 003（11:30-12:30）首尾相接，不得报冲突
    rooms = {c["meeting_room"] for c in data["conflicts"]}
    assert rooms == {"第一开标室"}
    assert all(c["session_nos"] != ["KC-20261120-02", "KC-20261120-03"] for c in data["conflicts"])


def test_rescheduled_old_version_excluded_but_new_kept(client, sample_server):
    data = _check(client, sample_server, path="/schedule/catalog-1.html")
    by_notice = {s["notice_no"]: s for s in data["valid_sessions"]}
    assert "GG-20261120-004" not in by_notice
    new = by_notice["GG-20261120-005"]
    assert new["supersedes"] == "GG-20261120-004"
    assert new["starts_at"].startswith("2026-11-20T14:00:00+08:00")


def test_other_date_returns_conflict_free(client, sample_server):
    data = _check(client, sample_server, path="/schedule/catalog-1.html", bid_date="2026-11-19")
    assert data["status"] == "ok"
    assert data["conflict_free"] is True
    assert data["conflicts"] == []
    assert [s["notice_no"] for s in data["valid_sessions"]] == ["GG-20261119-010"]


def test_get_endpoint_matches_post(client, sample_server):
    resp = client.get(
        "/api/v1/schedule-check",
        params={
            "bid_date": BID_DATE,
            "catalog_url": f"{sample_server}/schedule/catalog-1.html",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert len(body["conflicts"]) == 1


def test_external_and_login_links_ignored_in_schedule(client, sample_server):
    data = _check(client, sample_server, path="/schedule/catalog-1.html")
    urls = [s["url"] for s in data["sources"]]
    assert not any("example.gov" in u for u in urls)
    assert not any("login" in u for u in urls)


# --------------------------------------------------------------------------- #
# 待核实场景：任何异常都不得报告“无冲突”
# --------------------------------------------------------------------------- #
def test_double_successor_marks_pending(client, fixture_server_factory):
    base = fixture_server_factory("schedule-double")
    data = _check(client, base)

    assert data["status"] == "pending"
    assert data["conflict_free"] is False  # 关键：待核实时绝不放行
    pending_notices = {p["notice_no"] for p in data["pending_sessions"]}
    assert pending_notices == {"GG-T-001", "GG-T-002", "GG-T-003"}
    assert all("多条后继" in p["reason"] for p in data["pending_sessions"])
    # 未卷入双后继的独立场次仍作为有效场次输出
    assert {s["notice_no"] for s in data["valid_sessions"]} == {"GG-T-004"}
    assert any("多条后继" in w for w in data["warnings"])


def test_dangling_reschedule_reference_marks_pending(client, fixture_server_factory):
    base = fixture_server_factory("schedule-dangling")
    data = _check(client, base)

    assert data["status"] == "pending"
    assert data["conflict_free"] is False
    pending = data["pending_sessions"]
    assert len(pending) == 1 and pending[0]["notice_no"] == "GG-T-101"
    assert "GG-T-999" in pending[0]["reason"]
    assert {s["notice_no"] for s in data["valid_sessions"]} == {"GG-T-102"}


def test_detail_batch_mismatch_marks_pending(client, fixture_server_factory):
    base = fixture_server_factory("schedule-bad-batch")
    data = _check(client, base)

    assert data["status"] == "pending"
    assert data["conflict_free"] is False
    pending_notices = {p["notice_no"] for p in data["pending_sessions"]}
    assert pending_notices == {"GG-T-202"}
    assert any("批次" in p["reason"] for p in data["pending_sessions"])
    # 批次一致的场次仍然有效
    assert {s["notice_no"] for s in data["valid_sessions"]} == {"GG-T-201"}
    assert any(s["role"] == "failed" for s in data["sources"])


def test_broken_detail_never_reports_conflict_free(client, fixture_server_factory):
    base = fixture_server_factory("schedule-broken")
    data = _check(client, base)

    assert data["status"] == "pending"
    assert data["conflict_free"] is False
    assert any("抓取失败" in w for w in data["warnings"])
    failed = [s for s in data["sources"] if s["role"] == "failed"]
    assert any("missing.html" in s["url"] for s in failed)
    # 可访问的场次仍被解析输出，供人工一并核查
    assert {s["notice_no"] for s in data["valid_sessions"]} == {"GG-T-301"}
    assert data["pending_sessions"][0]["notice_no"] is None
    assert "missing.html" in data["pending_sessions"][0]["url"]


def test_batch_drift_on_recheck_marks_pending(client, fixture_server_factory):
    drift_root = FIXTURE_DIR / "schedule-drift"
    base_factory = fixture_server_factory
    base = base_factory("schedule-drift", handler_cls=_BatchDriftHandler)
    data = _check(client, base)

    assert data["status"] == "pending"
    assert data["conflict_free"] is False
    assert any("批次已变化" in w for w in data["warnings"])


def test_unreachable_catalog_returns_502(client):
    resp = client.post(
        "/api/v1/schedule-check",
        json={
            "bid_date": BID_DATE,
            "catalog_url": "http://127.0.0.1:1/never-listens.html",
            "timeout": 2,
        },
    )
    assert resp.status_code == 502
    assert "目录首页抓取失败" in resp.json()["detail"]
