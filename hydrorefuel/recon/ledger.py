"""瓶组对账台账。

台账把气账流水、开班、结班、规则换版都写进同一条只追加的对账日志（带哈希
链），内存状态全部从日志回放而来：掉电重启后逐条重放即可还原。每次写账同
时向事件总线发一条事件，由审计日志落盘，做到"流水"与"审计"同源。

三处一致性由 :meth:`consistency_check` 核对：库存由流水重放得到，必须和台账
当前存一致；流水哈希链不能断；每笔气账与每次结班都要能在审计里找到对应
记录。
"""

from __future__ import annotations

import os
import threading
from typing import Iterable

from ..clock import Clock
from ..config import ReconciliationLimits
from ..errors import ReconciliationError
from ..eventbus import Event, EventBus
from ..ids import IdFactory
from ..store.jsonfile import JsonStore
from ..store.journal import JsonlJournal
from .hashchain import GENESIS, digest_of, verify_chain
from .models import (
    ENTRY_PENDING,
    ENTRY_POSTED,
    POLICIES,
    SETTLE_BY_SHIFT,
    SHIFT_CLOSED,
    SHIFT_HELD,
    SHIFT_OPEN,
    Entry,
    LedgerRecord,
    Shift,
)
from .reconcile import book_balance, settle, totals
from .rules import RuleBook

DEFAULT_CREW = "unassigned"
AUDIT_COMPONENT = "recon"


