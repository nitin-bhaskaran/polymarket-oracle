"""Tests for Betfair paper-loop configuration."""

from core.betfair_main import paper_scan_interval


def test_paper_scan_interval_uses_paper_block():
    config = {
        "scanner": {"scan_interval": 300},
        "paper": {"scan_interval": 2700},
    }
    assert paper_scan_interval(config) == 2700


def test_paper_scan_interval_keeps_legacy_fallback():
    assert paper_scan_interval({"scanner": {"scan_interval": 600}}) == 600
