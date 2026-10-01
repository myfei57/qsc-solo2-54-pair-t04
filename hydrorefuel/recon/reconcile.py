"""收、发、存平衡与差异定位（纯函数，不碰落盘与时钟）。

平衡式：``账面结存 = 期初存 + 本班充瓶 - 本班取用``，
``差异 = 盘点实存 - 账面结存``。差异为正盘盈、为负盘亏。

差异按当班锁定的计量规则分三档：死区内是正常压力波动（不算亏空，按实存结
转）、容差内是计量误差（不追笔）、超出容差是实质差异。实质差异先要定位到
具体哪一笔对不上：

- 若相关流水都有复核实测，逐笔比账面量与实测，直接点名对不上的笔
  （``entry_verified``）；
- 若只有部分流水有复核，把已复核仍对平的笔排除，圈出"缺复核、无法排除"的
  候选笔（``unverified_candidates``），提示去补读这几笔，绝不硬点一笔背锅；
- 一笔复核都没有时，如实返回 ``unresolved``，由现场逐笔补读后再结。
"""

from __future__ import annotations

from .models import (
    BAND_FLUCTUATION,
    BAND_MATERIAL,
    BAND_TOLERANCE,
    ENTRY_POSTED,
    POLICY_HOLD,
    Entry,
)
from .rules import MeteringRule


def _posted(entries: list[Entry]) -> list[Entry]:
    return [entry for entry in entries if entry.status == ENTRY_POSTED]


def totals(entries: list[Entry]) -> dict:
    """汇总本班已入账的充瓶、取用与净额。"""

    posted = _posted(entries)
    charge = sum(entry.qty_kg for entry in posted if entry.kind == "charge")
    draw = sum(entry.qty_kg for entry in posted if entry.kind == "draw")
    return {
        "charge_kg": round(charge, 6),
        "draw_kg": round(draw, 6),
        "net_kg": round(charge - draw, 6),
        "posted_count": len(posted),
        "pending_count": sum(1 for entry in entries if entry.status != ENTRY_POSTED),
    }


def book_balance(opening_kg: float, entries: list[Entry]) -> float:
    """账面结存：期初 + 充瓶 - 取用。"""

    flow = totals(entries)
    return round(opening_kg + flow["net_kg"], 6)


def _signed_verified(entry: Entry) -> float:
    assert entry.verified_delta_kg is not None
    delta = entry.verified_delta_kg
    return delta if entry.kind == "charge" else -delta


def _entry_suspects(entries: list[Entry], tolerance_kg: float) -> list[dict]:
    """逐笔复核：账面量与复核实测不一致（超出容差）的流水。"""

    suspects: list[dict] = []
    for entry in _posted(entries):
        if entry.verified_delta_kg is None:
            continue
        signed_book = entry.signed()
        signed_real = _signed_verified(entry)
        mismatch = round(signed_real - signed_book, 6)
        if abs(mismatch) > tolerance_kg + 1e-9:
            suspects.append(
                {
                    "entry_id": entry.entry_id,
                    "kind": entry.kind,
                    "book_kg": entry.qty_kg,
                    "verified_kg": entry.verified_delta_kg,
                    "mismatch_kg": mismatch,
                    "reason": "entry_verified_mismatch",
                }
            )
    return suspects


def _unverified_candidates(entries: list[Entry], tolerance_kg: float) -> list[dict]:
    """复核不全时：排除已复核对平的笔，圈出缺复核的候选笔。"""

    suspects: list[dict] = []
    for entry in _posted(entries):
        if entry.verified_delta_kg is None:
            suspects.append(
                {
                    "entry_id": entry.entry_id,
                    "kind": entry.kind,
                    "book_kg": entry.qty_kg,
                    "verified_kg": None,
                    "mismatch_kg": None,
                    "reason": "unverified_candidate",
                }
            )
        elif abs(_signed_verified(entry) - entry.signed()) > tolerance_kg + 1e-9:
            # 理论上不会走到（有不符会先走 entry_verified），保险起见仍列出。
            suspects.append(
                {
                    "entry_id": entry.entry_id,
                    "kind": entry.kind,
                    "book_kg": entry.qty_kg,
                    "verified_kg": entry.verified_delta_kg,
                    "mismatch_kg": round(_signed_verified(entry) - entry.signed(), 6),
                    "reason": "entry_verified_mismatch",
                }
            )
    return suspects


def locate(entries: list[Entry], rule: MeteringRule) -> dict:
    """把实质差异定位到具体流水，返回嫌疑笔与定位方式。"""

    posted = _posted(entries)
    verified = [entry for entry in posted if entry.verified_delta_kg is not None]
    suspects = _entry_suspects(entries, rule.tolerance_kg)
    if suspects:
        method = "entry_verified"
    elif verified:
        # 有复核但复核部分都对平：问题只能出在没复核的那几笔，圈出来补读。
        suspects = _unverified_candidates(entries, rule.tolerance_kg)
        method = "unverified_candidates"
    else:
        method = "unresolved"
    return {
        "method": method,
        "located": method == "entry_verified",
        "suspects": suspects,
    }


def settle(
    opening_kg: float,
    entries: list[Entry],
    physical_close_kg: float,
    rule: MeteringRule,
    policy: str,
) -> dict:
    """按当班规则算收、发、存与差异，并给出结班处置。"""

    flow = totals(entries)
    book_close = book_balance(opening_kg, entries)
    variance = round(physical_close_kg - book_close, 6)
    band = rule.classify(variance)

    pending = [entry.entry_id for entry in entries if entry.status != ENTRY_POSTED]
    action = "close"
    variance_carried_kg = 0.0
    resolved_close_kg = physical_close_kg
    locating: dict = {"method": "none", "located": True, "suspects": []}

    if pending:
        # 计量补读未落的部分失败必须先补读，不允许结班。
        band = BAND_MATERIAL
        action = "blocked_pending"
    elif band == BAND_FLUCTUATION:
        # 正常压力波动：不算亏空，按盘点实存结转，不立差异。
        action = "close"
    elif band == BAND_TOLERANCE:
        # 计量误差：不追笔，按实存结转。
        action = "close_with_tolerance"
    else:
        locating = locate(entries, rule)
        if policy == POLICY_HOLD:
            # 整班挂起：不结转，等查清。
            action = "hold"
        else:
            # 单列差异续结：差异单独挂账，账面按实存继续往下走。
            action = "isolate"
            variance_carried_kg = variance
    if action == "hold":
        resolved_close_kg = book_close
    return {
        "opening_kg": round(opening_kg, 6),
        "charge_kg": flow["charge_kg"],
        "draw_kg": flow["draw_kg"],
        "net_kg": flow["net_kg"],
        "book_close_kg": book_close,
        "physical_close_kg": round(physical_close_kg, 6),
        "variance_kg": variance,
        "band": band,
        "deadband_kg": rule.deadband_kg,
        "tolerance_kg": rule.tolerance_kg,
        "material_kg": rule.material_kg,
        "action": action,
        "policy": policy,
        "variance_carried_kg": round(variance_carried_kg, 6),
        "resolved_close_kg": round(resolved_close_kg, 6),
        "pending_entry_ids": pending,
        "posted_count": flow["posted_count"],
        "locating": locating,
    }
