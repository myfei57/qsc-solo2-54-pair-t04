"""计量规则版本。

规则一旦发布即不可修改。流水携带发生时已生效的规则版本；已结班次保存结班
时使用的规则快照，后续即使压力公差、质量公差或换算系数调整，也必须按当时那一
版解释，不能用新口径重算历史班。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class MeasurementRule:
    """一版固定的气量计量口径。"""

    version: str
    effective_ts: float
    settlement: str
    mass_tolerance_kg: float
    pressure_tolerance_mpa: float
    kg_per_mpa: float
    expected_pressure_mpa: float = 42.0
    basis: str = "shift"

    def pressure_tolerance_kg(self) -> float:
        """允许的正常压力波动折算质量。"""

        return self.pressure_tolerance_mpa * self.kg_per_mpa

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "effective_ts": round(self.effective_ts, 3),
            "settlement": self.settlement,
            "basis": self.basis,
            "mass_tolerance_kg": round(self.mass_tolerance_kg, 6),
            "pressure_tolerance_mpa": round(self.pressure_tolerance_mpa, 6),
            "kg_per_mpa": round(self.kg_per_mpa, 6),
            "expected_pressure_mpa": round(self.expected_pressure_mpa, 6),
            "pressure_tolerance_kg": round(self.pressure_tolerance_kg(), 6),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MeasurementRule":
        settlement = str(raw.get("settlement", raw.get("basis", "shift")))
        return cls(
            version=str(raw["version"]),
            effective_ts=float(raw.get("effective_ts", 0.0)),
            settlement=settlement,
            mass_tolerance_kg=float(raw["mass_tolerance_kg"]),
            pressure_tolerance_mpa=float(raw["pressure_tolerance_mpa"]),
            kg_per_mpa=float(raw["kg_per_mpa"]),
            expected_pressure_mpa=float(raw.get("expected_pressure_mpa", 42.0)),
            basis=settlement,
        )


class RuleBook:
    """只追加的计量规则册。"""

    def __init__(self, initial: MeasurementRule) -> None:
        self._rules: list[MeasurementRule] = [initial]

    @property
    def current(self) -> MeasurementRule:
        return self._rules[-1]

    def versions(self) -> list[str]:
        return [rule.version for rule in self._rules]

    def get(self, version: str) -> MeasurementRule:
        for rule in reversed(self._rules):
            if rule.version == version:
                return rule
        raise KeyError(f"计量规则版本不存在: {version}")

    def at(self, ts: float) -> MeasurementRule:
        """返回指定时刻已经生效的最新规则；未来版本不会提前生效。"""

        selected = self._rules[0]
        for rule in self._rules:
            if rule.effective_ts <= ts:
                selected = rule
        return selected

    def publish(
        self,
        version: str,
        effective_ts: float,
        mass_tolerance_kg: float,
        pressure_tolerance_mpa: float,
        kg_per_mpa: float,
        expected_pressure_mpa: float = 42.0,
        *,
        settlement: str = "shift",
    ) -> MeasurementRule:
        """发布新版本；结算口径固定为班结，不允许借改版规则切成日结。"""

        if not version:
            raise ValueError("规则版本不能为空")
        if version in self.versions():
            raise ValueError(f"计量规则版本已存在: {version}")
        if settlement != "shift":
            raise ValueError("结算口径已定为班结，不能改成其他口径")
        if mass_tolerance_kg < 0:
            raise ValueError("质量公差不能为负")
        if pressure_tolerance_mpa < 0:
            raise ValueError("压力公差不能为负")
        if kg_per_mpa <= 0:
            raise ValueError("压力质量换算系数必须为正")
        if expected_pressure_mpa <= 0:
            raise ValueError("正常压力基准必须为正")
        rule = MeasurementRule(
            version=version,
            effective_ts=float(effective_ts),
            settlement="shift",
            mass_tolerance_kg=float(mass_tolerance_kg),
            pressure_tolerance_mpa=float(pressure_tolerance_mpa),
            kg_per_mpa=float(kg_per_mpa),
            expected_pressure_mpa=float(expected_pressure_mpa),
        )
        self._rules.append(rule)
        return rule

    def as_dicts(self) -> list[dict]:
        return [rule.as_dict() for rule in self._rules]

    @classmethod
    def from_dicts(cls, rows: list[Mapping[str, Any]]) -> "RuleBook":
        if not rows:
            raise ValueError("计量规则册不能为空")
        ordered = sorted(rows, key=lambda row: float(row.get("effective_ts", 0.0)))
        book = cls(MeasurementRule.from_dict(ordered[0]))
        for row in ordered[1:]:
            rule = MeasurementRule.from_dict(row)
            if rule.settlement != "shift":
                raise ValueError("历史计量规则的结算口径必须保持班结")
            book._rules.append(rule)
        return book
