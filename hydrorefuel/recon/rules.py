"""计量规则版本。

盘点只能给出瓶组压力，气量要靠一版计量规则换算：``气量 = 压力 × 系数``。
正常的压力波动（温漂、仪表抖动）必须被"死区"吃掉，不能算成亏空；超过死区
但在容差内的算计量误差；再大才是要追笔的实质差异。

规则只追加不改写，每条带版本号与生效时间。班一旦开出，就锁定开班那一刻的
规则版本，本班收、发、存与差额全部按那一版解释；以后再换版，已结班仍按旧
版重放，口径不会漂移。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..errors import ConfigError

KIND_CHARGE = "charge"
KIND_DRAW = "draw"
KIND_OPEN = "open"
KIND_CLOSE = "close"


@dataclass(frozen=True)
class MeteringRule:
    """一版不可变的计量与容差规则。"""

    version: str
    effective_ts: float
    pressure_factor_kg_per_mpa: float
    deadband_kg: float
    tolerance_kg: float
    material_kg: float
    note: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "MeteringRule":
        """从落盘字段还原一版规则。"""

        return cls(
            version=str(raw["version"]),
            effective_ts=float(raw["effective_ts"]),
            pressure_factor_kg_per_mpa=float(raw["pressure_factor_kg_per_mpa"]),
            deadband_kg=float(raw["deadband_kg"]),
            tolerance_kg=float(raw["tolerance_kg"]),
            material_kg=float(raw["material_kg"]),
            note=str(raw.get("note", "")),
        )

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "effective_ts": round(self.effective_ts, 3),
            "pressure_factor_kg_per_mpa": self.pressure_factor_kg_per_mpa,
            "deadband_kg": self.deadband_kg,
            "tolerance_kg": self.tolerance_kg,
            "material_kg": self.material_kg,
            "note": self.note,
        }

    def pressure_to_kg(self, pressure_mpa: float) -> float:
        """按本版系数把瓶组压力换算成气量。"""

        return max(0.0, float(pressure_mpa)) * self.pressure_factor_kg_per_mpa

    def classify(self, diff_kg: float) -> str:
        """按本版容差把差异分成三档。

        ``fluctuation`` 落在死区内，视为正常压力波动；``tolerance`` 超过死区
        但仍在容差内，算计量误差（不追笔）；``material`` 超出容差，是必须定位
        到具体流水的实质差异。
        """

        amount = abs(float(diff_kg))
        if amount <= self.deadband_kg + 1e-9:
            return "fluctuation"
        if amount <= self.tolerance_kg + 1e-9:
            return "tolerance"
        return "material"


class RuleBook:
    """只追加的计量规则册，按时间点取当时生效的版本。"""

    def __init__(self) -> None:
        self._rules: list[MeteringRule] = []

    def add(
        self,
        version: str,
        effective_ts: float,
        pressure_factor_kg_per_mpa: float,
        deadband_kg: float,
        tolerance_kg: float,
        material_kg: float,
        note: str = "",
    ) -> MeteringRule:
        """登记一版规则；版本号重复或参数顺序非法即拒绝。"""

        if not version:
            raise ConfigError("计量规则版本号不能为空", component="recon")
        if any(rule.version == version for rule in self._rules):
            raise ConfigError(f"计量规则版本 {version!r} 已存在", component="recon")
        if pressure_factor_kg_per_mpa <= 0:
            raise ConfigError("压力换算系数必须为正", component="recon")
        if not deadband_kg <= tolerance_kg <= material_kg:
            raise ConfigError("计量容差必须满足 deadband <= tolerance <= material", component="recon")
        rule = MeteringRule(
            version=version,
            effective_ts=float(effective_ts),
            pressure_factor_kg_per_mpa=float(pressure_factor_kg_per_mpa),
            deadband_kg=float(deadband_kg),
            tolerance_kg=float(tolerance_kg),
            material_kg=float(material_kg),
            note=note,
        )
        self._rules.append(rule)
        self._rules.sort(key=lambda item: item.effective_ts)
        return rule

    def restore(self, raw: Mapping[str, Any]) -> MeteringRule:
        """回放时按落盘字段恢复一版规则（保持历史口径，幂等）。"""

        rule = MeteringRule.from_mapping(raw)
        for existing in self._rules:
            if existing.version == rule.version:
                return existing
        self._rules.append(rule)
        self._rules.sort(key=lambda item: item.effective_ts)
        return rule

    def effective_at(self, ts: float) -> MeteringRule:
        """返回某时刻生效的规则；一条都没有即报错。"""

        chosen: MeteringRule | None = None
        for rule in self._rules:
            if rule.effective_ts <= ts:
                chosen = rule
        if chosen is None:
            raise ConfigError("该时刻还没有生效的计量规则版本", component="recon")
        return chosen

    def get(self, version: str) -> MeteringRule:
        """按版本号取规则。"""

        for rule in self._rules:
            if rule.version == version:
                return rule
        raise ConfigError(f"计量规则版本 {version!r} 不存在", component="recon")

    def all(self) -> list[MeteringRule]:
        """返回按生效时间排序的全部版本。"""

        return list(self._rules)

    def latest(self) -> MeteringRule | None:
        """返回最新版本，空册返回 ``None``。"""

        return self._rules[-1] if self._rules else None
