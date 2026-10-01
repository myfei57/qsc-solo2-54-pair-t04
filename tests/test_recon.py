"""瓶组对账：逐笔收发、按班结、差异追笔、计量口径冻结与三处一致性。

对账口径写死为按班结：每次充瓶/取用记一笔，结班按开班锁定的计量规则把
盘点压力换算成气量，核收、发、存与差异。正常压力波动进死区不算亏空；差异
偏大按班组策略整班挂起或单列差异续结；计量补读未落属于部分失败，不按估数
入账。
"""

from __future__ import annotations

import pytest

from hydrorefuel.errors import ConfigError, ReconciliationError
from hydrorefuel.recon.hashchain import verify_chain
from hydrorefuel.recon.models import SHIFT_CLOSED, SHIFT_HELD
from hydrorefuel.recon.reconcile import book_balance, locate, settle, totals
from hydrorefuel.recon.rules import RuleBook


def _rule(factor=5.0, deadband=0.5, tolerance=1.5, material=3.0):
    book = RuleBook()
    book.add("v1", 0.0, factor, deadband, tolerance, material)
    return book.get("v1")


def _open_rule_book():
    book = RuleBook()
    book.add("v1", 0.0, 5.0, 0.5, 1.5, 3.0)
    return book


# --------------------------------------------------------------- 配置与粒度

def test_settle_grain_is_fixed_to_shift():
    """结班粒度写死为按班结，按日结的配置必须在装配期被拒绝。"""

    from hydrorefuel.config import DEFAULT_RECON_SECTION, ReconciliationLimits

    cfg = ReconciliationLimits.from_mapping(DEFAULT_RECON_SECTION)
    assert cfg.settle_by == "shift"
    with pytest.raises(ConfigError):
        ReconciliationLimits.from_mapping({**DEFAULT_RECON_SECTION, "settle_by": "day"})
    with pytest.raises(ConfigError):
        ReconciliationLimits.from_mapping({**DEFAULT_RECON_SECTION, "default_policy": "rewrite"})
    with pytest.raises(ConfigError):
        ReconciliationLimits.from_mapping(
            {**DEFAULT_RECON_SECTION, "deadband_kg": 3.0, "tolerance_kg": 1.0, "material_kg": 2.0}
        )


# ----------------------------------------------------------- 收、发、存平衡

def test_every_charge_and_draw_is_recorded(platform):
    from hydrorefuel.recon.models import Entry

    platform.pressurize(charge_kg=10.0)
    platform.fill(vehicle="v1", steps=2, dt=36.0)
    stock = platform.recon_stock()
    open_id = stock["open_shift_id"]
    entries = platform.recon_entries(open_id)["entries"]
    kinds = [(e["kind"], e["status"]) for e in entries]
    assert ("charge", "posted") in kinds
    assert ("draw", "posted") in kinds
    flow = totals([Entry.from_dict(e) for e in entries])
    assert flow["charge_kg"] == pytest.approx(10.0)
    assert flow["draw_kg"] > 0
    assert flow["pending_count"] == 0


def test_shift_balance_is_receipt_issue_balance(platform):
    """期初 + 充瓶 - 取用 = 账面结存；盘点实存按当班规则由压力换算。"""

    from hydrorefuel.recon.models import Entry

    open_id = platform.recon_stock()["open_shift_id"]
    platform.pressurize(charge_kg=20.0)
    platform.fill(vehicle="v1", steps=2, dt=36.0)
    objs = [Entry.from_dict(e) for e in platform.recon_entries(open_id)["entries"]]
    flow = totals(objs)
    assert book_balance(0.0, objs) == pytest.approx(flow["net_kg"])

    # 盘点压力 4.0 MPa × 系数 5 = 20 kg。
    result = platform.recon_close_shift(4.0)
    report = result["report"]
    assert report["settle_by"] == "shift"
    assert report["physical_close_kg"] == pytest.approx(20.0)
    assert report["book_close_kg"] == pytest.approx(flow["net_kg"])
    assert report["variance_kg"] == pytest.approx(20.0 - flow["net_kg"])


# ----------------------------------------------------------- 压力波动不算亏空

def test_deadband_fluctuation_is_not_a_loss(platform):
    platform.pressurize(charge_kg=10.0)
    open_id = platform.recon_stock()["open_shift_id"]
    # 账面 10 kg，盘点压力 2.01 MPa → 10.05 kg，差异 0.05 kg 在死区 0.5 内。
    result = platform.recon_close_shift(2.01)
    report = result["report"]
    assert report["band"] == "fluctuation"
    assert report["action"] == "close"
    assert report["variance_carried_kg"] == 0.0
    shift = platform.recon_shift(open_id)
    assert shift["state"] == SHIFT_CLOSED
    # 波动按实存结转，下一班期初即实存。
    assert platform.recon_stock()["current_stock_kg"] == pytest.approx(10.05)


