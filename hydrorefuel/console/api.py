"""控制台接口处理器。

每个接口都是对平台公开方法的一次调用；动作类接口把请求体里的参数透传给
平台，缺少必填字段时返回 ``400``，平台异常统一转成 ``409`` 并在响应里带上
组件名。
"""

from __future__ import annotations

from ..app import Platform
from ..audit.timeline import build_timeline
from ..errors import HydroError
from .router import Router


def _as_float(body: dict, key: str, default: float) -> float:
    value = body.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise HydroError(f"参数 {key!r} 必须是数值", component="console") from error


def _optional_float(body: dict, key: str) -> float | None:
    if key not in body or body[key] is None:
        return None
    return _as_float(body, key, 0.0)


class Api:
    """把 HTTP 请求翻译成平台调用。"""

    def __init__(self, platform: Platform) -> None:
        self._platform = platform
        self._router = Router()
        self._register()

    @property
    def platform(self) -> Platform:
        return self._platform

    def _register(self) -> None:
        add = self._router.add
        add("GET", "/api/status", lambda params, body: self._platform.status())
        add("GET", "/api/snapshot", lambda params, body: self._platform.snapshot())
        add("GET", "/api/topology", lambda params, body: self._platform.topology_summary())
        add("GET", "/api/namespaces", lambda params, body: self._platform.namespaces_describe())
        add("GET", "/api/namespace/{address}", self._namespace)
        add("GET", "/api/readiness", lambda params, body: self._platform.readiness())
        add("GET", "/api/truck", lambda params, body: {"truck": self._platform.truck_snapshot()})
        add("GET", "/api/vent", lambda params, body: {"vent": self._platform.vent_snapshot()})
        add("GET", "/api/compressor", lambda params, body: {"compressor": self._platform.compressor_snapshot()})
        add("GET", "/api/buffer", lambda params, body: {"buffer": self._platform.buffer_snapshot()})
        add("GET", "/api/reconciliation", lambda params, body: self._platform.reconciliation_report(include_actual=True))
        add("GET", "/api/reconciliation/snapshot", lambda params, body: self._platform.reconciliation.snapshot())
        add("GET", "/api/reconciliation/movements", self._recon_movements)
        add("GET", "/api/reconciliation/shifts", lambda params, body: self._platform.reconciliation_shifts())
        add("GET", "/api/reconciliation/readings", self._recon_readings)
        add("GET", "/api/reconciliation/verify", lambda params, body: self._platform.verify_reconciliation())
        add("POST", "/api/reconciliation/shifts/open", self._recon_open_shift)
        add("POST", "/api/reconciliation/shifts/close", self._recon_close_shift)
        add("POST", "/api/reconciliation/movements", self._recon_movement)
        add("POST", "/api/reconciliation/readings/partial", self._recon_partial_reading)
        add("POST", "/api/reconciliation/rules", self._recon_publish_rule)
        add("POST", "/api/reconciliation/teams", self._recon_team_policy)
        add("GET", "/api/cool", lambda params, body: {"cool": self._platform.cool_snapshot()})
        add("GET", "/api/flow", lambda params, body: {"flow": self._platform.flow_snapshot()})
        add("GET", "/api/disp", lambda params, body: {"disp": self._platform.disp_snapshot()})
        add("GET", "/api/leak", lambda params, body: {"leak": self._platform.leak_snapshot()})
        add("GET", "/api/queue", lambda params, body: {"queue": self._platform.queue_snapshot()})
        add("GET", "/api/audit", self._audit)
        add("GET", "/api/alarms", lambda params, body: {"alarms": self._platform.audit.alarms()})
        add("GET", "/api/events", self._events)
        add("GET", "/api/trend", self._trend)
        add("GET", "/api/store", self._store)
        add("GET", "/api/timeline", self._timeline)
        add("GET", "/api/journal", self._journal)
        add("POST", "/api/step", self._step)
        add("POST", "/api/unload", self._unload)
        add("POST", "/api/pressurize", self._pressurize)
        add("POST", "/api/fill", self._fill)
        add("POST", "/api/stop", self._stop)
        add("POST", "/api/leak/sample", self._leak_sample)
        add("POST", "/api/leak/reset", lambda params, body: self._platform.release_leak())
        add("POST", "/api/calibrate", self._calibrate)
        add("POST", "/api/disturb", self._disturb)
        add("POST", "/api/enqueue", self._enqueue)
        add("POST", "/api/dequeue", lambda params, body: self._platform.dequeue_vehicle())
        add("POST", "/api/concurrency", self._concurrency)
        add("POST", "/api/reset", lambda params, body: self._platform.reset_state())

    def routes(self) -> list[dict]:
        """返回接口清单，供自检与文档核对。"""

        return [route.as_dict() for route in self._router.routes()]

    def handle(self, method: str, path: str, query: dict, body: dict) -> tuple[int, dict]:
        """处理一次请求，返回状态码与响应体。"""

        matched = self._router.match(method, path)
        if matched is None:
            return 404, {"error": "not_found", "path": path}
        handler, params = matched
        merged = {**query, **params}
        try:
            payload = handler(merged, body)
        except HydroError as error:
            # 控制台拒绝的动作也要留痕，便于巡检区分"谁触发了哪条联锁"。
            self._platform.note_error(error)
            return 409, {"error": type(error).__name__, **error.as_record()}
        return 200, payload

    def _namespace(self, params: dict, body: dict) -> dict:
        return {"device": self._platform.namespace(str(params.get("address", "")))}

    def _audit(self, params: dict, body: dict) -> dict:
        return {"entries": self._platform.audit.entries(limit=int(params.get("limit", 100)))}

    def _events(self, params: dict, body: dict) -> dict:
        topic = params.get("topic")
        return {"events": [event.as_dict() for event in self._platform.bus.history(topic)]}

    def _trend(self, params: dict, body: dict) -> dict:
        signal = params.get("signal", "buffer_pressure")
        samples = self._platform.trend.series(signal, limit=int(params.get("limit", 60)))
        filled = self._platform.trend.fill_gaps(signal, samples)
        return {
            "signal": signal,
            "period_s": self._platform.trend.period_s,
            "samples": [sample.as_dict() for sample in filled],
        }

    def _store(self, params: dict, body: dict) -> dict:
        store = self._platform.store
        return {
            "root": store.root,
            "documents": store.list_ids(),
            "platform_state_path": store.path_of("platform_state"),
            "platform_state": store.read("platform_state"),
        }

    def _timeline(self, params: dict, body: dict) -> dict:
        station = params.get("station")
        records = self._platform.audit.records(limit=int(params.get("limit", 200)))
        return {"timeline": build_timeline(records, station=station)}

    def _journal(self, params: dict, body: dict) -> dict:
        journal = self._platform.audit.journal
        payload: dict = {
            "topic": journal.topic,
            "count": journal.count(),
            "active_segment": journal.active_segment(),
            "segments": journal.segments(),
        }
        segment = params.get("segment")
        if segment is not None:
            payload["records"] = self._platform.audit.replay_segment(int(segment))
        return payload

    def _step(self, params: dict, body: dict) -> dict:
        return self._platform.cycle(dt=_as_float(body, "dt", 1.0))

    def _unload(self, params: dict, body: dict) -> dict:
        return self._platform.unload(
            hose_pressure_mpa=_as_float(body, "hose_pressure_mpa", 20.0),
            target_kg=_as_float(body, "target_kg", 20.0),
            dt=_as_float(body, "dt", 1.0),
        )

    def _pressurize(self, params: dict, body: dict) -> dict:
        return self._platform.pressurize(
            inlet_pressure_mpa=_as_float(body, "inlet_pressure_mpa", 12.0),
            flow_kg_per_min=_as_float(body, "flow_kg_per_min", 4.0),
            charge_kg=_as_float(body, "charge_kg", 8.0),
            dt=_as_float(body, "dt", 1.0),
        )

    def _fill(self, params: dict, body: dict) -> dict:
        return self._platform.fill(
            vehicle=str(body.get("vehicle", "vehicle01")),
            rate_kg_per_min=_as_float(body, "rate_kg_per_min", 3.0),
            steps=int(body.get("steps", 3)),
            dt=_as_float(body, "dt", 36.0),
        )

    def _stop(self, params: dict, body: dict) -> dict:
        return self._platform.stop_fill()

    def _leak_sample(self, params: dict, body: dict) -> dict:
        return self._platform.sample_leak(_as_float(body, "concentration_ppm", 0.0))

    def _calibrate(self, params: dict, body: dict) -> dict:
        return self._platform.calibrate_baseline(
            _as_float(body, "target_mpa", self._platform.baseline.target_mpa)
        )

    def _disturb(self, params: dict, body: dict) -> dict:
        return self._platform.disturb_buffer(_as_float(body, "delta_mpa", 0.0))

    def _recon_movements(self, params: dict, body: dict) -> dict:
        shift_id = params.get("shift_id")
        limit = params.get("limit")
        return self._platform.reconciliation_movements(
            shift_id=None if shift_id is None else str(shift_id),
            limit=None if limit is None else int(limit),
        )

    def _recon_readings(self, params: dict, body: dict) -> dict:
        shift_id = params.get("shift_id")
        return self._platform.reconciliation_readings(None if shift_id is None else str(shift_id))

    def _recon_open_shift(self, params: dict, body: dict) -> dict:
        team = body.get("team")
        return self._platform.open_reconciliation_shift(None if team is None else str(team))

    def _recon_close_shift(self, params: dict, body: dict) -> dict:
        return self._platform.close_reconciliation_shift(
            actual_pressure_mpa=_optional_float(body, "actual_pressure_mpa"),
            actual_stock_kg=_optional_float(body, "actual_stock_kg"),
        )

    def _recon_movement(self, params: dict, body: dict) -> dict:
        direction = str(body.get("direction", ""))
        reading = body.get("meter_reading_after")
        pressure = body.get("pressure_after_mpa")
        reading_id = body.get("reading_id")
        return self._platform.record_buffer_movement(
            direction=direction,
            mass_kg=_as_float(body, "mass_kg", 0.0),
            meter_id=str(body.get("meter_id", "manual")),
            meter_reading_after=None if reading is None else _as_float(body, "meter_reading_after", 0.0),
            pressure_after_mpa=None if pressure is None else _as_float(body, "pressure_after_mpa", 0.0),
            reading_id=None if reading_id is None else str(reading_id),
            note=str(body.get("note", "")),
        )

    def _recon_partial_reading(self, params: dict, body: dict) -> dict:
        reading = body.get("meter_reading_after")
        previous = body.get("previous_meter_reading")
        estimated = body.get("estimated_mass_kg")
        reference = body.get("reference_id")
        return self._platform.record_meter_partial_reading(
            direction=str(body.get("direction", "")),
            meter_id=str(body.get("meter_id", "")),
            meter_reading_after=None if reading is None else _as_float(body, "meter_reading_after", 0.0),
            reason=str(body.get("reason", "计量补读部分失败")),
            previous_meter_reading=None if previous is None else _as_float(body, "previous_meter_reading", 0.0),
            estimated_mass_kg=None if estimated is None else _as_float(body, "estimated_mass_kg", 0.0),
            reference_id=None if reference is None else str(reference),
        )

    def _recon_publish_rule(self, params: dict, body: dict) -> dict:
        return self._platform.publish_measurement_rule(
            version=str(body.get("version", "")),
            mass_tolerance_kg=_as_float(body, "mass_tolerance_kg", 0.0),
            pressure_tolerance_mpa=_as_float(body, "pressure_tolerance_mpa", 0.0),
            kg_per_mpa=_as_float(body, "kg_per_mpa", 0.0),
            expected_pressure_mpa=_as_float(body, "expected_pressure_mpa", 42.0),
        )

    def _recon_team_policy(self, params: dict, body: dict) -> dict:
        return self._platform.set_reconciliation_team_policy(
            team=str(body.get("team", "")),
            large_threshold_kg=_as_float(body, "large_threshold_kg", 0.0),
            on_large=str(body.get("on_large", "hold")),
        )

    def _enqueue(self, params: dict, body: dict) -> dict:
        return self._platform.enqueue_vehicle(str(body.get("vehicle", "vehicle01")))

    def _concurrency(self, params: dict, body: dict) -> dict:
        return self._platform.concurrency_probe(rounds=int(body.get("rounds", 24)))
