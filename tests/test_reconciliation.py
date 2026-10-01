"""瓶组气量班结对账。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from hydrorefuel.app import build_platform
from hydrorefuel.config import ConfigError, StationConfig, config_to_mapping, default_config
from hydrorefuel.console.server import ConsoleServer
from hydrorefuel.recon.ledger import BLOCKED, CHARGE, ISSUE, ReconciliationError


def _post(url: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def test_settlement_is_fixed_to_shift_and_config_round_trips():
    cfg = default_config()
    assert cfg.reconciliation.settlement == "shift"
    assert StationConfig.from_mapping(config_to_mapping(cfg)).reconciliation == cfg.reconciliation
    raw = config_to_mapping(cfg)
    raw["limits"]["reconciliation"]["settlement"] = "day"
    with pytest.raises(ConfigError):
        StationConfig.from_mapping(raw)


def test_charge_and_issue_create_shift_receipt_issue_balance(platform):
    platform.record_buffer_movement(CHARGE, 20.0, pressure_after_mpa=42.0)
    platform.record_buffer_movement(ISSUE, 5.0)
    report = platform.reconciliation_report(include_actual=True)
    assert report["settlement"] == "shift"
    assert report["received_kg"] == 20.0
    assert report["issued_kg"] == 5.0
    assert report["book_stock_kg"] == 15.0
    movements = platform.reconciliation_movements()["movements"]
    assert [item["direction"] for item in movements] == [CHARGE, ISSUE]
    assert movements[-1]["balance_kg"] == 15.0
    assert platform.verify_reconciliation()["consistent"] is True


def test_normal_pressure_band_does_not_become_loss(platform):
    platform.record_buffer_movement(CHARGE, 20.0, pressure_after_mpa=41.0)
    closed = platform.close_reconciliation_shift(actual_pressure_mpa=41.2)
    assert closed["state"] == "closed"
    assert closed["classification"] == "normal_pressure_variation"
    assert closed["adjustment_kg"] == 0
    assert closed["ending_stock_kg"] == 20.0


def test_large_difference_holds_whole_shift_for_team_a(platform):
    platform.record_buffer_movement(CHARGE, 20.0, pressure_after_mpa=42.0)
    blocked = platform.close_reconciliation_shift(actual_pressure_mpa=42.0, actual_stock_kg=10.0)
    assert blocked["state"] == BLOCKED
    assert blocked["classification"] == "large_difference"
    assert blocked["unexplained_difference_kg"] == -10.0
    with pytest.raises(ReconciliationError):
        platform.open_reconciliation_shift()
    assert platform.reconciliation.active_shift_id == blocked["shift_id"]
    alarms = platform.audit.alarms()
    assert any(item["action"] == "shift_close_blocked" for item in alarms)


def test_team_b_continues_with_separate_variance(platform):
    platform.close_reconciliation_shift(actual_pressure_mpa=42.0, actual_stock_kg=0.0)
    platform.open_reconciliation_shift("B")
    platform.record_buffer_movement(CHARGE, 20.0, pressure_after_mpa=42.0)
    platform.bank.restore(pressure_mpa=42.0, stored_kg=10.0)
    closed = platform.close_reconciliation_shift(actual_pressure_mpa=42.0, actual_stock_kg=10.0)
    assert closed["state"] == "closed"
    assert closed["classification"] == "large_difference"
    assert closed["adjustment_kg"] == -10.0
    assert closed["ending_stock_kg"] == 10.0
    assert closed["variance_movement_id"]
    next_shift = platform.open_reconciliation_shift("B")
    assert next_shift["opening_stock_kg"] == 10.0


def test_meter_mismatch_points_to_movement(platform):
    platform.record_buffer_movement(CHARGE, 20.0, pressure_after_mpa=42.0)
    before_movements = len(platform.reconciliation_movements()["movements"])
    partial = platform.record_meter_partial_reading(
        ISSUE,
        meter_id="mismatch-meter",
        meter_reading_after=3.0,
        reason="补读只读到一部分",
        previous_meter_reading=0.0,
        estimated_mass_kg=10.0,
    )
    assert partial["status"] == "partial_failed"
    assert len(platform.reconciliation_movements()["movements"]) == before_movements
    with pytest.raises(ReconciliationError):
        platform.close_reconciliation_shift(actual_pressure_mpa=42.0, actual_stock_kg=20.0)

    # 补全读数后正式入账；读数 3kg，正式取用 10kg，账面保留 7kg 差异线索。
    # 这里直接写台账以隔离库存动作，班末实物 3kg、账面 10kg，差额应指到该笔。
    movement = platform.reconciliation.post_movement(
        ISSUE,
        10.0,
        meter_id="mismatch-meter",
        meter_reading_after=3.0,
        reading_id=partial["reading_id"],
    )
    platform.bank.restore(pressure_mpa=42.0, stored_kg=3.0)
    closed = platform.close_reconciliation_shift(actual_pressure_mpa=42.0, actual_stock_kg=3.0)
    assert closed["state"] == BLOCKED
    assert closed["mismatches"][0]["movement_id"] == movement["movement_id"]
    assert closed["mismatches"][0]["meter_variance_kg"] == 7.0


def test_closed_shift_uses_its_rule_version(platform):
    platform.record_buffer_movement(CHARGE, 20.0, pressure_after_mpa=42.0)
    closed = platform.close_reconciliation_shift(actual_pressure_mpa=42.0, actual_stock_kg=20.0)
    old_rule = closed["rule_version"]
    platform.open_reconciliation_shift()
    new_rule = platform.publish_measurement_rule(
        version="R0002",
        mass_tolerance_kg=0.1,
        pressure_tolerance_mpa=0.2,
        kg_per_mpa=9.0,
        expected_pressure_mpa=43.0,
    )
    assert new_rule["version"] == "R0002"
    shifts = platform.reconciliation_shifts()["shifts"]
    assert shifts[0]["rule_version"] == old_rule
    assert shifts[0]["rule"]["pressure_tolerance_mpa"] == 1.5


def test_reconciliation_persistence_preserves_closed_history(platform):
    platform.record_buffer_movement(CHARGE, 12.0, pressure_after_mpa=42.0)
    platform.record_buffer_movement(ISSUE, 2.0)
    closed = platform.close_reconciliation_shift(actual_pressure_mpa=42.0, actual_stock_kg=10.0)
    assert closed["state"] == "closed"

    rebuilt = build_platform(
        default_config(),
        clock=platform.clock,
        storage_root=platform.store.root,
    )
    report = rebuilt.reconciliation_report()
    assert report["active_shift_id"] is None
    assert rebuilt.bank.stored_kg == 10.0
    assert rebuilt.verify_reconciliation()["consistent"] is True


def test_reconciliation_console_routes(platform):
    server = ConsoleServer(platform, port=0)
    base = server.start()
    try:
        status, payload = _post(
            f"{base}/api/reconciliation/movements",
            {"direction": "charge", "mass_kg": 10.0, "pressure_after_mpa": 42.0},
        )
        assert status == 200
        assert payload["direction"] == CHARGE
        with urllib.request.urlopen(f"{base}/api/reconciliation", timeout=5) as response:
            report = json.loads(response.read().decode("utf-8"))
        assert report["received_kg"] == 10.0
    finally:
        server.stop()