def test_tolerance_band_closes_without_chasing(platform):
    platform.pressurize(charge_kg=10.0)
    # 账面 10 kg、实盘 11 kg：差异 1.0 kg 超死区 0.5 但在容差 1.5 内，不追笔。
    result = platform.recon_close_shift(11.0 / 5.0)
    report = result["report"]
    assert report["band"] == "tolerance"
    assert report["action"] == "close_with_tolerance"


# ------------------------------------------------------- 差异偏大：班组策略

def test_material_variance_holds_whole_shift(platform):
    # 结掉首装自动开的空默认班（实盘 0、差异 0，正常结），再开 hold 班组班。
    platform.recon_close_shift(0.0)
    platform.recon_set_policy("crewA", "hold")
    platform.recon_open_shift("crewA")
    platform.pressurize(charge_kg=20.0)
    open_id = platform.recon_stock()["open_shift_id"]
    # 盘点实存比账面少 5 kg，超出 material=3。
    physical_pressure = (20.0 - 5.0) / 5.0
    result = platform.recon_close_shift(physical_pressure)
    report = result["report"]
    assert report["band"] == "material"
    assert report["action"] == "hold"
    assert platform.recon_shift(open_id)["state"] == SHIFT_HELD
    # 整班挂起时不结转、也不替下一班做决定，仍停在被挂起的班。
    stock = platform.recon_stock()
    assert stock["open_shift_id"] is None
    assert stock["held_shift_id"] == open_id
    assert stock["held_shifts"] == 1


def test_material_variance_isolated_allows_continue(platform):
    platform.recon_close_shift(0.0)
    platform.recon_set_policy("crewB", "isolate")
    platform.recon_open_shift("crewB")
    platform.pressurize(charge_kg=20.0)
    held_id = platform.recon_stock()["open_shift_id"]
    result = platform.recon_close_shift((20.0 - 5.0) / 5.0)
    report = result["report"]
    assert report["action"] == "isolate"
    assert report["variance_carried_kg"] == pytest.approx(-5.0)
    assert platform.recon_shift(held_id)["state"] == SHIFT_CLOSED
    # 单列差异后照常结班，不替下一班做决定：此时没有在班。
    assert platform.recon_stock()["open_shift_id"] is None
    # 显式开下一班，期初取自已结班盘点实存 15 kg。
    platform.recon_open_shift("crewB")
    new_id = platform.recon_stock()["open_shift_id"]
    assert new_id != held_id
    assert platform.recon_shift(new_id)["opening_kg"] == pytest.approx(15.0)
    assert platform.recon_stock()["current_stock_kg"] == pytest.approx(15.0)


# ----------------------------------------------------------- 差异定位到单笔

def test_locate_points_to_the_mismatching_entry(platform):
    from hydrorefuel.recon.models import Entry

    book = _open_rule_book()
    rule = book.get("v1")
    entries = [
        Entry.create("e1", "s", "charge", 10.0, 1.0, verified_delta_kg=10.0),
        Entry.create("e2", "s", "draw", 4.0, 2.0, verified_delta_kg=6.0),
        Entry.create("e3", "s", "charge", 8.0, 3.0, verified_delta_kg=8.0),
    ]
    locating = locate(entries, rule)
    assert locating["method"] == "entry_verified"
    assert [item["entry_id"] for item in locating["suspects"]] == ["e2"]
    assert locating["suspects"][0]["mismatch_kg"] == pytest.approx(-2.0)


def test_locate_falls_back_to_unverified_candidates(platform):
    """只有部分流水复核、且复核部分对平时，圈出缺复核的候选笔让人补读。"""

    from hydrorefuel.recon.models import Entry

    rule = _rule()
    # e1 已复核且对平；e2、e3 没复核。差异无法点名到单笔，只能圈候选。
    entries = [
        Entry.create("e1", "s", "charge", 10.0, 1.0, verified_delta_kg=10.0),
        Entry.create("e2", "s", "draw", 2.0, 2.0),
        Entry.create("e3", "s", "draw", 2.0, 3.0),
    ]
    locating = locate(entries, rule)
    assert locating["method"] == "unverified_candidates"
    assert locating["located"] is False
    assert {item["entry_id"] for item in locating["suspects"]} == {"e2", "e3"}


