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
        add("GET", "/api/recon/stock", lambda params, body: self._platform.recon_stock())
        add("GET", "/api/recon/entries", self._recon_entries)
        add("GET", "/api/recon/entry/{entry_id}", self._recon_entry)
        add("GET", "/api/recon/shifts", self._recon_shifts)
        add("GET", "/api/recon/shift/{shift_id}", self._recon_shift)
        add("GET", "/api/recon/rules", lambda params, body: self._platform.recon_rules())
        add("GET", "/api/recon/journal", lambda params, body: self._platform.recon_journal())
        add("GET", "/api/recon/consistency", lambda params, body: self._platform.recon_consistency())
        add("POST", "/api/recon/shift/open", self._recon_open)
        add("POST", "/api/recon/shift/close", self._recon_close)
        add("POST", "/api/recon/policy", self._recon_policy)
        add("POST", "/api/recon/charge", self._recon_charge)
        add("POST", "/api/recon/draw", self._recon_draw)
        add("POST", "/api/recon/pending", self._recon_pending)
        add("POST", "/api/recon/reading", self._recon_reading)
        add("POST", "/api/recon/reading/fail", self._recon_reading_fail)
        add("POST", "/api/recon/verify", self._recon_verify)
        add("POST", "/api/recon/rules", self._recon_register_rule)
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

    def _enqueue(self, params: dict, body: dict) -> dict:
        return self._platform.enqueue_vehicle(str(body.get("vehicle", "vehicle01")))

    def _concurrency(self, params: dict, body: dict) -> dict:
        return self._platform.concurrency_probe(rounds=int(body.get("rounds", 24)))

    # -------------------------------------------------------------- 瓶组对账

    def _recon_entries(self, params: dict, body: dict) -> dict:
        shift_id = params.get("shift")
        return self._platform.recon_entries(shift_id=shift_id)

    def _recon_entry(self, params: dict, body: dict) -> dict:
        return {"entry": self._platform.recon_entry(str(params.get("entry_id", "")))}

    def _recon_shift(self, params: dict, body: dict) -> dict:
        return {"shift": self._platform.recon_shift(str(params.get("shift_id", "")))}

    def _recon_shifts(self, params: dict, body: dict) -> dict:
        state = params.get("state")
        return self._platform.recon_shifts(state=state)

    def _recon_open(self, params: dict, body: dict) -> dict:
        opening = body.get("opening_kg")
        return self._platform.recon_open_shift(
            crew=str(body.get("crew", "unassigned")),
            opening_kg=None if opening is None else _as_float(body, "opening_kg", 0.0),
        )

    def _recon_close(self, params: dict, body: dict) -> dict:
        return self._platform.recon_close_shift(_as_float(body, "physical_pressure_mpa", 0.0))

    def _recon_policy(self, params: dict, body: dict) -> dict:
        return self._platform.recon_set_policy(str(body.get("crew", "")), str(body.get("policy", "")))

    def _recon_charge(self, params: dict, body: dict) -> dict:
        before = body.get("pressure_before_mpa")
        after = body.get("pressure_after_mpa")
        return self._platform.recon_charge(
            _as_float(body, "qty_kg", 0.0),
            operator=str(body.get("operator", "")),
            meter_ref=str(body.get("meter_ref", "")),
            pressure_before_mpa=None if before is None else float(before),
            pressure_after_mpa=None if after is None else float(after),
        )

    def _recon_draw(self, params: dict, body: dict) -> dict:
        before = body.get("pressure_before_mpa")
        after = body.get("pressure_after_mpa")
        return self._platform.recon_draw(
            _as_float(body, "qty_kg", 0.0),
            operator=str(body.get("operator", "")),
            meter_ref=str(body.get("meter_ref", "")),
            pressure_before_mpa=None if before is None else float(before),
            pressure_after_mpa=None if after is None else float(after),
        )

    def _recon_pending(self, params: dict, body: dict) -> dict:
        kind = str(body.get("kind", ""))
        if kind not in ("charge", "draw"):
            raise HydroError("kind 必须是 charge 或 draw", component="console")
        return self._platform.recon_pending(
            kind,
            operator=str(body.get("operator", "")),
            meter_ref=str(body.get("meter_ref", "")),
            note=str(body.get("note", "")),
        )

    def _recon_reading(self, params: dict, body: dict) -> dict:
        return self._platform.recon_supply_reading(
            str(body.get("entry_id", "")),
            _as_float(body, "qty_kg", 0.0),
            meter_ref=str(body.get("meter_ref", "")),
        )

    def _recon_reading_fail(self, params: dict, body: dict) -> dict:
        return self._platform.recon_fail_reading(
            str(body.get("entry_id", "")), str(body.get("reason", "读数失败"))
        )

    def _recon_verify(self, params: dict, body: dict) -> dict:
        return self._platform.recon_verify_entry(
            str(body.get("entry_id", "")), _as_float(body, "verified_delta_kg", 0.0)
        )

    def _recon_register_rule(self, params: dict, body: dict) -> dict:
        return self._platform.recon_register_rule(
            version=str(body.get("version", "")),
            pressure_factor_kg_per_mpa=_as_float(body, "pressure_factor_kg_per_mpa", 5.0),
            deadband_kg=_as_float(body, "deadband_kg", 0.5),
            tolerance_kg=_as_float(body, "tolerance_kg", 1.5),
            material_kg=_as_float(body, "material_kg", 3.0),
            note=str(body.get("note", "")),
        )
