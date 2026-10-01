"""瓶组收发存台账与班结对账。

台账只追加，不修改历史流水。每次充瓶、取用或经授权的差异调整都生成一条带哈希
前链的流水；结班按固定的"班结"口径计算收、发、存和差额。正常压力波动按结班
时那一版计量规则折算并吸收，不把它写成亏空。差额超班组阈值时，按班组策略整班
挂起或单列差异后继续结班。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..clock import Clock
from ..errors import HydroError, StorageError
from ..eventbus import Event, EventBus
from ..store.jsonfile import JsonStore
from ..store.journal import MAX_SEGMENTS, JsonlJournal
from .rules import MeasurementRule, RuleBook

CHARGE = "charge"
ISSUE = "issue"
VARIANCE = "variance"
MOVEMENTS = (CHARGE, ISSUE, VARIANCE)

OPEN = "open"
CLOSED = "closed"
BLOCKED = "blocked"

CONTINUE = "continue"
HOLD = "hold"
SETTLEMENT = "shift"
STATE_DOC = "reconciliation_state"


class ReconciliationError(HydroError):
    """对账前置条件不满足或输入不是可入账的完整计量。"""


@dataclass(frozen=True)
class TeamPolicy:
    """班组差额策略。"""

    team: str
    large_threshold_kg: float
    on_large: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "TeamPolicy":
        team = str(raw.get("team", ""))
        if not team:
            raise ValueError("班组名称不能为空")
        threshold = float(raw.get("large_threshold_kg", 0.0))
        if threshold < 0:
            raise ValueError("班组差额阈值不能为负")
        on_large = str(raw.get("on_large", HOLD))
        if on_large not in (HOLD, CONTINUE):
            raise ValueError("大额差异策略只能是 hold 或 continue")
        return cls(team=team, large_threshold_kg=threshold, on_large=on_large)

    def as_dict(self) -> dict:
        return {"team": self.team, "large_threshold_kg": self.large_threshold_kg, "on_large": self.on_large}


def _canonical_hash(record_type: str, payload: Mapping[str, Any], previous_hash: str) -> str:
    material = json.dumps(
        {"record_type": record_type, "payload": payload, "previous_hash": previous_hash},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _signed(direction: str, value: float) -> float:
    if direction == CHARGE:
        return value
    if direction == ISSUE:
        return -value
    if direction == VARIANCE:
        # 调用方对盘亏传负数、盘盈传正数。
        return value
    raise ReconciliationError(f"未知流水方向: {direction}", component="recon")


def _r(value: float) -> float:
    return round(float(value), 6)


class ReconciliationLedger:
    """收发存流水、不可变班结和一致性核对。"""

    def __init__(
        self,
        store: JsonStore,
        clock: Clock,
        bus: EventBus,
        rule: MeasurementRule,
        policies: Mapping[str, TeamPolicy],
        default_team: str,
    ) -> None:
        if rule.settlement != SETTLEMENT:
            raise ReconciliationError("结算口径必须固定为班结", component="recon")
        if default_team not in policies:
            raise ReconciliationError(f"默认班组不存在: {default_team}", component="recon")
        self._store = store
        self._clock = clock
        self._bus = bus
        self._policies = dict(policies)
        self._default_team = default_team
        self._lock = threading.RLock()
        self.movement_journal = JsonlJournal(store.root, "recon_movement", segment_limit=512)
        self.shift_journal = JsonlJournal(store.root, "recon_shift", segment_limit=128)
        self.reading_journal = JsonlJournal(store.root, "recon_reading", segment_limit=256)

        self._movements: list[dict] = []
        self._shift_records: list[dict] = []
        self._reading_records: list[dict] = []
        self._movement_hash = "0"
        self._shift_hash = "0"
        self._reading_hash = "0"
        self._movement_seq = 0
        self._shift_record_seq = 0
        self._reading_seq = 0
        self._shift_number = 0
        self._rule_book: RuleBook
        self._active_shift_id: str | None = None
        self._restore(rule)

    def _restore(self, fallback_rule: MeasurementRule) -> None:
        self._movements = self.movement_journal.read_all()
        self._shift_records = self.shift_journal.read_all()
        self._reading_records = self.reading_journal.read_all()
        state = self._store.read(STATE_DOC)
        rules_rows = [fallback_rule.as_dict()]
        if isinstance(state, dict):
            saved_rules = state.get("rules")
            if isinstance(saved_rules, list) and saved_rules:
                rules_rows = saved_rules
        self._rule_book = RuleBook.from_dicts(rules_rows)

        for row in self._movements:
            self._movement_hash = str(row.get("hash", ""))
            self._movement_seq = max(self._movement_seq, int(row.get("seq", 0)))
        for row in self._shift_records:
            self._shift_hash = str(row.get("hash", ""))
            self._shift_record_seq = max(self._shift_record_seq, int(row.get("record_seq", 0)))
            self._shift_number = max(self._shift_number, int(row.get("shift_number", 0)))
        for row in self._reading_records:
            self._reading_hash = str(row.get("hash", ""))
            self._reading_seq = max(self._reading_seq, int(row.get("seq", 0)))

        if isinstance(state, dict) and state.get("active_shift_id") is not None:
            self._active_shift_id = str(state["active_shift_id"])
        else:
            closed = {row["shift_id"] for row in self._shift_records if row.get("record_type") == "shift_close"}
            opens = [row for row in self._shift_records if row.get("record_type") == "shift_open"]
            unclosed = [row for row in opens if row["shift_id"] not in closed]
            if len(unclosed) > 1:
                raise StorageError("恢复到多个未结班次，无法确定唯一当前班", component="recon")
            self._active_shift_id = unclosed[-1]["shift_id"] if unclosed else None
        if not isinstance(state, dict):
            self._persist_state()

    def _persist_state(self) -> None:
        payload = {
            "settlement": SETTLEMENT,
            "default_team": self._default_team,
            "active_rule_version": self._rule_book.at(self._clock.now()).version,
            "rules": self._rule_book.as_dicts(),
            "policies": {name: policy.as_dict() for name, policy in self._policies.items()},
            "active_shift_id": self._active_shift_id,
            "last_movement_seq": self._movement_seq,
            "last_shift_record_seq": self._shift_record_seq,
            "last_reading_seq": self._reading_seq,
        }
        self._store.write(STATE_DOC, payload)

    @property
    def rule(self) -> MeasurementRule:
        """当前时刻已生效的规则；未来发布版本不会提前解释当前流水。"""

        return self._rule_book.at(self._clock.now())

    @property
    def active_shift_id(self) -> str | None:
        return self._active_shift_id

    def reset(self) -> dict:
        """清空对账台账与规则状态，仅用于交付/演练前整体复位。"""

        with self._lock:
            for name in ("recon_movement", "recon_shift", "recon_reading"):
                for index in range(MAX_SEGMENTS):
                    path = os.path.join(self._store.root, f"{name}-{index:05d}.jsonl")
                    if not os.path.isfile(path):
                        break
                    os.remove(path)
            self._store.remove(STATE_DOC)
            self.movement_journal = JsonlJournal(self._store.root, "recon_movement", segment_limit=512)
            self.shift_journal = JsonlJournal(self._store.root, "recon_shift", segment_limit=128)
            self.reading_journal = JsonlJournal(self._store.root, "recon_reading", segment_limit=256)
            initial = MeasurementRule(
                version="R0001",
                effective_ts=self._clock.now(),
                settlement=SETTLEMENT,
                mass_tolerance_kg=self.rule.mass_tolerance_kg,
                pressure_tolerance_mpa=self.rule.pressure_tolerance_mpa,
                kg_per_mpa=self.rule.kg_per_mpa,
                expected_pressure_mpa=self.rule.expected_pressure_mpa,
            )
            self._rule_book = RuleBook(initial)
            self._movements = []
            self._shift_records = []
            self._reading_records = []
            self._movement_hash = self._shift_hash = self._reading_hash = "0"
            self._movement_seq = self._shift_record_seq = self._reading_seq = self._shift_number = 0
            self._active_shift_id = None
            self._persist_state()
            self._publish("ledger_reset", severity="warning", message="对账台账已整体复位")
            return {"reset": True, "state_path": self._store.path_of(STATE_DOC)}

    def publish_rule(
        self,
        version: str,
        mass_tolerance_kg: float,
        pressure_tolerance_mpa: float,
        kg_per_mpa: float,
        expected_pressure_mpa: float = 42.0,
        effective_ts: float | None = None,
    ) -> dict:
        """发布一版计量规则；老规则与已结班均保持不变。"""

        with self._lock:
            rule = self._rule_book.publish(
                version=version,
                effective_ts=self._clock.now() if effective_ts is None else effective_ts,
                mass_tolerance_kg=mass_tolerance_kg,
                pressure_tolerance_mpa=pressure_tolerance_mpa,
                kg_per_mpa=kg_per_mpa,
                expected_pressure_mpa=expected_pressure_mpa,
                settlement=SETTLEMENT,
            )
            self._persist_state()
            self._publish("rule_published", severity="notice", rule=rule.as_dict())
            return rule.as_dict()

    def rules(self) -> list[dict]:
        with self._lock:
            return self._rule_book.as_dicts()

    def policies(self) -> dict:
        with self._lock:
            return copy.deepcopy({name: policy.as_dict() for name, policy in self._policies.items()})

    def set_team_policy(self, team: str, large_threshold_kg: float, on_large: str) -> dict:
        """更新班组的大额差异策略；只影响之后结班。"""

        with self._lock:
            policy = TeamPolicy.from_mapping(
                {"team": team, "large_threshold_kg": large_threshold_kg, "on_large": on_large}
            )
            self._policies[team] = policy
            self._persist_state()
            self._publish("team_policy_updated", severity="notice", **policy.as_dict())
            return policy.as_dict()

    def open_shift(self, team: str | None = None, opening_stock_kg: float | None = None) -> dict:
        """开班。首个班从 0 起，后续班承继上一结班存量。"""

        with self._lock:
            team_name = team or self._default_team
            if team_name not in self._policies:
                raise ReconciliationError(f"班组未配置差异策略: {team_name}", component="recon")
            if self._active_shift_id is not None:
                raise ReconciliationError(f"当前班 {self._active_shift_id} 尚未结班", component="recon")
            summaries = self.shift_summaries()
            if opening_stock_kg is None:
                opening = 0.0 if not summaries else _r(summaries[-1]["ending_stock_kg"])
            else:
                opening = _r(opening_stock_kg)
                if summaries:
                    opening = _r(summaries[-1]["ending_stock_kg"])
            self._shift_number += 1
            shift_id = f"S{self._shift_number:06d}"
            rule = self._rule_book.at(self._clock.now())
            payload = {
                "record_type": "shift_open",
                "record_seq": self._shift_record_seq + 1,
                "shift_number": self._shift_number,
                "shift_id": shift_id,
                "team": team_name,
                "state": OPEN,
                "opened_ts": _r(self._clock.now()),
                "opening_stock_kg": opening,
                "rule_version": rule.version,
                "rule": rule.as_dict(),
                "policy": self._policies[team_name].as_dict(),
            }
            record = self._append(self.shift_journal, self._shift_records, self._shift_hash, payload)
            self._shift_hash = record["hash"]
            self._shift_record_seq = record["record_seq"]
            self._active_shift_id = shift_id
            self._persist_state()
            self._publish("shift_opened", severity="notice", shift_id=shift_id, team=team_name, opening_stock_kg=opening)
            return copy.deepcopy(record)

    def _append(self, journal: JsonlJournal, rows: list[dict], previous_hash: str, payload: dict) -> dict:
        record_type = str(payload["record_type"])
        record = dict(payload)
        record["previous_hash"] = previous_hash
        record["hash"] = _canonical_hash(record_type, payload, previous_hash)
        journal.append(record)
        rows.append(record)
        return record

    def _require_active(self) -> dict:
        if self._active_shift_id is None:
            raise ReconciliationError("当前没有开班，不能记账或结班", component="recon")
        return self._open_record(self._active_shift_id)

    def _open_record(self, shift_id: str) -> dict:
        for row in reversed(self._shift_records):
            if row.get("shift_id") == shift_id and row.get("record_type") == "shift_open":
                return row
        raise ReconciliationError(f"班次不存在: {shift_id}", component="recon")

    def shift_summaries(self) -> list[dict]:
        """把开班/结班两类不可变记录折成班次视图。"""

        summaries: list[dict] = []
        for row in self._shift_records:
            if row.get("record_type") == "shift_open":
                summaries.append(copy.deepcopy(row))
            elif row.get("record_type") == "shift_close":
                target = next(item for item in reversed(summaries) if item["shift_id"] == row["shift_id"])
                target.update(copy.deepcopy(row))
                target["state"] = CLOSED
        return summaries

    def _last_meter_reading(self, meter_id: str) -> float | None:
        # 物理累计表跨班连续，不能在新班把既有底数误算成新班差额。
        for row in reversed(self._movements):
            if row.get("meter_id") == meter_id:
                value = row.get("meter_reading_after")
                return None if value is None else float(value)
        return None

    def record_partial_reading(
        self,
        direction: str,
        meter_id: str,
        meter_reading_after: float | None,
        reason: str,
        previous_meter_reading: float | None = None,
        reference_id: str | None = None,
        estimated_mass_kg: float | None = None,
    ) -> dict:
        """登记计量补读部分失败。估数只留痕为线索，绝不入账。"""

        with self._lock:
            shift = self._require_active()
            if direction not in MOVEMENTS:
                raise ReconciliationError(f"未知流水方向: {direction}", component="recon")
            if not meter_id:
                raise ReconciliationError("计量表编号不能为空", component="recon")
            if meter_reading_after is not None and float(meter_reading_after) < 0:
                raise ReconciliationError("表读数不能为负", component="recon")
            payload = {
                "record_type": "reading_partial",
                "seq": self._reading_seq + 1,
                "reading_id": f"R{self._reading_seq + 1:07d}",
                "shift_id": shift["shift_id"],
                "team": shift["team"],
                "ts": _r(self._clock.now()),
                "direction": direction,
                "meter_id": meter_id,
                "previous_meter_reading": None if previous_meter_reading is None else _r(previous_meter_reading),
                "meter_reading_after": None if meter_reading_after is None else _r(meter_reading_after),
                "estimated_mass_kg": None if estimated_mass_kg is None else _r(estimated_mass_kg),
                "status": "partial_failed",
                "reference_id": reference_id,
                "reason": str(reason),
            }
            record = self._append(self.reading_journal, self._reading_records, self._reading_hash, payload)
            self._reading_hash = record["hash"]
            self._reading_seq = record["seq"]
            self._persist_state()
            self._publish(
                "meter_reading_partial",
                severity="warning",
                shift_id=shift["shift_id"],
                reading_id=record["reading_id"],
                meter_id=meter_id,
                direction=direction,
                reason=reason,
                message="计量补读部分失败：未取得完整读数，不能按估数入账",
            )
            return copy.deepcopy(record)

    def _resolved_reading_ids(self, shift_id: str) -> set[str]:
        return {
            str(row["reading_id"])
            for row in self._reading_records
            if row.get("shift_id") == shift_id and row.get("record_type") == "reading_resolved"
        }

    def _unresolved_readings(self, shift_id: str) -> list[dict]:
        resolved = self._resolved_reading_ids(shift_id)
        return [
            row
            for row in self._reading_records
            if row.get("shift_id") == shift_id
            and row.get("record_type") == "reading_partial"
            and row.get("reading_id") not in resolved
        ]

    def partial_reading(self, reading_id: str) -> dict:
        """返回当前班一笔未处理的补读部分失败记录。"""

        with self._lock:
            shift = self._require_active()
            row = self._partial_reading(reading_id)
            if row["shift_id"] != shift["shift_id"]:
                raise ReconciliationError("补读记录与当前班次不一致", component="recon")
            if row["reading_id"] in self._resolved_reading_ids(shift["shift_id"]):
                raise ReconciliationError("该补读记录已经关联过完整入账", component="recon")
            return copy.deepcopy(row)

    def post_movement(
        self,
        direction: str,
        mass_kg: float,
        *,
        meter_id: str = "manual",
        meter_reading_after: float | None = None,
        pressure_before_mpa: float | None = None,
        pressure_after_mpa: float | None = None,
        reading_id: str | None = None,
        reference_id: str | None = None,
        note: str = "",
    ) -> dict:
        """追加一笔充瓶、取用或差异调整流水；差异盘亏传负数。"""

        with self._lock:
            shift = self._require_active()
            if direction not in MOVEMENTS:
                raise ReconciliationError(f"未知流水方向: {direction}", component="recon")
            mass = float(mass_kg)
            if direction == VARIANCE:
                if mass == 0:
                    raise ReconciliationError("差异调整不能为 0", component="recon")
            elif mass <= 0:
                raise ReconciliationError("充瓶和取用气量必须为正", component="recon")
            self._validate_pressure(pressure_before_mpa, "操作前")
            self._validate_pressure(pressure_after_mpa, "操作后")
            rule = self._rule_book.at(self._clock.now())

            previous_reading = self._last_meter_reading(meter_id)
            reading_after = None if meter_reading_after is None else _r(meter_reading_after)
            observed_delta: float | None = None
            meter_variance_kg: float | None = None
            if reading_after is not None:
                baseline = previous_reading if previous_reading is not None else 0.0
                observed_delta = _r(reading_after - baseline)
                signed_observed = observed_delta if direction == CHARGE else -observed_delta
                meter_variance_kg = _r(signed_observed - _signed(direction, mass))
            if meter_variance_kg is not None and abs(meter_variance_kg) > rule.mass_tolerance_kg and reading_id is None:
                raise ReconciliationError(
                    f"计量差额 {meter_variance_kg} kg 超过公差，需先完成计量补读",
                    component="recon",
                )

            linked_reading = None
            if reading_id is not None:
                linked_reading = self._partial_reading(reading_id)
                if linked_reading["shift_id"] != shift["shift_id"]:
                    raise ReconciliationError("补读记录与当前班次不一致", component="recon")
                if linked_reading["reading_id"] in self._resolved_reading_ids(shift["shift_id"]):
                    raise ReconciliationError("该补读记录已经关联过完整入账", component="recon")

            signed_mass = _signed(direction, mass)
            self._movement_seq += 1
            balance_after = _r(
                shift["opening_stock_kg"]
                + sum(float(item["signed_mass_kg"]) for item in self._active_movements(shift["shift_id"]))
                + signed_mass
            )
            payload = {
                "record_type": "movement",
                "seq": self._movement_seq,
                "movement_id": f"M{self._movement_seq:07d}",
                "shift_id": shift["shift_id"],
                "team": shift["team"],
                "ts": _r(self._clock.now()),
                "direction": direction,
                "mass_kg": _r(abs(mass)),
                "signed_mass_kg": _r(signed_mass),
                "balance_kg": balance_after,
                "meter_id": meter_id,
                "meter_reading_after": reading_after,
                "meter_delta_kg": None if observed_delta is None else _r(abs(observed_delta)),
                "meter_variance_kg": meter_variance_kg,
                "pressure_before_mpa": None if pressure_before_mpa is None else _r(pressure_before_mpa),
                "pressure_after_mpa": None if pressure_after_mpa is None else _r(pressure_after_mpa),
                "rule_version": rule.version,
                "reading_id": reading_id,
                "reference_id": reference_id,
                "note": note,
            }
            record = self._append(self.movement_journal, self._movements, self._movement_hash, payload)
            self._movement_hash = record["hash"]
            if linked_reading is not None:
                resolve_payload = {
                    "record_type": "reading_resolved",
                    "seq": self._reading_seq + 1,
                    "reading_id": linked_reading["reading_id"],
                    "shift_id": shift["shift_id"],
                    "ts": record["ts"],
                    "status": "resolved",
                    "resolved_movement_id": record["movement_id"],
                }
                resolved = self._append(
                    self.reading_journal, self._reading_records, self._reading_hash, resolve_payload
                )
                self._reading_hash = resolved["hash"]
                self._reading_seq = resolved["seq"]
            self._persist_state()
            self._publish(
                f"buffer_{direction}",
                severity="notice",
                shift_id=shift["shift_id"],
                movement_id=record["movement_id"],
                mass_kg=record["mass_kg"],
                signed_mass_kg=record["signed_mass_kg"],
                meter_id=meter_id,
                rule_version=rule.version,
                reading_id=reading_id,
            )
            return copy.deepcopy(record)

    def _validate_pressure(self, value: float | None, label: str) -> None:
        if value is not None and float(value) < 0:
            raise ReconciliationError(f"{label}压力不能为负", component="recon")

    def _partial_reading(self, reading_id: str) -> dict:
        for row in reversed(self._reading_records):
            if row.get("reading_id") == reading_id and row.get("record_type") == "reading_partial":
                return row
        raise ReconciliationError(f"计量补读记录不存在: {reading_id}", component="recon")

    def _active_movements(self, shift_id: str) -> list[dict]:
        return [row for row in self._movements if row["shift_id"] == shift_id]

    def _allocate_mismatches(self, movements: Sequence[dict], unexplained_kg: float) -> list[dict]:
        all_mismatches = [
            row
            for row in movements
            if row.get("meter_variance_kg") is not None
            and abs(float(row["meter_variance_kg"])) > self.rule.mass_tolerance_kg
        ]
        candidates = [
            row
            for row in all_mismatches
            if (unexplained_kg > 0 and float(row["meter_variance_kg"]) > 0)
            or (unexplained_kg < 0 and float(row["meter_variance_kg"]) < 0)
        ]
        candidates.sort(key=lambda item: abs(float(item["meter_variance_kg"])), reverse=True)
        remaining = abs(unexplained_kg)
        result = []
        for row in candidates:
            if remaining <= 1e-9:
                break
            amount = min(remaining, abs(float(row["meter_variance_kg"])))
            result.append(
                {
                    "movement_id": row["movement_id"],
                    "ts": row["ts"],
                    "direction": row["direction"],
                    "meter_id": row["meter_id"],
                    "meter_variance_kg": _r(row["meter_variance_kg"]),
                    "allocated_kg": _r(amount if unexplained_kg > 0 else -amount),
                    "reference_id": row.get("reference_id"),
                    "reason": "meter_reading_mismatch",
                }
            )
            remaining -= amount
        # 表差方向不能直接解释班末盘亏时，也把异常流水单列为待核线索。
        if not result and all_mismatches:
            row = sorted(all_mismatches, key=lambda item: abs(float(item["meter_variance_kg"])), reverse=True)[0]
            result.append(
                {
                    "movement_id": row["movement_id"],
                    "ts": row["ts"],
                    "direction": row["direction"],
                    "meter_id": row["meter_id"],
                    "meter_variance_kg": _r(row["meter_variance_kg"]),
                    "allocated_kg": 0.0,
                    "reference_id": row.get("reference_id"),
                    "reason": "meter_reading_mismatch_candidate",
                }
            )
        if remaining > 1e-9:
            result.append(
                {
                    "movement_id": None,
                    "allocated_kg": _r(remaining if unexplained_kg > 0 else -remaining),
                    "reason": "unassigned_shift_difference",
                }
            )
        return result

    def close_shift(self, actual_pressure_mpa: float, actual_stock_kg: float | None = None) -> dict:
        """按班结口径结班，返回已结班或大额差异挂起结果。"""

        with self._lock:
            shift = self._require_active()
            shift_id = shift["shift_id"]
            unresolved = self._unresolved_readings(shift_id)
            if unresolved:
                raise ReconciliationError(
                    f"班内还有 {len(unresolved)} 笔计量补读部分失败，未取得完整读数不能结班",
                    component="recon",
                )

            rule = self._rule_book.at(self._clock.now())
            policy = TeamPolicy.from_mapping(shift["policy"])
            rows = self._active_movements(shift_id)
            physical_rows = [row for row in rows if row["direction"] != VARIANCE]
            received = _r(sum(row["mass_kg"] for row in physical_rows if row["direction"] == CHARGE))
            issued = _r(sum(row["mass_kg"] for row in physical_rows if row["direction"] == ISSUE))
            prior_adjustments = _r(sum(float(row["signed_mass_kg"]) for row in rows if row["direction"] == VARIANCE))
            opening = _r(shift["opening_stock_kg"])
            book_before_adjustment = _r(opening + received - issued + prior_adjustments)

            actual_pressure = float(actual_pressure_mpa)
            if actual_pressure < 0:
                raise ReconciliationError("盘点压力不能为负", component="recon")
            physical_stock = _r(actual_pressure * rule.kg_per_mpa if actual_stock_kg is None else actual_stock_kg)
            raw_difference = _r(physical_stock - book_before_adjustment)
            expected_pressure = _r(rule.expected_pressure_mpa)
            pressure_deviation = _r(actual_pressure - expected_pressure)
            # 压力带是独立的正常波动判据；账面质量不按压力反推。
            # 只有压力在带内且质量也在公差内，才属于完全正常。压力带内但质量确有
            # 短缺时，不能把实际亏空伪装成压力波动。
            pressure_in_band = abs(pressure_deviation) <= rule.pressure_tolerance_mpa + 1e-9
            mass_in_tolerance = abs(raw_difference) <= rule.mass_tolerance_kg + 1e-9
            pressure_normal = pressure_in_band and mass_in_tolerance
            pressure_explained = raw_difference if pressure_normal else 0.0
            unexplained = _r(raw_difference - pressure_explained)
            mismatches = self._allocate_mismatches(physical_rows, unexplained)
            large = abs(unexplained) > policy.large_threshold_kg + 1e-9 and not pressure_normal
            classification = (
                "normal_pressure_variation" if pressure_normal else ("large_difference" if large else "small_difference")
            )

            if large and policy.on_large == HOLD:
                result = {
                    "state": BLOCKED,
                    "shift_id": shift_id,
                    "team": shift["team"],
                    "rule_version": rule.version,
                    "opening_stock_kg": opening,
                    "received_kg": received,
                    "issued_kg": issued,
                    "book_stock_kg": book_before_adjustment,
                    "actual_stock_kg": physical_stock,
                    "actual_pressure_mpa": _r(actual_pressure),
                    "expected_pressure_mpa": expected_pressure,
                    "pressure_deviation_mpa": pressure_deviation,
                    "raw_difference_kg": raw_difference,
                    "pressure_explained_kg": _r(pressure_explained),
                    "unexplained_difference_kg": unexplained,
                    "classification": classification,
                    "policy": policy.as_dict(),
                    "mismatches": mismatches,
                }
                self._publish(
                    "shift_close_blocked",
                    severity="alarm",
                    message="差额超过班组阈值：整班挂起，未单列差异、未结班",
                    **{key: value for key, value in result.items() if key != "state"},
                )
                return result

            adjustment = None
            adjustment_amount = 0.0
            if not pressure_normal and abs(unexplained) > 1e-9:
                adjustment = self.post_movement(
                    VARIANCE,
                    unexplained,
                    meter_id="reconciliation",
                    pressure_before_mpa=expected_pressure,
                    pressure_after_mpa=actual_pressure,
                    reference_id=shift_id,
                    note="结班单列差异",
                )
                adjustment_amount = unexplained
            ending = _r(book_before_adjustment + adjustment_amount)
            close_payload = {
                "record_type": "shift_close",
                "record_seq": self._shift_record_seq + 1,
                "shift_number": int(shift["shift_number"]),
                "shift_id": shift_id,
                "team": shift["team"],
                "state": CLOSED,
                "opened_ts": shift["opened_ts"],
                "closed_ts": _r(self._clock.now()),
                "opening_stock_kg": opening,
                "received_kg": received,
                "issued_kg": issued,
                "adjustment_kg": _r(adjustment_amount),
                "book_stock_kg": book_before_adjustment,
                "actual_stock_kg": physical_stock,
                "ending_stock_kg": ending,
                "actual_pressure_mpa": _r(actual_pressure),
                "expected_pressure_mpa": expected_pressure,
                "pressure_deviation_mpa": pressure_deviation,
                "raw_difference_kg": raw_difference,
                "pressure_explained_kg": _r(pressure_explained),
                "unexplained_difference_kg": _r(adjustment_amount),
                "classification": classification,
                "rule_version": rule.version,
                "rule": rule.as_dict(),
                "policy": policy.as_dict(),
                "mismatches": mismatches,
                "variance_movement_id": None if adjustment is None else adjustment["movement_id"],
                "movement_count": len(rows) + (1 if adjustment is not None else 0),
            }
            record = self._append(self.shift_journal, self._shift_records, self._shift_hash, close_payload)
            self._shift_hash = record["hash"]
            self._shift_record_seq = record["record_seq"]
            self._active_shift_id = None
            self._persist_state()
            self._publish(
                "shift_closed",
                severity="warning" if adjustment is not None else "notice",
                message="班次已结，历史不可变",
                **{key: value for key, value in record.items() if key not in ("hash", "previous_hash")},
            )
            return copy.deepcopy(record)

    def current_report(self, actual_pressure_mpa: float | None = None) -> dict:
        """返回当前班收发存，供控制台展示。"""

        with self._lock:
            if self._active_shift_id is None:
                summaries = self.shift_summaries()
                return {
                    "settlement": SETTLEMENT,
                    "active_shift_id": None,
                    "received_kg": 0.0,
                    "issued_kg": 0.0,
                    "book_stock_kg": summaries[-1]["ending_stock_kg"] if summaries else 0.0,
                }
            shift = self._open_record(self._active_shift_id)
            rows = self._active_movements(shift["shift_id"])
            received = _r(sum(row["mass_kg"] for row in rows if row["direction"] == CHARGE))
            issued = _r(sum(row["mass_kg"] for row in rows if row["direction"] == ISSUE))
            adjustments = _r(sum(float(row["signed_mass_kg"]) for row in rows if row["direction"] == VARIANCE))
            book = _r(shift["opening_stock_kg"] + received - issued + adjustments)
            report = {
                "settlement": SETTLEMENT,
                "active_shift_id": shift["shift_id"],
                "team": shift["team"],
                "opened_ts": shift["opened_ts"],
                "opening_stock_kg": _r(shift["opening_stock_kg"]),
                "received_kg": received,
                "issued_kg": issued,
                "adjustment_kg": adjustments,
                "book_stock_kg": book,
                "movement_count": len(rows),
                "unresolved_reading_count": len(self._unresolved_readings(shift["shift_id"])),
                "rule_version": self._rule_book.at(self._clock.now()).version,
            }
            if actual_pressure_mpa is not None:
                current_rule = self._rule_book.at(self._clock.now())
                pressure = float(actual_pressure_mpa)
                report["actual_pressure_mpa"] = _r(pressure)
                report["pressure_implied_stock_kg"] = _r(pressure * current_rule.kg_per_mpa)
            return report

    def last_meter_reading(self, meter_id: str) -> float | None:
        """返回该物理累计表最近一次读数，跨班连续。"""

        with self._lock:
            return self._last_meter_reading(meter_id)

    def movements(self, shift_id: str | None = None, limit: int | None = None) -> list[dict]:
        with self._lock:
            rows = self._movements if shift_id is None else self._active_movements(shift_id)
            selected = list(rows)
            if limit is not None:
                selected = selected[-max(0, int(limit)) :]
            return copy.deepcopy(selected)

    def shifts(self) -> list[dict]:
        with self._lock:
            return self.shift_summaries()

    def readings(self, shift_id: str | None = None) -> list[dict]:
        with self._lock:
            if shift_id is None:
                return copy.deepcopy(self._reading_records)
            return copy.deepcopy(self._unresolved_readings(shift_id))

    def verify(self, audit_journal: JsonlJournal | None = None, bank_stored_kg: float | None = None) -> dict:
        """核对库存、流水、班结和审计的一致性。"""

        with self._lock:
            movement_ok, movement_error = self._verify_chain(self._movements)
            shift_ok, shift_error = self._verify_chain(self._shift_records)
            reading_ok, reading_error = self._verify_chain(self._reading_records)
            shift_checks = self._verify_shift_totals()
            active_report = self.current_report()
            stock_ok = True
            if bank_stored_kg is not None and self._active_shift_id is not None:
                stock_ok = abs(float(bank_stored_kg) - float(active_report["book_stock_kg"])) <= 1e-5
            audit_ok, audit_missing = self._verify_audit(audit_journal)
            return {
                "consistent": all(
                    (movement_ok, shift_ok, reading_ok, shift_checks["ok"], stock_ok, audit_ok)
                ),
                "settlement": SETTLEMENT,
                "inventory": {
                    "ok": stock_ok,
                    "bank_stored_kg": bank_stored_kg,
                    "ledger_stock_kg": active_report.get("book_stock_kg"),
                },
                "movement_journal": {"ok": movement_ok, "count": len(self._movements), "error": movement_error},
                "shift_journal": {"ok": shift_ok, "count": len(self._shift_records), "error": shift_error},
                "reading_journal": {"ok": reading_ok, "count": len(self._reading_records), "error": reading_error},
                "shifts": shift_checks,
                "audit": {"ok": audit_ok, "missing": audit_missing},
            }

    def _verify_chain(self, rows: Sequence[dict]) -> tuple[bool, str | None]:
        previous = "0"
        for row in rows:
            payload = {key: value for key, value in row.items() if key not in ("hash", "previous_hash")}
            record_type = str(row.get("record_type", ""))
            expected = _canonical_hash(record_type, payload, previous)
            if row.get("hash") != expected:
                return False, f"哈希前链在 {record_type} seq={row.get('seq', row.get('record_seq'))} 处不一致"
            previous = str(row.get("hash"))
        return True, None

    def _verify_shift_totals(self) -> dict:
        checks = []
        previous_ending = 0.0
        for index, shift in enumerate(self.shift_summaries()):
            if shift.get("state") == OPEN:
                checks.append({"shift_id": shift["shift_id"], "ok": True})
                continue
            rows = self._active_movements(shift["shift_id"])
            received = sum(row["mass_kg"] for row in rows if row["direction"] == CHARGE)
            issued = sum(row["mass_kg"] for row in rows if row["direction"] == ISSUE)
            signed_adjustment = sum(float(row["signed_mass_kg"]) for row in rows if row["direction"] == VARIANCE)
            ending = shift["opening_stock_kg"] + received - issued + signed_adjustment
            opening_ok = index == 0 or abs(shift["opening_stock_kg"] - previous_ending) <= 1e-5
            total_ok = abs(ending - shift["ending_stock_kg"]) <= 1e-5
            checks.append({"shift_id": shift["shift_id"], "ok": opening_ok and total_ok})
            previous_ending = shift["ending_stock_kg"]
        return {"ok": all(item["ok"] for item in checks), "checks": checks}

    def _verify_audit(self, audit_journal: JsonlJournal | None) -> tuple[bool, list[dict]]:
        if audit_journal is None:
            return True, []
        audit_rows = audit_journal.read_all()
        movement_keys = {
            (str(row.get("action")), str(row.get("payload", {}).get("movement_id")))
            for row in audit_rows
            if row.get("component") == "recon" and row.get("payload", {}).get("movement_id")
        }
        shift_open_keys = {
            str(row.get("payload", {}).get("shift_id"))
            for row in audit_rows
            if row.get("component") == "recon" and row.get("action") == "shift_opened"
        }
        shift_close_keys = {
            str(row.get("payload", {}).get("shift_id"))
            for row in audit_rows
            if row.get("component") == "recon" and row.get("action") == "shift_closed"
        }
        shift_blocked_keys = {
            str(row.get("payload", {}).get("shift_id"))
            for row in audit_rows
            if row.get("component") == "recon" and row.get("action") == "shift_close_blocked"
        }
        missing: list[dict] = []
        for row in self._movements:
            if (f"buffer_{row['direction']}", row["movement_id"]) not in movement_keys:
                missing.append({"kind": "movement", "id": row["movement_id"]})
        for row in self._shift_records:
            if row["record_type"] == "shift_open" and row["shift_id"] not in shift_open_keys:
                missing.append({"kind": "shift_open", "id": row["shift_id"]})
            if row["record_type"] == "shift_close" and row["shift_id"] not in shift_close_keys:
                missing.append({"kind": "shift_close", "id": row["shift_id"]})
        return not missing, missing

    def snapshot(self) -> dict:
        with self._lock:
            summaries = self.shift_summaries()
            active = next((row for row in reversed(summaries) if row.get("state") == OPEN), None)
            closed = [row for row in summaries if row.get("state") == CLOSED]
            return {
                "settlement": SETTLEMENT,
                "active_rule": self._rule_book.at(self._clock.now()).as_dict(),
                "rules": self.rules(),
                "default_team": self._default_team,
                "policies": self.policies(),
                "active_shift": copy.deepcopy(active),
                "current": self.current_report(),
                "last_closed_shift": copy.deepcopy(closed[-1]) if closed else None,
                "closed_shift_count": len(closed),
                "movement_count": len(self._movements),
                "partial_reading_count": len(self._unresolved_readings(self._active_shift_id))
                if self._active_shift_id
                else 0,
            }

    def _publish(self, action: str, severity: str = "notice", message: str = "", **payload: Any) -> None:
        clean = {key: value for key, value in payload.items() if value is not None}
        self._bus.publish(
            Event.of(
                "recon",
                "recon",
                action,
                station="",
                severity=severity,
                message=message,
                **clean,
            )
        )