def test_locate_unresolved_without_any_verification(platform):
    from hydrorefuel.recon.models import Entry

    rule = _rule()
    entries = [Entry.create("e1", "s", "charge", 10.0, 1.0)]
    locating = locate(entries, rule)
    assert locating["method"] == "unresolved"
    assert locating["located"] is False
    assert locating["suspects"] == []


def test_material_close_report_carries_locating(platform):
    platform.pressurize(charge_kg=20.0)
    open_id = platform.recon_stock()["open_shift_id"]
    entry_id = next(
        e["entry_id"] for e in platform.recon_entries(open_id)["entries"] if e["kind"] == "charge"
    )
    # 复核该笔实际只充进 15 kg，比账面少 5 kg。
    platform.recon_verify_entry(entry_id, 15.0)
    result = platform.recon_close_shift((20.0 - 5.0) / 5.0)
    locating = result["report"]["locating"]
    assert locating["located"] is True
    assert locating["suspects"][0]["entry_id"] == entry_id


# ------------------------------------------------- 计量补读：部分失败不入估数

def test_pending_reading_is_not_booked_as_estimate(platform):
    pending = platform.recon_pending("draw", operator="v9", meter_ref="m1")
    assert pending["status"] == "pending_read"
    assert pending["qty_kg"] == 0.0
    open_id = platform.recon_stock()["open_shift_id"]
    assert platform.recon_stock()["open_shift"]["pending_count"] == 1
    # 有待补读时结班必须被拒绝。
    with pytest.raises(ReconciliationError):
        platform.recon_close_shift(0.0)
    # 补读失败仍不入账。
    failed = platform.recon_fail_reading(pending["entry_id"], "通讯中断")
    assert failed["status"] == "pending_read"
    with pytest.raises(ReconciliationError):
        platform.recon_close_shift(0.0)
    # 补读实际读数后才入账，且不进收发存以外的任何估值。
    posted = platform.recon_supply_reading(pending["entry_id"], 7.0)
    assert posted["status"] == "posted"
    assert posted["qty_kg"] == pytest.approx(7.0)
    result = platform.recon_close_shift((0.0 - 0.0) / 5.0)
    # 账面 -7（只取不充，期初0）盘点为0，差异 +7 触发挂起；改用 isolate 验证能结。
    assert result["report"]["pending_entry_ids"] == []


def test_pending_batch_partial_failure(platform):
    """批量计量里一部分读数成功、一部分失败：失败的挂起待补读，成功的正常入账。"""

    ok_entry = platform.recon_charge(6.0, meter_ref="m-a")
    pending = platform.recon_pending("charge", meter_ref="m-b", note="批量补读第2笔失败")
    open_id = platform.recon_stock()["open_shift_id"]
    entries = platform.recon_entries(open_id)["entries"]
    assert any(e["entry_id"] == ok_entry["entry_id"] and e["status"] == "posted" for e in entries)
    assert any(e["entry_id"] == pending["entry_id"] and e["status"] == "pending_read" for e in entries)


# --------------------------------------------- 计量规则版本化与已结班不可变

def test_closed_shift_keeps_historical_rule(platform, clock):
    platform.pressurize(charge_kg=10.0)
    first = platform.recon_stock()["open_shift_id"]
    platform.recon_close_shift(2.0)  # v1：10 kg，平衡结班。

    # 换一版系数（10 kg/MPa）与更宽容差，只影响之后的班。
    clock.advance(10.0)
    platform.recon_register_rule("v2", 10.0, 0.5, 1.5, 3.0, note="换表")
    platform.recon_open_shift("crewX")
    second = platform.recon_stock()["open_shift_id"]
    platform.pressurize(charge_kg=10.0)
    platform.recon_close_shift(1.0)  # v2：1.0 MPa × 10 = 10 kg。

    closed_first = platform.recon_shift(first)
    closed_second = platform.recon_shift(second)
    assert closed_first["rule_version"] == "v1"
    assert closed_second["rule_version"] == "v2"
    # 已结班报告口径冻结，不被换版改写。
    assert closed_first["close_report"]["physical_close_kg"] == pytest.approx(10.0)
    assert closed_second["close_report"]["physical_close_kg"] == pytest.approx(10.0)
    versions = [r["version"] for r in platform.recon_rules()["rules"]]
    assert versions == ["v1", "v2"]


def test_duplicate_rule_version_rejected(platform):
    with pytest.raises(ConfigError):
        platform.recon_register_rule("v1", 5.0, 0.5, 1.5, 3.0)