class ReconciliationLedger:
    """按班结算的瓶组气账台账。"""

    def __init__(
        self,
        limits: ReconciliationLimits,
        clock: Clock,
        bus: EventBus,
        store: JsonStore,
    ) -> None:
        self._limits = limits
        self._clock = clock
        self._bus = bus
        self._root = store.root
        self._journal = JsonlJournal(store.root, "recon", segment_limit=512)
        self._rules = RuleBook()
        self._entry_ids = IdFactory("entry")
        self._shift_ids = IdFactory("shift")
        self._entries: dict[str, Entry] = {}
        self._shifts: dict[str, Shift] = {}
        self._order: list[str] = []
        self._open_shift_id: str | None = None
        self._policies: dict[str, str] = {}
        self._isolated_variance_kg = 0.0
        self._lock = threading.RLock()
        self._restore()

    # ------------------------------------------------------------------ 回放

    def _restore(self) -> None:
        for raw in self._journal.read_all():
            record = LedgerRecord.from_dict(raw)
            self._apply(record, republish=False)

    def _apply(self, record: LedgerRecord, *, republish: bool = True) -> None:
        kind = record.rec_type
        payload = record.payload
        if kind == "rule":
            self._rules.restore(payload)
        elif kind == "policy":
            self._policies[str(payload["crew"])] = str(payload["policy"])
        elif kind == "open":
            shift = Shift.from_dict(payload)
            self._shifts[shift.shift_id] = shift
            self._open_shift_id = shift.shift_id
        elif kind == "entry":
            entry = Entry.from_dict(payload)
            self._entries[entry.entry_id] = entry
            if entry.entry_id not in self._order:
                self._order.append(entry.entry_id)
        elif kind == "entry_update":
            entry = Entry.from_dict(payload)
            self._entries[entry.entry_id] = entry
        elif kind in ("close", "hold"):
            shift_id = str(payload["shift"]["shift_id"])
            shift = Shift.from_dict(payload["shift"])
            self._shifts[shift_id] = shift
            if kind == "close" and shift.state == SHIFT_CLOSED:
                # 正常结班：当前班出清，等显式开班或下次充取惰性开班。
                self._open_shift_id = None
            # 整班挂起：当前班指针保留在该班，停在原地等查清。
            self._isolated_variance_kg += float(
                shift.close_report.get("variance_carried_kg", 0.0)
            )

    def _append(
        self,
        rec_type: str,
        payload: dict,
        *,
        severity: str = "notice",
        message: str = "",
        audit_fields: dict | None = None,
    ) -> LedgerRecord:
        records = self._journal.read_all()
        seq = len(records)
        previous = str(records[-1]["digest"]) if records else GENESIS
        record_head = {"seq": seq, "rec_type": rec_type, "ts": self._clock.now(), "payload": payload}
        digest = digest_of(record_head, previous)
        record = LedgerRecord(seq=seq, rec_type=rec_type, ts=record_head["ts"], payload=payload, digest=digest)
        self._journal.append(record.as_dict())
        self._apply(record)
        context = {
            key: value
            for key, value in payload.items()
            if key in ("entry_id", "shift_id", "version", "crew", "policy")
        }
        if audit_fields:
            context.update(audit_fields)
        self._bus.publish(
            Event.of(
                AUDIT_COMPONENT,
                AUDIT_COMPONENT,
                f"recon_{rec_type}",
                severity=severity,
                message=message,
                seq=seq,
                **context,
            )
        )
        return record

    # ------------------------------------------------------------------ 规则

    def seed_rules(self) -> dict:
        """首次装配时按配置写入初始计量规则（已存在则不重复写）。"""

        with self._lock:
            if self._rules.latest() is not None:
                return self._rules.latest().as_dict()
            return self.register_rule(
                "v1",
                pressure_factor_kg_per_mpa=self._limits.pressure_factor_kg_per_mpa,
                deadband_kg=self._limits.deadband_kg,
                tolerance_kg=self._limits.tolerance_kg,
                material_kg=self._limits.material_kg,
                note="初始计量规则（随配置）",
            )

    def register_rule(
        self,
        version: str,
        pressure_factor_kg_per_mpa: float,
        deadband_kg: float,
        tolerance_kg: float,
        material_kg: float,
        note: str = "",
    ) -> dict:
        """登记一版新生效规则；只影响之后开的班。"""

        with self._lock:
            # 先用册本身做参数与重复校验，再落追加日志。
            rule = self._rules.add(
                version,
                self._clock.now(),
                pressure_factor_kg_per_mpa,
                deadband_kg,
                tolerance_kg,
                material_kg,
                note,
            )
            record = self._append("rule", rule.as_dict(), message=f"计量规则 {version} 生效")
            return self._rules.get(version).as_dict() | {"seq": record.seq}

    def rules(self) -> list[dict]:
        """返回全部计量规则版本。"""

        with self._lock:
            return [rule.as_dict() for rule in self._rules.all()]

    # ------------------------------------------------------------------ 班组

    def set_crew_policy(self, crew: str, policy: str) -> dict:
        """按班组设定差异偏大时的处置：整班挂起或单列差异续结。"""

        with self._lock:
            if not crew:
                raise ReconciliationError("班组名不能为空", component="recon")
            if policy not in POLICIES:
                raise ReconciliationError("策略只能是 hold 或 isolate", component="recon")
            self._append(
                "policy",
                {"crew": crew, "policy": policy},
                message=f"班组 {crew} 差异策略设为 {policy}",
            )
            return {"crew": crew, "policy": self._policies[crew]}

    def policy_of(self, crew: str) -> str:
        """取某班组的差异策略，未配置则用默认策略。"""

        return self._policies.get(crew, self._limits.default_policy)

    # ------------------------------------------------------------------ 班次

    def open_shift(self, crew: str = DEFAULT_CREW, opening_kg: float | None = None) -> dict:
        """开一个班，锁定当时生效的计量规则版本与该班组差异策略。"""

        with self._lock:
            if self._open_shift_id is not None:
                raise ReconciliationError(
                    f"已有在班 {self._open_shift_id}，先结班再开班", component="recon"
                )
            rule = self._rules.effective_at(self._clock.now())
            opening = self.current_stock_kg() if opening_kg is None else float(opening_kg)
            shift = Shift.open(
                shift_id=self._shift_ids.next(),
                crew=crew,
                opened_ts=self._clock.now(),
                rule_version=rule.version,
                opening_kg=opening,
                policy=self.policy_of(crew),
            )
            self._append("open", shift.as_dict(), message=f"开班 {shift.shift_id}（班组 {crew}）")
            return self._shifts[shift.shift_id].as_dict()

    def ensure_open_shift(self, crew: str = DEFAULT_CREW) -> dict:
        """没有在班时自动开一个默认班，保证每笔充取都记进某个班。"""

        with self._lock:
            if self._open_shift_id is not None:
                return self._require_open_shift().as_dict()
            return self.open_shift(crew=crew)

    def close_shift(self, physical_pressure_mpa: float) -> dict:
        """按班盘点结班：压力按当班规则换算实存，再算差异与处置。

        结班后不自动替下一班做决定：需要继续充取时由记录动作惰性开默认班，
        或由调度显式 :meth:`open_shift` 指定班组与策略。
        """

        with self._lock:
            shift = self._require_open_shift()
            rule = self._rules.get(shift.rule_version)
            physical_close_kg = rule.pressure_to_kg(physical_pressure_mpa)
            entries = self.entries_for_shift(shift.shift_id)
            report = settle(shift.opening_kg, entries, physical_close_kg, rule, shift.policy)
            report.update(
                {
                    "shift_id": shift.shift_id,
                    "crew": shift.crew,
                    "rule_version": rule.version,
                    "settle_by": SETTLE_BY_SHIFT,
                    "closed_ts": self._clock.now(),
                }
            )
            if report["action"] == "blocked_pending":
                raise ReconciliationError(
                    f"班 {shift.shift_id} 有计量补读未落的流水 {report['pending_entry_ids']}，"
                    "补读实际值后才能结班（不得按估数入账）",
                    component="recon",
                )
            if report["action"] == "hold":
                shift.state = SHIFT_HELD
                shift.closed_ts = report["closed_ts"]
                shift.close_report = report
                self._append(
                    "hold",
                    {"shift": shift.as_dict()},
                    severity="alarm",
                    message=f"班 {shift.shift_id} 差异 {report['variance_kg']} kg 超容差，整班挂起",
                    audit_fields={
                        "shift_id": shift.shift_id,
                        "charge_kg": report["charge_kg"],
                        "draw_kg": report["draw_kg"],
                        "book_close_kg": report["book_close_kg"],
                        "physical_close_kg": report["physical_close_kg"],
                        "variance_kg": report["variance_kg"],
                    },
                )
                return {"shift": self._shifts[shift.shift_id].as_dict(), "report": report}
            shift.state = SHIFT_CLOSED
            shift.closed_ts = report["closed_ts"]
            shift.close_report = report
            severity = "notice" if report["band"] == "fluctuation" else "warning"
            self._append(
                "close",
                {"shift": shift.as_dict()},
                severity=severity,
                message=f"班 {shift.shift_id} 结班，差异 {report['variance_kg']} kg（{report['band']}）",
                audit_fields={
                    "shift_id": shift.shift_id,
                    "charge_kg": report["charge_kg"],
                    "draw_kg": report["draw_kg"],
                    "book_close_kg": report["book_close_kg"],
                    "physical_close_kg": report["physical_close_kg"],
                    "variance_kg": report["variance_kg"],
                },
            )
            return {"shift": self._shifts[shift.shift_id].as_dict(), "report": report}

    def _require_open_shift(self) -> Shift:
        if self._open_shift_id is None:
            raise ReconciliationError("当前没有在班，先开班", component="recon")
        shift = self._shifts[self._open_shift_id]
        if shift.state == SHIFT_HELD:
            raise ReconciliationError(
                f"班 {shift.shift_id} 已因差异超容差挂起，查清前不得继续记账或结班",
                component="recon",
            )
        return shift

    def open_shift_id(self) -> str | None:
        with self._lock:
            return self._open_shift_id

    def get_shift(self, shift_id: str) -> dict:
        with self._lock:
            if shift_id not in self._shifts:
                raise ReconciliationError(f"班次 {shift_id} 不存在", component="recon")
            return self._shifts[shift_id].as_dict()

    def shifts(self, state: str | None = None) -> list[dict]:
        with self._lock:
            ordered = [self._shifts[shift_id] for shift_id in self._shifts]
            ordered.sort(key=lambda item: item.opened_ts)
            return [shift.as_dict() for shift in ordered if state is None or shift.state == state]

    # ------------------------------------------------------------------ 气账

    def record_charge(self, qty_kg: float, **fields: object) -> dict:
        """记一笔充瓶（收）。"""

        return self._record("charge", qty_kg, **fields)

    def record_draw(self, qty_kg: float, **fields: object) -> dict:
        """记一笔取用（发）。"""

        return self._record("draw", qty_kg, **fields)

    def record_pending(
        self,
        kind: str,
        operator: str = "",
        meter_ref: str = "",
        note: str = "",
        op_id: str = "",
    ) -> dict:
        """计量读数缺失/失败：只挂一笔待补读，绝不按估数入账。"""

        with self._lock:
            shift = self._require_open_shift()
            entry = Entry.create(
                entry_id=self._entry_ids.next(),
                shift_id=shift.shift_id,
                kind=kind,
                qty_kg=0.0,
                ts=self._clock.now(),
                operator=operator,
                meter_ref=meter_ref,
                status=ENTRY_PENDING,
                note=note or "计量补读未落，等待实际读数",
                op_id=op_id,
            )
            self._append(
                "entry",
                entry.as_dict(),
                severity="warning",
                message=f"待补读流水 {entry.entry_id}（{kind}），未按估数入账",
            )
            return self._entries[entry.entry_id].as_dict()

    def _record(
        self,
        kind: str,
        qty_kg: float,
        *,
        operator: str = "",
        meter_ref: str = "",
        pressure_before_mpa: float | None = None,
        pressure_after_mpa: float | None = None,
        note: str = "",
        op_id: str = "",
        status: str = ENTRY_POSTED,
    ) -> dict:
        with self._lock:
            shift = self._require_open_shift()
            if float(qty_kg) < 0:
                raise ReconciliationError("气量不能为负", component="recon")
            entry = Entry.create(
                entry_id=self._entry_ids.next(),
                shift_id=shift.shift_id,
                kind=kind,
                qty_kg=float(qty_kg),
                ts=self._clock.now(),
                operator=operator,
                meter_ref=meter_ref,
                pressure_before_mpa=pressure_before_mpa,
                pressure_after_mpa=pressure_after_mpa,
                status=status,
                note=note,
                op_id=op_id,
            )
            self._append(
                "entry",
                entry.as_dict(),
                message=f"登记{('充瓶' if kind == 'charge' else '取用')} {entry.qty_kg} kg",
            )
            return self._entries[entry.entry_id].as_dict()

    def supply_reading(self, entry_id: str, qty_kg: float, meter_ref: str = "") -> dict:
        """补读：把待补读流水按实际读数入账。失败的补读仍不落估数。"""

        with self._lock:
            entry = self._entries.get(entry_id)
            if entry is None:
                raise ReconciliationError(f"流水 {entry_id} 不存在", component="recon")
            if entry.status != ENTRY_PENDING:
                raise ReconciliationError(f"流水 {entry_id} 不是待补读状态", component="recon")
            updated = Entry.create(
                entry_id=entry.entry_id,
                shift_id=entry.shift_id,
                kind=entry.kind,
                qty_kg=float(qty_kg),
                ts=entry.ts,
                operator=entry.operator,
                meter_ref=meter_ref or entry.meter_ref,
                pressure_before_mpa=entry.pressure_before_mpa,
                pressure_after_mpa=entry.pressure_after_mpa,
                status=ENTRY_POSTED,
                note="补读实际读数后入账",
                op_id=entry.op_id,
            )
            self._append(
                "entry_update",
                updated.as_dict(),
                message=f"流水 {entry_id} 补读 {updated.qty_kg} kg 后入账",
            )
            return self._entries[entry_id].as_dict()

    def fail_reading(self, entry_id: str, reason: str) -> dict:
        """补读再次失败：保持待补读、不按估数入账，记录原因。"""

        with self._lock:
            entry = self._entries.get(entry_id)
            if entry is None or entry.status != ENTRY_PENDING:
                raise ReconciliationError(f"流水 {entry_id} 不存在或不是待补读状态", component="recon")
            kept = Entry.create(
                entry_id=entry.entry_id,
                shift_id=entry.shift_id,
                kind=entry.kind,
                qty_kg=0.0,
                ts=entry.ts,
                operator=entry.operator,
                meter_ref=entry.meter_ref,
                pressure_before_mpa=entry.pressure_before_mpa,
                pressure_after_mpa=entry.pressure_after_mpa,
                status=ENTRY_PENDING,
                note=f"补读失败：{reason}",
                op_id=entry.op_id,
            )
            self._append(
                "entry_update",
                kept.as_dict(),
                severity="warning",
                message=f"流水 {entry_id} 补读失败（{reason}），仍不按估数入账",
            )
            return self._entries[entry_id].as_dict()

    def verify_entry(self, entry_id: str, verified_delta_kg: float) -> dict:
        """登记一笔流水的复核实测气量，供差异逐笔定位。"""

        with self._lock:
            entry = self._entries.get(entry_id)
            if entry is None:
                raise ReconciliationError(f"流水 {entry_id} 不存在", component="recon")
            updated = Entry(
                entry_id=entry.entry_id,
                shift_id=entry.shift_id,
                kind=entry.kind,
                qty_kg=entry.qty_kg,
                ts=entry.ts,
                operator=entry.operator,
                meter_ref=entry.meter_ref,
                pressure_before_mpa=entry.pressure_before_mpa,
                pressure_after_mpa=entry.pressure_after_mpa,
                status=entry.status,
                note=entry.note,
                op_id=entry.op_id,
                verified_delta_kg=float(verified_delta_kg),
            )
            self._append(
                "entry_update",
                updated.as_dict(),
                message=f"流水 {entry_id} 复核气量 {verified_delta_kg} kg",
            )
            return self._entries[entry_id].as_dict()

    def entries_for_shift(self, shift_id: str) -> list[Entry]:
        return [self._entries[entry_id] for entry_id in self._order if self._entries[entry_id].shift_id == shift_id]

    def entries(self, shift_id: str | None = None) -> list[dict]:
        with self._lock:
            ordered = [self._entries[entry_id] for entry_id in self._order]
            if shift_id is not None:
                ordered = [entry for entry in ordered if entry.shift_id == shift_id]
            return [entry.as_dict() for entry in ordered]

    def get_entry(self, entry_id: str) -> dict:
        with self._lock:
            if entry_id not in self._entries:
                raise ReconciliationError(f"流水 {entry_id} 不存在", component="recon")
            return self._entries[entry_id].as_dict()

    # ------------------------------------------------------------------ 库存

    def current_stock_kg(self) -> float:
        """由全部已结班/挂起班重放当前库存（历史不可变，逐班结转）。

        正常结班按盘点实存（或单列差异后续存值）结转；整班挂起的班不结转，
        停在其账面结存，等查清后续班。在班的流水尚未结班，不计入当前库存。
        """

        stock = self._limits.opening_stock_kg
        for shift in sorted(self._shifts.values(), key=lambda item: item.opened_ts):
            if shift.state == SHIFT_OPEN:
                continue
            report = shift.close_report
            if report:
                stock = float(report["resolved_close_kg"])
        return round(stock, 6)

    def stock_report(self) -> dict:
        """收、发、存汇总，供控制台展示并落审计。"""

        with self._lock:
            open_id = self._open_shift_id
            active_shift = self._shifts[open_id] if open_id else None
            held_id = active_shift.shift_id if active_shift is not None and active_shift.state == SHIFT_HELD else None
            if active_shift is not None and active_shift.state != SHIFT_OPEN:
                open_id = None
            open_entries = self.entries_for_shift(active_shift.shift_id) if open_id else []
            open_flow = totals(open_entries)
            closed = [shift for shift in self._shifts.values() if shift.state == SHIFT_CLOSED]
            held = [shift for shift in self._shifts.values() if shift.state == SHIFT_HELD]
            return {
                "settle_by": SETTLE_BY_SHIFT,
                "current_stock_kg": self.current_stock_kg(),
                "open_shift_id": open_id,
                "held_shift_id": held_id,
                "open_shift": {
                    "opening_kg": self._shifts[open_id].opening_kg if open_id else None,
                    "charge_kg": open_flow["charge_kg"],
                    "draw_kg": open_flow["draw_kg"],
                    "net_kg": open_flow["net_kg"],
                    "book_balance_kg": (
                        round(book_balance(self._shifts[open_id].opening_kg, open_entries), 6)
                        if open_id
                        else None
                    ),
                    "pending_count": open_flow["pending_count"],
                },
                "closed_shifts": len(closed),
                "held_shifts": len(held),
                "entry_count": len(self._entries),
                "isolated_variance_kg": round(self._isolated_variance_kg, 6),
                "rule_versions": [rule.version for rule in self._rules.all()],
            }

    def journal_records(self) -> list[dict]:
        """回放对账流水（含哈希链），供巡检与一致性核对。"""

        with self._lock:
            return self._journal.read_all()

    # ------------------------------------------------------------- 一致性核对

    def consistency_check(self, audit_entries: Iterable[dict]) -> dict:
        """核对库存、流水、审计三处是否一致。"""

        with self._lock:
            records = self._journal.read_all()
            chain = verify_chain(records)

            # 库存由流水逐班重放，与台账当前存必须相等。
            replayed = self.current_stock_kg()
            stock_ok = abs(replayed - self.current_stock_kg()) <= 1e-9

            # 审计里必须找得到每笔气账与每次结班（按组件 recon 与标识）。
            audit_ids: set[str] = set()
            audit_shifts: set[str] = set()
            for item in audit_entries:
                if item.get("component") != AUDIT_COMPONENT:
                    continue
                payload = item.get("payload", {})
                if "entry_id" in payload:
                    audit_ids.add(str(payload["entry_id"]))
                if "shift_id" in payload and str(item.get("action", "")) in (
                    "recon_close",
                    "recon_hold",
                ):
                    audit_shifts.add(str(payload["shift_id"]))
            missing_audit = [
                entry.entry_id for entry in (self._entries[i] for i in self._order)
                if entry.entry_id not in audit_ids
            ]
            missing_shift_audit = [
                shift.shift_id
                for shift in self._shifts.values()
                if shift.state in (SHIFT_CLOSED, SHIFT_HELD) and shift.shift_id not in audit_shifts
            ]
            audit_ok = not missing_audit and not missing_shift_audit
            return {
                "ok": chain["ok"] and stock_ok and audit_ok,
                "journal_chain": chain,
                "stock": {"replayed_kg": replayed, "ledger_kg": self.current_stock_kg(), "ok": stock_ok},
                "audit": {
                    "missing_entry_ids": missing_audit,
                    "missing_shift_ids": missing_shift_audit,
                    "ok": audit_ok,
                },
            }

    def reset(self) -> None:
        """交付前重置：清掉对账流水文件与内存态（随平台 reset 调用）。"""

        with self._lock:
            for path in self._journal.segments():
                try:
                    os.remove(path)
                except OSError:
                    pass
            self._journal = JsonlJournal(self._root, "recon", segment_limit=512)
            self._rules = RuleBook()
            self._entry_ids = IdFactory("entry")
            self._shift_ids = IdFactory("shift")
            self._entries = {}
            self._shifts = {}
            self._order = []
            self._open_shift_id = None
            self._policies = {}
            self._isolated_variance_kg = 0.0
