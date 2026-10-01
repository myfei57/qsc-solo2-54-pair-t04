"""对账数据模型：流水记录、气账流水、班次。

三类对象共用一个追加日志：

- :class:`LedgerRecord` 是日志信封（序号、类型、时间、负载、哈希链摘要）；
- :class:`Entry` 是一笔充瓶或取用气账，``posted`` 才计入收发存，
  ``pending_read`` 表示计量补读未落（部分失败，不按估数入账）；
- :class:`Shift` 是一个班次，``open`` 在结、``closed`` 已结（历史不可变）、
  ``held`` 差异过大被挂起。

班组差异策略 ``hold`` 表示差异偏大时整班挂起、不结转；``isolate`` 表示把
差异单列挂账后照常结班续存。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

ENTRY_POSTED = "posted"
ENTRY_PENDING = "pending_read"
ENTRY_VOID = "void"

SHIFT_OPEN = "open"
SHIFT_CLOSED = "closed"
SHIFT_HELD = "held"

POLICY_HOLD = "hold"
POLICY_ISOLATE = "isolate"
POLICIES = (POLICY_HOLD, POLICY_ISOLATE)

BAND_FLUCTUATION = "fluctuation"
BAND_TOLERANCE = "tolerance"
BAND_MATERIAL = "material"

# 结班粒度写死：按班结，不按日结。
SETTLE_BY_SHIFT = "shift"


@dataclass(frozen=True)
class LedgerRecord:
    """对账日志里的一条追加记录。"""

    seq: int
    rec_type: str
    ts: float
    payload: dict
    digest: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "LedgerRecord":
        return cls(
            seq=int(raw["seq"]),
            rec_type=str(raw["rec_type"]),
            ts=float(raw["ts"]),
            payload=dict(raw.get("payload", {})),
            digest=str(raw.get("digest", "")),
        )

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "rec_type": self.rec_type,
            "ts": round(self.ts, 3),
            "payload": self.payload,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class Entry:
    """一笔充瓶（收）或取用（发）气账。"""

    entry_id: str
    shift_id: str
    kind: str
    qty_kg: float
    ts: float
    operator: str
    meter_ref: str
    pressure_before_mpa: float | None
    pressure_after_mpa: float | None
    status: str
    note: str
    op_id: str
    # 盘点/复核对这笔的独立计量，缺省表示这笔当时没有逐笔复核。
    verified_delta_kg: float | None = None

    @classmethod
    def create(
        cls,
        entry_id: str,
        shift_id: str,
        kind: str,
        qty_kg: float,
        ts: float,
        operator: str = "",
        meter_ref: str = "",
        pressure_before_mpa: float | None = None,
        pressure_after_mpa: float | None = None,
        status: str = ENTRY_POSTED,
        note: str = "",
        op_id: str = "",
        verified_delta_kg: float | None = None,
    ) -> "Entry":
        if kind not in ("charge", "draw"):
            raise ValueError("气账类型只能是 charge 或 draw")
        return cls(
            entry_id=entry_id,
            shift_id=shift_id,
            kind=kind,
            qty_kg=round(float(qty_kg), 6),
            ts=float(ts),
            operator=operator,
            meter_ref=meter_ref,
            pressure_before_mpa=pressure_before_mpa,
            pressure_after_mpa=pressure_after_mpa,
            status=status,
            note=note,
            op_id=op_id,
            verified_delta_kg=None if verified_delta_kg is None else round(float(verified_delta_kg), 6),
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Entry":
        verified = raw.get("verified_delta_kg")
        return cls(
            entry_id=str(raw["entry_id"]),
            shift_id=str(raw["shift_id"]),
            kind=str(raw["kind"]),
            qty_kg=float(raw["qty_kg"]),
            ts=float(raw["ts"]),
            operator=str(raw.get("operator", "")),
            meter_ref=str(raw.get("meter_ref", "")),
            pressure_before_mpa=raw.get("pressure_before_mpa"),
            pressure_after_mpa=raw.get("pressure_after_mpa"),
            status=str(raw.get("status", ENTRY_POSTED)),
            note=str(raw.get("note", "")),
            op_id=str(raw.get("op_id", "")),
            verified_delta_kg=None if verified is None else float(verified),
        )

    def as_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "shift_id": self.shift_id,
            "kind": self.kind,
            "qty_kg": self.qty_kg,
            "ts": round(self.ts, 3),
            "operator": self.operator,
            "meter_ref": self.meter_ref,
            "pressure_before_mpa": self.pressure_before_mpa,
            "pressure_after_mpa": self.pressure_after_mpa,
            "status": self.status,
            "note": self.note,
            "op_id": self.op_id,
            "verified_delta_kg": self.verified_delta_kg,
        }

    def signed(self) -> float:
        """按收发方向带号：充瓶为正，取用为负。"""

        return self.qty_kg if self.kind == "charge" else -self.qty_kg


@dataclass
class Shift:
    """一个班次及其结班口径。"""

    shift_id: str
    crew: str
    opened_ts: float
    rule_version: str
    state: str = SHIFT_OPEN
    closed_ts: float | None = None
    policy: str = POLICY_HOLD
    opening_kg: float = 0.0
    close_report: dict = field(default_factory=dict)

    @classmethod
    def open(
        cls,
        shift_id: str,
        crew: str,
        opened_ts: float,
        rule_version: str,
        opening_kg: float,
        policy: str,
    ) -> "Shift":
        if policy not in POLICIES:
            raise ValueError("班组策略只能是 hold 或 isolate")
        return cls(
            shift_id=shift_id,
            crew=crew,
            opened_ts=float(opened_ts),
            rule_version=rule_version,
            state=SHIFT_OPEN,
            closed_ts=None,
            policy=policy,
            opening_kg=round(float(opening_kg), 6),
            close_report={},
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Shift":
        return cls(
            shift_id=str(raw["shift_id"]),
            crew=str(raw["crew"]),
            opened_ts=float(raw["opened_ts"]),
            rule_version=str(raw["rule_version"]),
            state=str(raw.get("state", SHIFT_OPEN)),
            closed_ts=raw.get("closed_ts"),
            policy=str(raw.get("policy", POLICY_HOLD)),
            opening_kg=float(raw.get("opening_kg", 0.0)),
            close_report=dict(raw.get("close_report", {})),
        )

    def as_dict(self) -> dict:
        return {
            "shift_id": self.shift_id,
            "crew": self.crew,
            "opened_ts": round(self.opened_ts, 3),
            "rule_version": self.rule_version,
            "state": self.state,
            "closed_ts": self.closed_ts,
            "policy": self.policy,
            "opening_kg": self.opening_kg,
            "close_report": self.close_report,
        }
