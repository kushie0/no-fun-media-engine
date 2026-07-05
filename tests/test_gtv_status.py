"""Tests for nofun.gtv_status — the read-only gtv dashboard provider (Phase 3)."""
from __future__ import annotations

import nofun.gtv_status as gs


def test_assignments_parses_last_wins(tmp_path):
    log = tmp_path / 'gtv_heal.log'
    log.write_text(
        "2026-07-03 14:00:00  gtv heal v2 started\n"
        "2026-07-03 14:00:01  assigned 192.168.0.242:5555 -> /gtv1\n"
        "2026-07-03 14:05:00  assigned 192.168.0.174:5555 -> /gtv2\n"
        "2026-07-03 14:10:00  assigned 192.168.0.99:5555 -> /gtv1\n"   # gtv1 reassigned; last wins
        "2026-07-03 14:10:01  restarted (not receiving) 192.168.0.242:5555 -> rtsp://x/gtv1\n"
    )
    assert gs._assignments(log) == {'gtv1': '192.168.0.99', 'gtv2': '192.168.0.174'}


def test_assignments_missing_log_is_empty(tmp_path):
    assert gs._assignments(tmp_path / 'nope.log') == {}


def test_feeds_status_marks_receiving_by_assigned_stick(tmp_path, monkeypatch):
    log = tmp_path / 'gtv_heal.log'
    log.write_text("assigned 192.168.0.242:5555 -> /gtv1\nassigned 192.168.0.174:5555 -> /gtv2\n")
    monkeypatch.setattr(gs, '_live_feeds', lambda: {'gtv1', 'gtv2'})
    monkeypatch.setattr(gs, '_reader_ips', lambda port: {'192.168.0.242'})  # only gtv1's stick pulling

    rows = gs.gtv_feeds_status(port=8656, host='10.0.0.1', log=log)

    assert [r['feed'] for r in rows] == ['gtv1', 'gtv2']              # sorted, one per live feed
    by_feed = {r['feed']: r for r in rows}
    assert by_feed['gtv1']['receiving'] is True                       # assigned stick has a conn
    assert by_feed['gtv2']['receiving'] is False                      # assigned stick absent
    assert by_feed['gtv1']['url'] == 'rtsp://10.0.0.1:8656/gtv1'
    assert by_feed['gtv2']['stick'] == '192.168.0.174'


def test_feeds_status_empty_when_nothing_live(monkeypatch):
    monkeypatch.setattr(gs, '_live_feeds', lambda: set())
    monkeypatch.setattr(gs, '_reader_ips', lambda port: set())
    assert gs.gtv_feeds_status(host='x') == []


def test_get_local_ip_returns_str():
    ip = gs.get_local_ip()
    assert isinstance(ip, str) and ip                        # always resolves (falls back to 127.0.0.1)


def test_feeds_status_receiving_none_when_conns_unreadable(tmp_path, monkeypatch):
    log = tmp_path / 'gtv_heal.log'
    log.write_text("assigned 192.168.0.242:5555 -> /gtv1\n")
    monkeypatch.setattr(gs, '_live_feeds', lambda: {'gtv1'})
    monkeypatch.setattr(gs, '_reader_ips', lambda port: None)         # no admin → unreadable
    rows = gs.gtv_feeds_status(host='x', log=log)
    assert rows[0]['receiving'] is None