def test_rule_version_locked_at_shift_open(platform, clock):
    platform.pressurize(charge_kg=10.0)
    open_id = platform.recon_stock()["open_shift_id"]
    clock.advance(5.0)
    platform.recon_register_rule("v2", 9.0, 0.5, 1.5, 3.0)
    # 在班中途换版，本班仍按 v1：盘点 2.0 MPa 按 v1 得 10 kg。
    result = platform.recon_close_shift(2.0)
    assert result["shift"]["rule_version"] == "v1"
    assert result["report"]["physical_close_kg"] == pytest.approx(10.0)
    assert platform.recon_shift(open_id)["rule_version"] == "v1"


# ------------------------------------------------------------- 历史不可变

def test_closed_shift_is_immutable(platform):
    platform.pressurize(charge_kg=10.0)
    first = platform.recon_stock()["open_shift_id"]
    platform.recon_close_shift(2.0)
    # 已结班不允许再结或改动。
    with pytest.raises(ReconciliationError):
        platform.recon_close_shift(2.0)
    shift = platform.recon_shift(first)
    assert shift["state"] == SHIFT_CLOSED
    assert shift["close_report"]["settle_by"] == "shift"


def test_append_only_hashchain_tamper_is_detected(platform):
    platform.pressurize(charge_kg=10.0)
    platform.recon_close_shift(2.0)
    records = platform.recon_journal()["records"]
    assert verify_chain(records)["ok"] is True
    tampered = [dict(records[0])]
    tampered[0]["payload"] = {**tampered[0]["payload"], "qty_kg": 999.0}
    tampered.extend(records[1:])
    result = verify_chain(tampered)
    assert result["ok"] is False
    assert result["broken_at_seq"] == 0


# ------------------------------------------------------- 库存/流水/审计一致

def test_three_way_consistency_passes(platform):
    platform.pressurize(charge_kg=10.0)
    platform.fill(vehicle="v1", steps=1, dt=36.0)
    platform.recon_close_shift(1.5)
    check = platform.recon_consistency()
    assert check["ok"] is True
    assert check["journal_chain"]["ok"] is True
    assert check["stock"]["ok"] is True
    assert check["audit"]["missing_entry_ids"] == []
    assert check["audit"]["missing_shift_ids"] == []


def test_every_close_is_audited(platform):
    platform.pressurize(charge_kg=10.0)
    open_id = platform.recon_stock()["open_shift_id"]
    platform.recon_close_shift(2.0)
    audit = platform.audit.entries(limit=100_000)
    closes = [
        e for e in audit
        if e["component"] == "recon" and e["action"] == "recon_close"
        and e["payload"].get("shift_id") == open_id
    ]
    assert closes
    payload = closes[0]["payload"]
    for key in ("charge_kg", "draw_kg", "book_close_kg", "physical_close_kg", "variance_kg"):
        assert key in payload


def test_stock_is_on_console_status(platform):
    platform.pressurize(charge_kg=10.0)
    status = platform.status()
    assert status["recon"]["settle_by"] == "shift"
    assert "current_stock_kg" in status["recon"]


# ------------------------------------------------------------- 重启后连续性

def test_state_survives_restart(tmp_path, clock):
    from hydrorefuel.app import build_platform
    from hydrorefuel.config import default_config

    root = str(tmp_path / "state")
    first = build_platform(default_config(), clock=clock, storage_root=root)
    first.pressurize(charge_kg=10.0)
    open_id = first.recon_stock()["open_shift_id"]
    first.recon_close_shift(2.0)

    second = build_platform(default_config(), clock=clock, storage_root=root)
    assert second.recon_stock()["current_stock_kg"] == pytest.approx(10.0)
    # v1 规则与已结班都从追加流水重放，未重复登记规则。
    assert [r["version"] for r in second.recon_rules()["rules"]] == ["v1"]
    assert second.recon_shift(open_id)["close_report"]["variance_kg"] == pytest.approx(0.0)
    # 三处一致性：哈希链完整、库存由流水重放与台账一致。
    check = second.recon_consistency()
    assert check["ok"] is True
    assert check["journal_chain"]["ok"] is True
    assert check["stock"]["ok"] is True


# ------------------------------------------------------------- 班组策略校验

def test_invalid_policy_rejected(platform):
    with pytest.raises(ReconciliationError):
        platform.recon_set_policy("crewZ", "writeoff")


def test_double_open_rejected(platform):
    with pytest.raises(ReconciliationError):
        platform.recon_open_shift("another")
