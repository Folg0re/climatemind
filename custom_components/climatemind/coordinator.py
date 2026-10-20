"""DataUpdateCoordinator for RoomMind."""

from __future__ import annotations

import logging
import math
import time
from datetime import timedelta
from functools import partial
from typing import Any

from homeassistant.components.persistent_notification import async_create as async_create_notification
from homeassistant.components.persistent_notification import async_dismiss as async_dismiss_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    AC_COOLING_BOOST_TARGET,
    AC_HEATING_BOOST_TARGET,
    CLIMATE_MODE_COOL_ONLY,
    CLIMATE_MODE_HEAT_ONLY,
    COMPRESSOR_STATE_SETTLE_SECONDS,
    DEFAULT_COMFORT_COOL,
    DEFAULT_COMFORT_HEAT,
    DEFAULT_ECO_COOL,
    DEFAULT_ECO_HEAT,
    DEFAULT_OUTDOOR_HEATING_MAX,
    DOMAIN,
    HEATING_BOOST_TARGET,
    HISTORY_ROTATE_CYCLES,
    HISTORY_WRITE_CYCLES,
    MAX_PREDICTION_DELTA,
    MAX_SENSOR_STALENESS,
    MAX_TARGET_TEMP,
    MIN_TARGET_TEMP,
    MODE_COOLING,
    MODE_HEATING,
    MODE_IDLE,
    OUTDOOR_UNAVAILABLE_NOTIFICATION_ID,
    OUTDOOR_UNAVAILABLE_NOTIFY_CYCLES,
    OVERRIDE_TURN_ON_REFRESH_DELAY_S,
    SCHEDULE_STATE_ON,
    THERMAL_SAVE_CYCLES,
    UPDATE_INTERVAL,
    TargetTemps,
    build_override_live,
    is_override_active,
    is_override_suppressed,
    make_roommind_context,
)
from .control.mpc_controller import (
    DEFAULT_OUTDOOR_TEMP_FALLBACK,
    MPCController,
    check_acs_can_heat,
    get_can_heat_cool,
    infer_running_mode,
    is_mpc_active,
)
from .control.residual_heat import mean_rebound
from .control.solar import compute_q_solar_norm
from .control.thermal_model import RoomModelManager
from .managers.ac_coil_dry_manager import AcCoilDryManager, CoilDryRoomResult
from .managers.compressor_group_manager import (
    CompressorGroupConfig,
    CompressorGroupManager,
    CompressorGroupState,
    resolve_master_action,
)
from .managers.cover_orchestrator import CoverOrchestrator, CoverResult
from .managers.ekf_training_manager import EkfTrainingManager
from .managers.heat_source_orchestrator import HeatSourcePlan, evaluate_heat_sources
from .managers.mold_manager import MoldManager
from .managers.residual_heat_tracker import AcReboundTracker, ResidualHeatTracker
from .managers.valve_manager import ValveManager
from .managers.weather_manager import WeatherManager
from .managers.window_manager import WindowManager
from .utils.device_utils import (
    build_rooms_devices_map,
    get_ac_eids,
    get_all_entity_ids,
    get_direct_setpoint_eids,
    get_setpoint_offsets,
    get_trv_eids,
    room_contributes_to_group,
)
from .utils.history_store import HistoryStore
from .utils.schedule_utils import resolve_schedule_index
from .utils.sensor_utils import read_sensor_value
from .utils.temp_utils import celsius_delta_to_ha, ha_temp_to_celsius, ha_temp_unit_str

_LOGGER = logging.getLogger(__name__)
_COMPRESSOR_STATES = ("heat", "cool", "heat_cool", "auto", "dry")

# Tabella di conversione standard Danfoss per valvole termostatiche manuali (Posizione 0 - 5)
DANFOSS_VALVE_CURVE = {
    0.0: 0.00,
    0.25: 0.05,
    0.5: 0.10,
    0.75: 0.12,
    1.0: 0.15,
    1.25: 0.20,
    1.5: 0.28,
    1.75: 0.35,
    2.0: 0.42,
    2.25: 0.50,
    2.5: 0.58,
    2.75: 0.64,
    3.0: 0.70,
    3.25: 0.76,
    3.5: 0.81,
    3.75: 0.86,
    4.0: 0.90,
    4.25: 0.93,
    4.5: 0.96,
    4.75: 0.98,
    5.0: 1.00,
}


def _get_manual_valve_factor(val: float | None) -> float:
    """Converte la posizione della valvola manuale (0-5) in fattore di flusso (0.0 - 1.0) tramite curva Danfoss."""
    if val is None:
        return 1.0
    val = max(0.0, min(5.0, val))
    keys = sorted(DANFOSS_VALVE_CURVE.keys())
    if val in DANFOSS_VALVE_CURVE:
        return DANFOSS_VALVE_CURVE[val]

    lower = keys[0]
    upper = keys[-1]
    for k in keys:
        if k <= val:
            lower = k
        if k >= val:
            upper = k
            break
    if lower == upper:
        return DANFOSS_VALVE_CURVE[lower]

    fraction = (val - lower) / (upper - lower)
    return DANFOSS_VALVE_CURVE[lower] + fraction * (DANFOSS_VALVE_CURVE[upper] - DANFOSS_VALVE_CURVE[lower])


def _round_down_to_int(val: float | None) -> int | None:
    """Tronca/Arrotonda un valore numerico per difetto al numero intero (per AC senza decimali)."""
    if val is None or not isinstance(val, (int, float)):
        return None
    return int(math.floor(val))


def _get_area_name(hass: HomeAssistant, area_id: str) -> str:
    """Get human-readable area name from area registry."""
    try:
        area_reg = ar.async_get(hass)
        area = area_reg.async_get_area(area_id)
        return area.name if area else area_id
    except Exception:  # noqa: BLE001
        return area_id


ROOM_ENTITY_SUFFIXES = (
    "_target_temp",
    "_mode",
    "_override",
    "_climate_control",
    "_cover_auto",
    "_cover_paused",
)
COVER_ENTITY_SUFFIXES = ("_cover_auto", "_cover_paused")


def _match_room_entity(parts: str, rooms: dict) -> tuple[str, str] | None:
    """Match parts to an owning room."""
    for area_id in rooms:
        for suffix in ROOM_ENTITY_SUFFIXES:
            if parts == f"{area_id}{suffix}":
                return area_id, suffix
    return None


def _orchestrated_training_power(heat_source_plan: HeatSourcePlan) -> float:
    """Return mean power for devices actively heating in an orchestrated plan."""
    active = [command.power_fraction for command in heat_source_plan.commands if command.active]
    return sum(active) / len(active) if active else 0.0


class RoomMindCoordinator(DataUpdateCoordinator):
    """Central coordinator for RoomMind room data and state."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )
        self.entry = entry
        self.rooms: dict = {}
        self.outdoor_temp: float | None = None
        self.outdoor_temp_effective: float | None = None
        self.outdoor_temp_source: str = "none"
        self.outdoor_humidity: float | None = None
        self._outdoor_unavailable_cycles: int = 0
        self._outdoor_warning_sent: bool = False
        self._window_manager = WindowManager()
        self._previous_modes: dict[str, str] = {}
        self._model_manager: RoomModelManager = RoomModelManager()
        self._model_loaded = False
        self._thermal_save_count: int = 0
        self._history_store: HistoryStore | None = None
        self._history_write_count: int = 0
        self._history_rotate_count: int = 0
        self._pending_predictions: dict[str, float] = {}
        self._prediction_forecasts: dict[str, list[dict]] = {}
        self._weather_manager = WeatherManager(hass)
        self._current_q_solar: float = 0.0
        self._valve_manager = ValveManager(hass)
        self._mold_manager = MoldManager(hass)
        self._coil_dry_manager = AcCoilDryManager(hass)
        self._residual_tracker = ResidualHeatTracker()
        self._rebound_tracker = AcReboundTracker()
        self._ekf_training = EkfTrainingManager(self._model_manager)

        from .managers.cover_manager import CoverManager

        self._cover_manager = CoverManager()
        self._cover_orchestrator = CoverOrchestrator(hass, self._cover_manager, self._model_manager)
        self._compressor_manager = CompressorGroupManager()
        self._heat_source_states: dict[str, str] = {}
        self._entity_areas: set[str] = set()
        self._mode_on_since: dict[str, float] = {}
        self._mode_seeded: set[str] = set()
        self._last_valid_temps: dict[str, tuple[float, float]] = {}
        self._had_valid_temp: set[str] = set()
        self._startup_ts: float = time.monotonic()
        self._startup_guard_warned: set[str] = set()
        # Out-of-range schedule block temps already warned about (#395).
        # Keyed by (area_id, field, raw value) so the coordinator's 30s cycle
        # does not flood the log with the same typo.
        self._block_temp_warned: set[tuple[str, str, str]] = set()
        # monotonic time at which the override entity seeded an override per room (#447)
        self._override_seeded_at: dict[str, float] = {}
        self._switch_entity_areas: set[str] = set()
        self._climate_control_switch_areas: set[str] = set()
        self._binary_sensor_entity_areas: set[str] = set()
        self._climate_entity_areas: set[str] = set()

        self._last_sent_commands: dict[str, dict] = {}
        self._last_correction_ts: dict[str, float] = {}
        self._schedule_blocks_cache: dict[str, dict] = {}
        self.async_add_entities: Any = None
        self.async_add_switch_entities: Any = None
        self.async_add_climate_entities: Any = None
        self.async_add_binary_sensor_entities: Any = None

    def _is_central_heating_active(self, settings: dict) -> bool:
        """Verifica se il riscaldamento centralizzato condominiale è attualmente abilitato e attivo via schedule."""
        if not settings.get("central_heating_enabled", False):
            return True
        schedule_eid = settings.get("central_heating_schedule")
        if not schedule_eid:
            return True
        state = self.hass.states.get(schedule_eid)
        if state is None or state.state in ("unavailable", "unknown"):
            return True
        return bool(state.state == "on")

    async def _async_update_data(self) -> dict:
        """Fetch and compute state for all rooms."""
        store = self.hass.data[DOMAIN]["store"]
        rooms = store.get_rooms()

        settings = store.get_settings()
        outdoor_sensor_id = settings.get("outdoor_temp_sensor")
        raw_outdoor = read_sensor_value(self.hass, outdoor_sensor_id, "global", "outdoor temperature")
        self.outdoor_temp = (
            ha_temp_to_celsius(self.hass, raw_outdoor, entity_id=outdoor_sensor_id) if raw_outdoor is not None else None
        )
        self.outdoor_humidity = read_sensor_value(
            self.hass, settings.get("outdoor_humidity_sensor"), "global", "outdoor humidity"
        )

        self.outdoor_temp_effective, self.outdoor_temp_source = self._resolve_outdoor_temp(settings)
        self._update_outdoor_unavailable_notification(settings)

        self._compressor_manager.load_groups(settings.get("compressor_groups", []))

        if not self._model_loaded:
            thermal_data = store.get_thermal_data()
            if thermal_data:
                self._model_manager = RoomModelManager.from_dict(thermal_data)
                self._ekf_training._model_manager = self._model_manager
                self._cover_orchestrator._model_manager = self._model_manager
            self._valve_manager.load_actuation_data(settings.get("valve_last_actuation", {}))
            self._coil_dry_manager.load_state(settings.get("coil_dry_state", {}))
            self._model_loaded = True

        if self._history_store is None:
            self._history_store = HistoryStore(self.hass.config.path(".storage/climatemind_history"))

        room_states: dict[str, dict] = {}
        outdoor_forecast = await self._weather_manager.async_read_forecast(settings)

        self._cover_orchestrator.set_cloud_series(WeatherManager.extract_cloud_series(outdoor_forecast))

        cloud_coverage = None
        weather_entity = settings.get("weather_entity")
        if weather_entity:
            ws = self.hass.states.get(weather_entity)
            if ws:
                cloud_coverage = ws.attributes.get("cloud_coverage")
        self._current_q_solar = compute_q_solar_norm(
            self.hass.config.latitude,
            self.hass.config.longitude,
            time.time(),
            cloud_coverage,
        )

        for area_id, room in rooms.items():
            try:
                room_state = await self._async_process_room(room, settings, outdoor_forecast)
                room_states[area_id] = room_state
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Room '%s': processing failed, skipping", area_id)

        await self._async_control_master_devices(room_states, rooms, settings)

        learning_disabled = set(settings.get("learning_disabled_rooms", []))
        self._history_write_count += 1
        if self._history_write_count >= HISTORY_WRITE_CYCLES and self._history_store:
            self._history_write_count = 0
            for area_id, rs in room_states.items():
                if area_id in learning_disabled:
                    continue
                current_temp = rs.get("current_temp")
                mode = rs.get("mode", MODE_IDLE)
                target_temp = rs.get("target_temp")
                predicted = self._pending_predictions.pop(area_id, None)
                try:
                    await self.hass.async_add_executor_job(
                        self._history_store.record,
                        area_id,
                        {
                            "room_temp": rs.get("current_temp_raw", current_temp),
                            "outdoor_temp": self.outdoor_temp_effective,
                            "target_temp": target_temp,
                            "mode": mode,
                            "predicted_temp": predicted,
                            "window_open": rs.get("window_open", False),
                            "heating_power": rs.get("heating_power", 0),
                            "solar_irradiance": round(self._current_q_solar, 3),
                            "blind_position": rs.get("blind_position"),
                            "cover_reason": rs.get("cover_reason", ""),
                            "device_setpoint": rs.get("device_setpoint"),
                            "occupancy": rs.get("q_occupancy", 0.0) > 0,
                        },
                    )
                except Exception:  # noqa: BLE001
                    _LOGGER.warning("History record failed for '%s'", area_id)

                room_config = rooms.get(area_id, {})
                if (
                    current_temp is not None
                    and self.outdoor_temp_effective is not None
                    and not room_config.get("is_outdoor", False)
                ):
                    try:
                        is_window_open = rs.get("window_open", False)
                        if is_window_open:
                            raw_pred = self._model_manager.predict_window_open(
                                area_id,
                                current_temp,
                                self.outdoor_temp_effective,
                                3.0,
                            )
                        else:
                            model = self._model_manager.get_model(area_id)
                            hp = rs.get("heating_power", 100) / 100.0
                            Q = (
                                hp * model.Q_heat
                                if mode == "heating"
                                else (-hp * model.Q_cool if mode == "cooling" else 0.0)
                            )
                            raw_pred = model.predict(
                                current_temp,
                                self.outdoor_temp_effective,
                                Q,
                                3.0,
                                q_solar=self._current_q_solar * rs.get("shading_factor", 1.0),
                                q_rebound=mean_rebound(
                                    self._rebound_tracker.get_charge(area_id), model.cooling_drive(Q), 3.0
                                ),
                            )
                        clamped = max(
                            current_temp - MAX_PREDICTION_DELTA, min(current_temp + MAX_PREDICTION_DELTA, raw_pred)
                        )
                        self._pending_predictions[area_id] = round(clamped, 2)
                    except Exception:  # noqa: BLE001
                        pass

        self._thermal_save_count += 1
        if self._thermal_save_count >= THERMAL_SAVE_CYCLES:
            self._thermal_save_count = 0
            await store.async_save_thermal_data(self._model_manager.to_dict())

        self._history_rotate_count += 1
        if self._history_rotate_count >= HISTORY_ROTATE_CYCLES and self._history_store:
            self._history_rotate_count = 0
            for area_id in rooms:
                try:
                    await self.hass.async_add_executor_job(self._history_store.rotate, area_id)
                except Exception:  # noqa: BLE001
                    _LOGGER.warning("History rotation failed for '%s'", area_id)

        await self._valve_manager.async_finish_cycles(build_rooms_devices_map(rooms))

        if self._valve_manager.should_run_cycle_check():
            await self._valve_manager.async_check_and_cycle(rooms, settings)

        if self._valve_manager.actuation_dirty and self._thermal_save_count == 0:
            await store.async_save_settings({"valve_last_actuation": self._valve_manager.get_actuation_data()})
            self._valve_manager.actuation_dirty = False

        # Coil dry: drop state for devices no longer configured anywhere
        self._coil_dry_manager.prune(set(build_rooms_devices_map(rooms)))

        # Persist coil dry state on change — a 20 min run is far shorter than
        # the 15 min thermal save cycle, so this must not piggyback on it.
        if self._coil_dry_manager.state_dirty:
            await store.async_save_settings({"coil_dry_state": self._coil_dry_manager.get_state()})
            self._coil_dry_manager.state_dirty = False

        self.rooms = room_states
        return {"rooms": room_states}

    def _read_room_sensors(
        self,
        room: dict,
        area_id: str,
    ) -> tuple[float | None, float | None, float | None, bool]:
        """Read temperature and humidity sensors for a room."""
        temp_sensor_id = room.get("temperature_sensor")
        has_external_sensor = bool(temp_sensor_id)

        raw_temp = read_sensor_value(self.hass, temp_sensor_id, area_id, "temperature")
        current_temp = (
            ha_temp_to_celsius(self.hass, raw_temp, entity_id=temp_sensor_id) if raw_temp is not None else None
        )

        if current_temp is None and not has_external_sensor:
            raw_dev = self._read_device_temp(room)
            current_temp = ha_temp_to_celsius(self.hass, raw_dev) if raw_dev is not None else None

        current_temp_raw = current_temp

        if current_temp is not None:
            self._last_valid_temps[area_id] = (current_temp, time.monotonic())
            self._had_valid_temp.add(area_id)
        elif area_id in self._last_valid_temps:
            cached_temp, cached_ts = self._last_valid_temps[area_id]
            if time.monotonic() - cached_ts < MAX_SENSOR_STALENESS:
                current_temp = cached_temp
            else:
                del self._last_valid_temps[area_id]

        current_humidity = read_sensor_value(self.hass, room.get("humidity_sensor"), area_id, "humidity")
        return current_temp, current_temp_raw, current_humidity, has_external_sensor

    def _resolve_outdoor_temp(self, settings: dict) -> tuple[float | None, str]:
        if self.outdoor_temp is not None:
            return self.outdoor_temp, "sensor"

        weather_eid = settings.get("weather_entity") or ""
        if weather_eid:
            ws = self.hass.states.get(weather_eid)
            if ws is not None and ws.state not in ("unavailable", "unknown"):
                temp_attr = ws.attributes.get("temperature")
                if isinstance(temp_attr, (int, float)) and not isinstance(temp_attr, bool):
                    converted = ha_temp_to_celsius(self.hass, float(temp_attr), entity_id=weather_eid)
                    if converted is not None:
                        return converted, "weather"

        return None, "none"

    def _update_outdoor_unavailable_notification(self, settings: dict) -> None:
        if self.outdoor_temp_effective is not None:
            self._outdoor_unavailable_cycles = 0
            if self._outdoor_warning_sent:
                self._outdoor_warning_sent = False
                async_dismiss_notification(self.hass, OUTDOOR_UNAVAILABLE_NOTIFICATION_ID)
            return

        self._outdoor_unavailable_cycles += 1

        if self._outdoor_warning_sent or self._outdoor_unavailable_cycles < OUTDOOR_UNAVAILABLE_NOTIFY_CYCLES:
            return
        if not settings.get("outdoor_unavailable_notify", True):
            return

        sensor_id = settings.get("outdoor_temp_sensor") or "(not configured)"
        weather_eid = settings.get("weather_entity") or "(not configured)"
        message = (
            "RoomMind cannot read an outdoor temperature. Thermal model "
            "learning is paused for all rooms until a valid source returns.\n\n"
            f"• Outdoor sensor: `{sensor_id}`\n"
            f"• Weather entity: `{weather_eid}`"
        )
        _LOGGER.warning(
            "Outdoor temperature unavailable for %d cycles — EKF learning paused", self._outdoor_unavailable_cycles
        )
        async_create_notification(
            self.hass,
            message,
            title="RoomMind: outdoor temperature unavailable",
            notification_id=OUTDOOR_UNAVAILABLE_NOTIFICATION_ID,
        )
        self._outdoor_warning_sent = True

    async def _evaluate_mold_risk(
        self,
        area_id: str,
        current_temp: float | None,
        current_humidity: float | None,
        settings: dict,
    ) -> tuple[str, float | None, bool, float]:
        mold = await self._mold_manager.evaluate(
            area_id,
            _get_area_name(self.hass, area_id),
            current_temp,
            current_humidity,
            self.outdoor_temp_effective,
            settings,
            celsius_delta_to_ha_fn=lambda d: celsius_delta_to_ha(self.hass, d),
            ha_temp_unit_str_fn=lambda: ha_temp_unit_str(self.hass),
        )
        return mold.risk_level, mold.surface_rh, mold.prevention_active, mold.prevention_delta

    def note_override_seed(self, area_id: str) -> None:
        """Mark an override just seeded by switching the override entity on.

        Until the caller's real targets arrive (or the grace period ends) the seed
        is not evaluated: an override lifts the outdoor gate, so acting on a
        placeholder band can start the AC against it (#447). In memory only.
        """
        self._override_seeded_at[area_id] = time.monotonic()

    def clear_override_seed(self, area_id: str) -> None:
        """Forget the seed marker: the override now holds values the user chose."""
        self._override_seeded_at.pop(area_id, None)

    def is_override_seed_pending(self, area_id: str) -> bool:
        """Return True while a fresh seed is still masked (read-only, unlike the masking itself)."""
        seeded_at = self._override_seeded_at.get(area_id)
        return seeded_at is not None and time.monotonic() - seeded_at < OVERRIDE_TURN_ON_REFRESH_DELAY_S

    def _without_pending_override_seed(self, room: dict) -> dict:
        """Return *room* as if no override existed while a fresh seed is pending."""
        area_id = room.get("area_id", "unknown")
        seeded_at = self._override_seeded_at.get(area_id)
        if seeded_at is None:
            return room
        if time.monotonic() - seeded_at >= OVERRIDE_TURN_ON_REFRESH_DELAY_S:
            del self._override_seeded_at[area_id]
            return room
        return {**room, "override_heat": None, "override_cool": None, "override_until": None, "override_type": None}

    async def _async_process_room(self, room: dict, settings: dict, outdoor_forecast: list[dict]) -> dict:
        room = self._without_pending_override_seed(room)
        area_id = room.get("area_id", "unknown")
        current_temp, current_temp_raw, current_humidity, has_external_sensor = self._read_room_sensors(room, area_id)

        if room.get("is_outdoor", False):
            return {
                "area_id": area_id,
                "current_temp": current_temp,
                "current_temp_raw": current_temp_raw,
                "current_humidity": current_humidity,
                "target_temp": None,
                "heat_target": None,
                "cool_target": None,
                "mode": MODE_IDLE,
                "heating_power": 0,
                "device_setpoint": None,
                "window_open": False,
                "override_active": False,
                "override_type": None,
                "override_heat": None,
                "override_cool": None,
                "override_until": None,
                "override_suppressed": False,
                "active_schedule_index": -1,
                "confidence": None,
                "mpc_active": False,
                "presence_away": False,
                "force_off": False,
                "mold_risk_level": "ok",
                "mold_surface_rh": None,
                "mold_prevention_active": False,
                "mold_prevention_delta": 0,
                "shading_factor": 1.0,
                "n_observations": 0,
                "blind_position": None,
                "cover_auto_paused": False,
                "cover_forced_reason": "",
                "active_cover_schedule_index": -1,
                "q_occupancy": 0.0,
                "active_heat_sources": None,
                "compressor_protection_active": False,
                "compressor_protection_reason": None,
                "coil_dry_active": False,
                "coil_dry_phase": None,
                "coil_dry_until": None,
                "coil_dry_entities": [],
                "schedule_temp_warnings": [],
            }

        (
            mold_risk_level,
            mold_surface_rh,
            mold_prevention_active_room,
            mold_prevention_temp_delta,
        ) = await self._evaluate_mold_risk(area_id, current_temp, current_humidity, settings)

        from .utils.schedule_utils import (
            cap_cool_only_heat,
            find_rejected_block_temps,
            get_active_schedule_entity,
            make_target_resolver,
            read_schedule_blocks,
        )

        schedule_entity_id = get_active_schedule_entity(self.hass, room)
        schedule_blocks = (
            await read_schedule_blocks(self.hass, schedule_entity_id, cache=self._schedule_blocks_cache)
            if schedule_entity_id
            else None
        )
        schedule_temp_warnings = find_rejected_block_temps(schedule_blocks, partial(ha_temp_to_celsius, self.hass))

        targets = self._resolve_target_temps(room, settings, schedule_blocks, schedule_entity_id)
        targets = TargetTemps(*cap_cool_only_heat(targets.heat, targets.cool, room.get("climate_mode", "auto")))

        force_off = targets.heat is None and targets.cool is None
        if mold_prevention_active_room and mold_prevention_temp_delta > 0:
            if force_off:
                eco_heat = room.get("eco_heat", room.get("eco_temp", DEFAULT_ECO_HEAT))
                eco_cool = room.get("eco_cool", DEFAULT_ECO_COOL)
                targets = TargetTemps(
                    heat=eco_heat + mold_prevention_temp_delta,
                    cool=eco_cool,
                )
                force_off = False
            elif targets.heat is not None:
                targets = TargetTemps(
                    heat=targets.heat + mold_prevention_temp_delta,
                    cool=targets.cool,
                )

        presence_away = not room.get("ignore_presence", False) and self._is_presence_away(room, settings)
        target_resolver = make_target_resolver(
            schedule_blocks,
            room,
            settings,
            hass=self.hass,
            presence_away=presence_away,
            mold_prevention_delta=mold_prevention_temp_delta,
        )

        system_type = room.get("heating_system_type", "")
        q_residual = self._residual_tracker.get_q_residual(
            area_id,
            system_type,
            self._previous_modes.get(area_id, MODE_IDLE),
        )
        seed_pending = False
        if (
            has_external_sensor
            and settings.get("climate_control_active", True)
            and room.get("climate_control_enabled", True)
            and area_id not in self._mode_seeded
            and self._previous_modes.get(area_id, MODE_IDLE) == MODE_IDLE
        ):
            seeded_mode: str | None = infer_running_mode(self.hass, room, before=self._startup_ts)
            seed_pending = seeded_mode is None and time.monotonic() - self._startup_ts < MAX_SENSOR_STALENESS
            if seeded_mode is not None and seeded_mode != MODE_IDLE:
                self._previous_modes[area_id] = seeded_mode
                self._mode_on_since[area_id] = time.time()
                if seeded_mode == MODE_COOLING:
                    self._rebound_tracker.seed_running(area_id)

        q_rebound = self._rebound_tracker.get_charge(area_id)

        cover_eids: list[str] = room.get("covers", [])
        cover_pos_result = self._cover_orchestrator.read_positions(area_id, room)
        shading_factor = cover_pos_result.shading_factor

        q_occupancy = 0.0
        for occ_eid in room.get("occupancy_sensors", []):
            occ_state = self.hass.states.get(occ_eid)
            if occ_state and occ_state.state == "on":
                q_occupancy = 1.0
                break

        controller = MPCController(
            self.hass,
            room,
            model_manager=self._model_manager,
            outdoor_temp=self.outdoor_temp_effective,
            outdoor_forecast=outdoor_forecast,
            settings=settings,
            previous_mode=self._previous_modes.get(area_id, MODE_IDLE),
            mode_on_since=self._mode_on_since.get(area_id),
            has_external_sensor=has_external_sensor,
            target_resolver=target_resolver,
            q_solar=self._current_q_solar,
            latitude=self.hass.config.latitude,
            longitude=self.hass.config.longitude,
            cloud_series=WeatherManager.extract_cloud_series(outdoor_forecast),
            q_residual=q_residual,
            heating_system_type=system_type,
            shading_factor=shading_factor,
            q_occupancy=q_occupancy,
            q_rebound=q_rebound,
            previous_heat_sources=self._heat_source_states.get(area_id, "none"),
        )

        if current_temp is None:
            mode = MODE_IDLE
            power_fraction = 0.0
        else:
            mode, power_fraction = await controller.async_evaluate(current_temp, targets)

        # --- GESTIONE RISCALDAMENTO CENTRALIZZATO & VALVOLE MANUALI ---
        central_active = self._is_central_heating_active(settings)

        manual_valve_eid = room.get("manual_valve_entity")
        manual_valve_val = None
        if manual_valve_eid:
            st = self.hass.states.get(manual_valve_eid)
            if st and st.state not in ("unavailable", "unknown"):
                try:
                    manual_valve_val = float(st.state)
                except (ValueError, TypeError):
                    pass
        valve_factor = _get_manual_valve_factor(manual_valve_val)

        if mode == MODE_HEATING:
            if not central_active or valve_factor <= 0.0:
                mode = MODE_IDLE
                power_fraction = 0.0
            else:
                power_fraction = round(power_fraction * valve_factor, 2)

        climate_mode = room.get("climate_mode", "auto")
        if climate_mode == CLIMATE_MODE_COOL_ONLY:
            target_temp = targets.cool
        elif climate_mode == CLIMATE_MODE_HEAT_ONLY:
            target_temp = targets.heat
        else:
            if mode == MODE_HEATING and targets.heat is not None:
                target_temp = targets.heat
            elif mode == MODE_COOLING and targets.cool is not None:
                target_temp = targets.cool
            else:
                target_temp = targets.heat if targets.heat is not None else targets.cool

        if force_off:
            mode = MODE_IDLE
            power_fraction = 0.0

        window_states = self._read_window_states(room)
        raw_state = self._window_manager.resolve_raw(
            area_id, window_states, self._missing_window_sensors(window_states)
        )
        # Pending: sensors unavailable and no state known yet (typically right after
        # a restart).  Keep the current pause state and send no commands; update()
        # is skipped so the first real reading still counts as the first observation.
        window_pending = raw_state is None
        if raw_state is None:
            raw_open = True  # skips EKF training like an open_delay window
            window_open = self._window_manager.is_paused(area_id)
        else:
            raw_open = raw_state
            window_open = self._window_manager.update(
                area_id,
                raw_state,
                room.get("window_open_delay", 0),
                room.get("window_close_delay", 0),
            )
        if window_open or window_pending:
            mode = MODE_IDLE
            power_fraction = 0.0

        climate_active = settings.get("climate_control_active", True) and room.get("climate_control_enabled", True)
        waiting_for_temp = has_external_sensor and self._waiting_for_first_reading(area_id)
        waiting_for_data = window_pending or waiting_for_temp
        if (
            climate_active
            and has_external_sensor
            and not waiting_for_temp
            and area_id not in self._had_valid_temp
            and area_id not in self._startup_guard_warned
        ):
            self._startup_guard_warned.add(area_id)
            _LOGGER.warning(
                "Room '%s': no temperature reading within %ds after startup, resuming control (devices stay off)",
                area_id,
                MAX_SENSOR_STALENESS,
            )

        trv_max_temps: list[float] = []
        for eid in get_trv_eids(room.get("devices", [])):
            st = self.hass.states.get(eid)
            if st and st.attributes.get("max_temp") is not None:
                trv_max_temps.append(ha_temp_to_celsius(self.hass, st.attributes["max_temp"]))
        device_max_temp = min(trv_max_temps) if trv_max_temps else None

        ac_min_temps: list[float] = []
        ac_max_temps: list[float] = []
        for eid in get_ac_eids(room.get("devices", [])):
            st = self.hass.states.get(eid)
            if st:
                if st.attributes.get("min_temp") is not None:
                    ac_min_temps.append(ha_temp_to_celsius(self.hass, st.attributes["min_temp"]))
                if st.attributes.get("max_temp") is not None:
                    ac_max_temps.append(ha_temp_to_celsius(self.hass, st.attributes["max_temp"]))
        device_min_temp = max(ac_min_temps) if ac_min_temps else None
        ac_device_max_temp = min(ac_max_temps) if ac_max_temps else None

        cycling_eids = {
            eid for eid in get_trv_eids(room.get("devices", [])) if self._valve_manager.is_entity_cycling(eid)
        }

        heat_source_plan = None
        if (
            room.get("heat_source_orchestration", False)
            and mode == MODE_HEATING
            and has_external_sensor
            and get_trv_eids(room.get("devices", []))
            and get_ac_eids(room.get("devices", []))
        ):
            heat_source_plan = evaluate_heat_sources(
                room_config=room,
                mode=mode,
                power_fraction=power_fraction,
                current_temp=current_temp,
                target_temp=targets.heat,
                outdoor_temp=self.outdoor_temp_effective,
                previous_active_sources=self._heat_source_states.get(area_id, "none"),
                hass=self.hass,
            )
            if heat_source_plan is not None:
                self._heat_source_states[area_id] = heat_source_plan.active_sources
            else:
                self._heat_source_states.pop(area_id, None)
        else:
            self._heat_source_states.pop(area_id, None)

        all_device_eids = get_all_entity_ids(room.get("devices", []))
        compressor_forced_on: set[str] = set()
        compressor_forced_off: set[str] = set()
        compressor_protection_reason: str | None = None

        # With a plan only devices with an active command run. Everything else is
        # parked, also a device the plan leaves out altogether (unknown state,
        # AC below its minimum outdoor temperature): async_apply never touches it.
        plan_parked_eids: set[str] = set()
        plan_unlisted_eids: set[str] = set()
        if heat_source_plan is not None and mode == MODE_HEATING:
            plan_parked_eids = set(all_device_eids) - {cmd.entity_id for cmd in heat_source_plan.commands if cmd.active}
            plan_unlisted_eids = set(all_device_eids) - {cmd.entity_id for cmd in heat_source_plan.commands}

        if all_device_eids and climate_active and not window_open and not force_off:
            for eid in all_device_eids:
                if self._compressor_manager.get_group_for_entity(eid) is None:
                    continue
                if mode != MODE_IDLE:
                    if not self._compressor_manager.check_can_activate(eid):
                        compressor_forced_off.add(eid)
                    else:
                        enforced = self._compressor_manager.get_enforced_action(eid)
                        if enforced is not None and enforced != "idle":
                            if (mode == MODE_HEATING and enforced == "cool") or (
                                mode == MODE_COOLING and enforced == "heat"
                            ):
                                compressor_forced_off.add(eid)
                    # The room still heats, but the heat-source plan parks this
                    # device: for the compressor that is a stop like any other.
                    if eid in plan_parked_eids and self._compressor_manager.check_must_stay_active(eid):
                        compressor_forced_on.add(eid)
                else:
                    if self._compressor_manager.check_must_stay_active(eid):
                        compressor_forced_on.add(eid)

            if compressor_forced_off:
                compressor_protection_reason = "min_off"
            elif compressor_forced_on:
                compressor_protection_reason = "min_run"

            _mode_relevant_eids = (
                set(get_ac_eids(room.get("devices", []))) if mode == MODE_COOLING else set(all_device_eids)
            )
            if compressor_forced_off and _mode_relevant_eids and compressor_forced_off >= _mode_relevant_eids:
                mode = MODE_IDLE
                power_fraction = 0.0
                compressor_forced_off.clear()

        if climate_active and system_type:
            self._residual_tracker.update(
                area_id,
                mode,
                power_fraction,
                self._previous_modes.get(area_id, MODE_IDLE),
                q_residual=q_residual,
            )

        coil_dry = await self._coil_dry_manager.async_process_room(
            area_id=area_id,
            room=room,
            settings=settings,
            mode=mode,
            commandable=climate_active and not waiting_for_data,
            compressor_forced_on=compressor_forced_on,
            compressor_forced_off=compressor_forced_off,
            exclude_eids=cycling_eids,
            force_off=force_off,
            can_activate=self._compressor_manager.check_can_activate,
        )

        offset = room.get("calibration_offset", 0.0)
        if offset != 0.0:
            h_dev = targets.heat + offset if targets.heat is not None else None
            c_dev = targets.cool + offset if targets.cool is not None else None
            rounded_heat = _round_down_to_int(h_dev)
            rounded_cool = _round_down_to_int(c_dev)
            device_targets = TargetTemps(
                heat=float(rounded_heat) if rounded_heat is not None else None,
                cool=float(rounded_cool) if rounded_cool is not None else None,
            )
        else:
            rounded_heat = _round_down_to_int(targets.heat)
            rounded_cool = _round_down_to_int(targets.cool)
            device_targets = TargetTemps(
                heat=float(rounded_heat) if rounded_heat is not None else None,
                cool=float(rounded_cool) if rounded_cool is not None else None,
            )

        last_cmd = self._last_sent_commands.get(area_id, {})
        current_cmd = {
            "mode": mode,
            "power_fraction": round(power_fraction, 2),
            "heat": device_targets.heat,
            "cool": device_targets.cool,
            "heat_sources": self._heat_source_states.get(area_id),
        }

        has_changed = (
            last_cmd.get("mode") != current_cmd["mode"]
            or last_cmd.get("heat") != current_cmd["heat"]
            or last_cmd.get("cool") != current_cmd["cool"]
            or last_cmd.get("heat_sources") != current_cmd["heat_sources"]
            or abs(last_cmd.get("power_fraction", 0) - current_cmd["power_fraction"]) > 0.05
            or bool(compressor_forced_on)
        )

        # --- CONTROLLO STATO REALE SU HOME ASSISTANT CON FORZATURA E COOLDOWN ---
        ac_eids = get_ac_eids(room.get("devices", []))

        for eid in ac_eids:
            try:
                await self.hass.services.async_call(
                    "homeassistant",
                    "update_entity",
                    {"entity_id": eid},
                    blocking=False,
                    context=make_roommind_context(),
                )
            except Exception:
                pass

        real_state_mismatch = False
        for eid in ac_eids:
            st = self.hass.states.get(eid)
            if st is not None and st.state not in ("unavailable", "unknown"):
                if mode == MODE_COOLING and st.state not in ("cool", "auto", "heat_cool"):
                    real_state_mismatch = True
                    break
                elif mode == MODE_HEATING and st.state not in ("heat", "auto", "heat_cool"):
                    real_state_mismatch = True
                    break
                elif mode == MODE_IDLE and st.state != "off":
                    real_state_mismatch = True
                    break

        now = time.monotonic()
        last_corr = self._last_correction_ts.get(area_id, 0.0)
        if real_state_mismatch and (now - last_corr > 180):
            has_changed = True
            self._last_correction_ts[area_id] = now
            self._last_sent_commands.pop(area_id, None)

        if not climate_active or waiting_for_data or current_temp is None:
            mode = MODE_IDLE
            power_fraction = 0.0
            if last_cmd.get("mode") != MODE_IDLE:
                has_changed = True

        if not climate_active:
            mode = MODE_IDLE
            power_fraction = 0.0
        elif waiting_for_data:
            mode = MODE_IDLE
            power_fraction = 0.0
            _LOGGER.debug("Room '%s': no temperature reading or starting up, skipping device control", area_id)
        elif not has_changed:
            _LOGGER.debug("Room '%s': no state change, skipping AC command dispatch", area_id)
        else:
            if not seed_pending:
                self._mode_seeded.add(area_id)
            self._last_sent_commands[area_id] = current_cmd
            try:
                await controller.async_apply(
                    mode,
                    device_targets,
                    power_fraction=power_fraction,
                    current_temp=current_temp,
                    exclude_eids=cycling_eids | coil_dry.controlled_eids,
                    heating_boost_target=_round_down_to_int(device_max_temp),
                    ac_heating_boost_target=_round_down_to_int(ac_device_max_temp),
                    cooling_boost_target=_round_down_to_int(device_min_temp),
                    heat_source_plan=heat_source_plan,
                    compressor_forced_on=compressor_forced_on or None,
                    compressor_forced_off=compressor_forced_off or None,
                    force_off=force_off,
                )
            except Exception:  # noqa: BLE001
                _LOGGER.warning("Room '%s': climate service call failed", area_id, exc_info=True)

        if climate_active and not waiting_for_data and current_temp is not None:
            for eid in all_device_eids:
                if self._compressor_manager.get_group_for_entity(eid) is None:
                    continue
                if eid in cycling_eids:
                    continue
                if eid in compressor_forced_off:
                    self._compressor_manager.update_member(eid, False)
                elif eid in compressor_forced_on:
                    dev_state = self.hass.states.get(eid)
                    actually_on = (
                        dev_state is not None
                        and dev_state.state
                        not in (
                            "off",
                            "unavailable",
                            "unknown",
                        )
                    ) or self._compressor_manager.recently_started(eid, COMPRESSOR_STATE_SETTLE_SECONDS)
                    self._compressor_manager.update_member(eid, actually_on)
                elif eid in coil_dry.compressor_active_eids:
                    # coil_dry_mode="dry" really runs the compressor
                    self._compressor_manager.update_member(eid, True)
                elif eid in plan_parked_eids:
                    # A listed device is stopped by the plan; one the plan leaves out
                    # keeps whatever it was doing, so it counts when it really runs.
                    dev_state = self.hass.states.get(eid)
                    runs = eid in plan_unlisted_eids and dev_state is not None and dev_state.state in _COMPRESSOR_STATES
                    self._compressor_manager.update_member(eid, runs)
                elif mode != MODE_IDLE:
                    self._compressor_manager.update_member(eid, True)
                else:
                    self._compressor_manager.update_member(eid, False)

        has_override = is_override_active(room)
        cover_result = await self._cover_orchestrator.async_process(
            area_id=area_id,
            room=room,
            targets=targets,
            mode=mode,
            current_temp=current_temp,
            outdoor_temp=self.outdoor_temp_effective,
            q_solar=self._current_q_solar,
            predicted_peak_temp=controller.predicted_peak_temp,
            has_override=has_override,
        )

        if mode == MODE_HEATING:
            excluded = set(room.get("valve_protection_exclude", []))
            heating_eids = [eid for eid in get_trv_eids(room.get("devices", [])) if eid not in excluded]
            self._valve_manager.record_heating(heating_eids)

        mpc_active = False
        if has_external_sensor and current_temp is not None:
            try:
                _ch, _cc = get_can_heat_cool(
                    room,
                    self.outdoor_temp_effective,
                    acs_can_heat=check_acs_can_heat(self.hass, room),
                    override_active=is_override_active(room),
                )
                _T_out = (
                    self.outdoor_temp_effective
                    if self.outdoor_temp_effective is not None
                    else DEFAULT_OUTDOOR_TEMP_FALLBACK
                )
                mpc_active = is_mpc_active(
                    self._model_manager,
                    area_id,
                    _ch,
                    _cc,
                    current_temp,
                    _T_out,
                    settings=settings,
                )
            except Exception:  # noqa: BLE001
                mpc_active = False

        display_mode, display_pf = await self._observe_and_train(
            area_id=area_id,
            room=room,
            settings=settings,
            current_temp_raw=current_temp_raw,
            mode=mode,
            power_fraction=power_fraction,
            window_open=window_open,
            raw_open=raw_open,
            q_residual=q_residual,
            shading_factor=shading_factor,
            q_occupancy=q_occupancy,
            q_rebound=q_rebound,
            has_external_sensor=has_external_sensor,
            heat_source_plan=heat_source_plan,
            climate_active=climate_active,
            coil_dry_skip_training=coil_dry.skip_ekf_training,
            compressor_hold=bool(compressor_forced_on),
        )

        return self._build_room_state_dict(
            area_id=area_id,
            room=room,
            settings=settings,
            current_temp=current_temp,
            current_temp_raw=current_temp_raw,
            current_humidity=current_humidity,
            target_temp=target_temp,
            direct_target=controller.direct_target(mode, target_temp) if target_temp is not None else None,
            targets=targets,
            schedule_temp_warnings=schedule_temp_warnings,
            display_mode=display_mode,
            display_pf=display_pf,
            heat_source_plan=heat_source_plan,
            device_max_temp=device_max_temp,
            ac_device_max_temp=ac_device_max_temp,
            device_min_temp=device_min_temp,
            has_external_sensor=has_external_sensor,
            window_open=window_open,
            presence_away=presence_away,
            force_off=force_off,
            mode=mode,
            power_fraction=power_fraction,
            mold_risk_level=mold_risk_level,
            mold_surface_rh=mold_surface_rh,
            mold_prevention_active_room=mold_prevention_active_room,
            mold_prevention_temp_delta=mold_prevention_temp_delta,
            shading_factor=shading_factor,
            q_occupancy=q_occupancy,
            cover_eids=cover_eids,
            cover_result=cover_result,
            mpc_active=mpc_active,
            compressor_protection_reason=compressor_protection_reason,
            coil_dry=coil_dry,
        )

    async def _observe_and_train(
        self,
        *,
        area_id: str,
        room: dict,
        settings: dict,
        current_temp_raw: float | None,
        mode: str,
        power_fraction: float,
        window_open: bool,
        raw_open: bool,
        q_residual: float,
        shading_factor: float | None,
        q_occupancy: float,
        has_external_sensor: bool,
        heat_source_plan: Any | None,
        q_rebound: float | None = None,
        climate_active: bool,
        coil_dry_skip_training: bool = False,
        compressor_hold: bool = False,
    ) -> tuple[str, float]:
        observed_mode: str | None = None
        observed_pf = 0.0

        if not climate_active:
            observed_mode, observed_pf = self._observe_device_action(room)
            if observed_mode is None and self._devices_lack_hvac_action(room):
                inferred = self._infer_device_mode(room)
                observed_mode = inferred
                observed_pf = 1.0 if inferred != MODE_IDLE else 0.0

        managed_display_mode: str | None = None
        managed_display_pf = 0.0
        if climate_active and not has_external_sensor:
            obs_mode, obs_pf = self._observe_device_action(room)
            if obs_mode is not None:
                managed_display_mode = obs_mode
                managed_display_pf = obs_pf
            else:
                managed_display_mode = self._infer_device_mode(room)
                managed_display_pf = 1.0 if managed_display_mode != MODE_IDLE else 0.0

        if climate_active:
            if has_external_sensor:
                ekf_mode: str | None = mode
                ekf_pf = power_fraction
                if heat_source_plan is not None and heat_source_plan.commands:
                    ekf_pf = _orchestrated_training_power(heat_source_plan)
            else:
                ekf_mode = managed_display_mode
                ekf_pf = managed_display_pf
        else:
            ekf_mode = observed_mode
            ekf_pf = observed_pf

        q_residual_training = q_residual
        if climate_active and has_external_sensor and ekf_mode in (MODE_HEATING, MODE_COOLING):
            obs_mode, _ = self._observe_device_action(room)
            if obs_mode == MODE_IDLE:
                ekf_mode = MODE_IDLE
                ekf_pf = 0.0
                q_residual_training = 0.0

        if climate_active and has_external_sensor and ekf_mode in (MODE_HEATING, MODE_COOLING):
            ekf_pf = self._delivered_training_power(room, ekf_mode, ekf_pf, heat_source_plan)

        if ekf_mode in (MODE_HEATING, MODE_COOLING) and ekf_pf == 0.0:
            ekf_mode = MODE_IDLE
            q_residual_training = 0.0

        hold_running: str | None = None
        if climate_active and has_external_sensor and compressor_hold and ekf_mode == MODE_IDLE:
            obs_mode, _ = self._observe_device_action(room)
            if obs_mode in (MODE_HEATING, MODE_COOLING):
                hold_running = obs_mode

        learning_disabled = settings.get("learning_disabled_rooms", [])
        learning_active = area_id not in learning_disabled
        if (
            learning_active
            and not coil_dry_skip_training
            and hold_running is None
            and current_temp_raw is not None
            and self.outdoor_temp_effective is not None
        ):
            can_heat, can_cool = get_can_heat_cool(room, acs_can_heat=check_acs_can_heat(self.hass, room))
            self._ekf_training.process(
                area_id=area_id,
                current_temp=current_temp_raw,
                T_outdoor=self.outdoor_temp_effective,
                ekf_mode=ekf_mode,
                ekf_pf=ekf_pf,
                window_open=window_open,
                raw_open=raw_open,
                q_residual=q_residual_training,
                shading_factor=shading_factor if shading_factor is not None else 0.0,
                q_solar=self._current_q_solar,
                can_heat=can_heat,
                can_cool=can_cool,
                dt_minutes=UPDATE_INTERVAL / 60.0,
                q_occupancy=q_occupancy,
                q_rebound=q_rebound,
            )
        else:
            self._ekf_training.clear(area_id)
        # Same mode the EKF learns from, so the charge matches its labels.
        self._rebound_tracker.update(area_id, hold_running or ekf_mode)

        _prev_mode = self._previous_modes.get(area_id, MODE_IDLE)
        if mode != MODE_IDLE and _prev_mode != mode:
            self._mode_on_since[area_id] = time.time()
        elif mode == MODE_IDLE:
            self._mode_on_since.pop(area_id, None)
        self._previous_modes[area_id] = mode

        if climate_active:
            if has_external_sensor:
                display_mode = mode
                display_pf = power_fraction
            else:
                display_mode = managed_display_mode if managed_display_mode is not None else mode
                display_pf = managed_display_pf if managed_display_mode is not None else power_fraction
        else:
            if observed_mode is not None and observed_mode != MODE_IDLE:
                display_mode = observed_mode
                display_pf = observed_pf
            elif observed_mode is None:
                display_mode = self._infer_device_mode(room)
                display_pf = 1.0 if display_mode != MODE_IDLE else 0.0
            else:
                display_mode = MODE_IDLE
                display_pf = 0.0

        return display_mode, display_pf

    def _build_room_state_dict(
        self,
        *,
        area_id: str,
        room: dict,
        settings: dict,
        current_temp: float | None,
        current_temp_raw: float | None,
        current_humidity: float | None,
        target_temp: float | None,
        direct_target: float | None = None,
        targets: TargetTemps,
        schedule_temp_warnings: list[dict[str, Any]],
        display_mode: str,
        display_pf: float,
        heat_source_plan: HeatSourcePlan | None,
        device_max_temp: float | None,
        ac_device_max_temp: float | None,
        device_min_temp: float | None,
        has_external_sensor: bool,
        window_open: bool,
        presence_away: bool,
        force_off: bool,
        mode: str,
        power_fraction: float,
        mold_risk_level: str | None,
        mold_surface_rh: float | None,
        mold_prevention_active_room: bool,
        mold_prevention_temp_delta: float,
        shading_factor: float | None,
        q_occupancy: float,
        cover_eids: list[str],
        cover_result: CoverResult,
        mpc_active: bool,
        compressor_protection_reason: str | None = None,
        coil_dry: CoilDryRoomResult | None = None,
    ) -> dict:
        _room_devices = room.get("devices", [])
        _direct_eids = get_direct_setpoint_eids(_room_devices)
        # Directness is evaluated only over the devices actually driven in the
        # current mode: cooling only ever commands ACs (TRVs are turned off),
        # heating can command both. Including mode-irrelevant devices in the
        # check previously made a room with a direct AC + a proportional TRV
        # display the TRV's proportional boost value for the AC too, even
        # though async_apply() (see mpc_controller) already sends the correct
        # direct target to the AC. (#device_setpoint mixed-mode display bug)
        _mode_relevant_eids: set[str] = (
            set(get_ac_eids(_room_devices)) if mode == MODE_COOLING else set(get_all_entity_ids(_room_devices))
        )
        _all_direct = bool(_mode_relevant_eids) and _mode_relevant_eids <= _direct_eids
        _direct_offsets = get_setpoint_offsets(_room_devices) if has_external_sensor else {}
        # One shown value only when every driven device agrees on the offset
        _relevant_offsets = {_direct_offsets.get(eid, 0.0) for eid in _mode_relevant_eids}
        _display_offset = _relevant_offsets.pop() if _all_direct and len(_relevant_offsets) == 1 else 0.0

        raw_setpoint = (
            self._compute_device_setpoint_orchestrated(
                heat_source_plan,
                current_temp,
                target_temp,
                device_max_temp,
                ac_device_max_temp,
                direct_eids=_direct_eids,
                direct_offsets=_direct_offsets,
                direct_target=direct_target,
            )
            if heat_source_plan is not None
            else self._compute_device_setpoint(
                mode,
                power_fraction,
                current_temp,
                target_temp,
                has_external_sensor,
                device_max_temp=device_max_temp,
                device_min_temp=device_min_temp,
                has_thermostats=bool(get_trv_eids(_room_devices)),
                has_acs=bool(get_ac_eids(_room_devices)),
                all_direct=_all_direct,
                direct_offset=_display_offset,
                direct_target=direct_target,
            )
        )

        offset = room.get("calibration_offset", 0.0)
        adjusted_setpoint = raw_setpoint + offset if raw_setpoint is not None else None

        return {
            "area_id": area_id,
            "current_temp": current_temp,
            "current_temp_raw": current_temp_raw,
            "current_humidity": current_humidity,
            "target_temp": target_temp,
            "heat_target": targets.heat,
            "cool_target": targets.cool,
            "mode": display_mode,
            "commanded_mode": mode,
            "heating_power": round(display_pf * 100) if display_mode != MODE_IDLE else 0,
            "device_setpoint": (
                round(adjusted_setpoint, 1)
                if _all_direct and adjusted_setpoint is not None
                else _round_down_to_int(adjusted_setpoint)
            ),
            "window_open": window_open,
            **build_override_live(
                room,
                suppressed=is_override_suppressed(room, settings, presence_away),
            ),
            "active_schedule_index": self._get_active_schedule_index(room),
            "confidence": self._model_manager.get_confidence(area_id),
            "mpc_active": mpc_active,
            "presence_away": presence_away,
            "force_off": force_off,
            "mold_risk_level": mold_risk_level,
            "mold_surface_rh": (round(mold_surface_rh, 1) if mold_surface_rh is not None else None),
            "mold_prevention_active": mold_prevention_active_room,
            "mold_prevention_delta": mold_prevention_temp_delta,
            "shading_factor": shading_factor,
            "q_occupancy": q_occupancy,
            "n_observations": self._model_manager.get_n_observations(area_id),
            "blind_position": (self._cover_orchestrator.get_current_position(area_id) if cover_eids else None),
            "cover_auto_paused": (self._cover_orchestrator.is_user_override_active(area_id) if cover_eids else False),
            "cover_override_until": (self._cover_orchestrator.get_user_override_until(area_id) if cover_eids else None),
            "cover_reason": (cover_result.decision.reason if cover_eids else ""),
            "cover_forced_reason": (cover_result.forced_reason if cover_eids else ""),
            "active_cover_schedule_index": (cover_result.active_cover_schedule_index if cover_eids else -1),
            "active_heat_sources": self._heat_source_states.get(area_id),
            "schedule_temp_warnings": schedule_temp_warnings,
            "compressor_protection_active": compressor_protection_reason is not None,
            "compressor_protection_reason": compressor_protection_reason,
            "coil_dry_active": bool(coil_dry and coil_dry.active),
            "coil_dry_phase": coil_dry.phase if coil_dry else None,
            "coil_dry_until": coil_dry.until if coil_dry else None,
            "coil_dry_entities": sorted(coil_dry.controlled_eids) if coil_dry else [],
        }

    @staticmethod
    def _compute_device_setpoint_orchestrated(
        heat_source_plan: HeatSourcePlan,
        current_temp: float | None,
        target_temp: float | None,
        device_max_temp: float | None,
        ac_device_max_temp: float | None,
        direct_eids: set[str] | None = None,
        direct_offsets: dict[str, float] | None = None,
        direct_target: float | None = None,
    ) -> float | None:
        if current_temp is None or target_temp is None:
            return None
        active_cmds = [c for c in heat_source_plan.commands if c.active]
        if not active_cmds:
            return None
        cmd = active_cmds[0]
        if direct_eids and cmd.entity_id in direct_eids:
            base = direct_target if direct_target is not None else target_temp
            return base + (direct_offsets or {}).get(cmd.entity_id, 0.0)
        if cmd.device_type == "thermostat":
            boost = device_max_temp if device_max_temp is not None else HEATING_BOOST_TARGET
        else:
            boost = ac_device_max_temp if ac_device_max_temp is not None else AC_HEATING_BOOST_TARGET
        sp = round(current_temp + cmd.power_fraction * (boost - current_temp), 1)
        sp = max(target_temp, sp)
        sp = min(boost, sp)
        return sp

    @staticmethod
    def _compute_device_setpoint(
        mode: str,
        power_fraction: float,
        current_temp: float | None,
        target_temp: float | None,
        has_external_sensor: bool,
        device_max_temp: float | None = None,
        device_min_temp: float | None = None,
        has_thermostats: bool = True,
        has_acs: bool = False,
        all_direct: bool = False,
        direct_offset: float = 0.0,
        direct_target: float | None = None,
    ) -> float | None:
        if not has_external_sensor or current_temp is None or target_temp is None:
            return None
        if all_direct:
            return (direct_target if direct_target is not None else target_temp) + direct_offset

        if mode == MODE_HEATING:
            default_boost = HEATING_BOOST_TARGET if has_thermostats else AC_HEATING_BOOST_TARGET
            boost = device_max_temp if device_max_temp is not None else default_boost
            if not has_thermostats and not has_acs:
                return None
            sp = round(current_temp + power_fraction * (boost - current_temp), 1)
            sp = max(target_temp, sp)
            sp = min(boost, sp)
            return sp

        if mode == MODE_COOLING and has_acs:
            boost = device_min_temp if device_min_temp is not None else AC_COOLING_BOOST_TARGET
            sp = round(current_temp - power_fraction * (current_temp - boost), 1)
            sp = max(boost, sp)
            sp = min(target_temp, sp)
            return sp

        return None

    def _read_device_temp(self, room: dict) -> float | None:
        for entity_id in get_all_entity_ids(room.get("devices", [])):
            state = self.hass.states.get(entity_id)
            if state and state.attributes.get("current_temperature") is not None:
                try:
                    return float(state.attributes["current_temperature"])
                except (ValueError, TypeError):
                    continue
        return None

    def _observe_device_action(self, room: dict) -> tuple[str | None, float]:
        dominated: str | None = None

        for eid in get_all_entity_ids(room.get("devices", [])):
            state = self.hass.states.get(eid)
            if state is None or state.state in ("unavailable", "unknown"):
                continue

            if state.state == "off":
                if dominated is None:
                    dominated = "idle"
                continue

            action = state.attributes.get("hvac_action")
            if action is None:
                return (None, 0.0)

            if action in ("heating", "preheating"):
                if dominated == "cooling":
                    return (None, 0.0)
                dominated = "heating"
            elif action == "cooling":
                if dominated == "heating":
                    return (None, 0.0)
                dominated = "cooling"
            elif action in ("idle", "off"):
                if dominated is None:
                    dominated = "idle"
            else:
                return (None, 0.0)

        if dominated is None:
            return (None, 0.0)

        pf = 1.0 if dominated in ("heating", "cooling") else 0.0
        return (dominated, pf)

    def _delivered_training_power(
        self, room: dict, mode: str, commanded_pf: float, heat_source_plan: Any | None
    ) -> float:
        """Share of the driven devices that report running in ``mode`` (EKF training label).

        Driven devices: the plan's active commands with orchestration, else every device
        that is not off. A parked device still running (compressor hold) delivers, so it
        counts as well. A heat pump that is defrosting delivers no heat (0.0).
        Devices without a usable ``hvac_action`` keep their commanded pf;
        no observable device at all keeps ``commanded_pf``.
        """
        running_actions = ("heating", "preheating") if mode == MODE_HEATING else ("cooling",)
        if heat_source_plan is not None and heat_source_plan.commands:
            driven = {c.entity_id: c.power_fraction for c in heat_source_plan.commands if c.active}
            parked = [c.entity_id for c in heat_source_plan.commands if not c.active]
        else:
            driven = dict.fromkeys(get_all_entity_ids(room.get("devices", [])), commanded_pf)
            parked = []
        labels: list[float] = []
        for eid, pf in driven.items():
            state = self.hass.states.get(eid)
            if state is None or state.state in ("unavailable", "unknown", "off"):
                continue
            action = state.attributes.get("hvac_action")
            if action in running_actions:
                labels.append(1.0)
            elif action in ("idle", "off", "heating", "preheating", "cooling", "defrosting"):
                labels.append(0.0)
            else:
                labels.append(pf)
        for eid in parked:
            state = self.hass.states.get(eid)
            if state is not None and state.attributes.get("hvac_action") in running_actions:
                labels.append(1.0)
        return sum(labels) / len(labels) if labels else commanded_pf

    def _devices_lack_hvac_action(self, room: dict) -> bool:
        for eid in get_all_entity_ids(room.get("devices", [])):
            state = self.hass.states.get(eid)
            if state is None or state.state in ("unavailable", "unknown", "off"):
                continue
            if state.attributes.get("hvac_action") is None:
                return True
        return False

    def _infer_device_mode(self, room: dict) -> str:
        for eid in get_all_entity_ids(room.get("devices", [])):
            state = self.hass.states.get(eid)
            if state is None or state.state in ("unavailable", "unknown", "off"):
                continue
            current = state.attributes.get("current_temperature")
            setpoint = state.attributes.get("temperature")
            if state.state == "heat":
                if current is not None and setpoint is not None and current >= setpoint:
                    continue
                return MODE_HEATING
            if state.state == "cool":
                if current is not None and setpoint is not None and current <= setpoint:
                    continue
                return MODE_COOLING
            if state.state in ("heat_cool", "auto"):
                # Range mode: the device fires only once the room temp leaves
                # the [low, high] dead-band.  Above high → cooling, below low →
                # heating, within the band (or setpoints missing) → idle.
                low = state.attributes.get("target_temp_low")
                high = state.attributes.get("target_temp_high")
                if current is not None and high is not None and current > high:
                    return MODE_COOLING
                if current is not None and low is not None and current < low:
                    return MODE_HEATING
                continue
        return MODE_IDLE

    def _read_window_states(self, room: dict) -> dict[str, str | None]:
        """Return the raw state of each configured window/door sensor."""
        states: dict[str, str | None] = {}
        for entity_id in room.get("window_sensors", []):
            state = self.hass.states.get(entity_id)
            states[entity_id] = state.state if state else None
        return states

    def _missing_window_sensors(self, states: dict[str, str | None]) -> frozenset[str]:
        """Return sensors without a state that HA also does not know in the entity registry.

        Only once HA is fully running: while it is still starting, entities may
        not be loaded yet, so a missing state means "not yet", not "gone".
        """
        stateless = [entity_id for entity_id, state in states.items() if state is None]
        if not stateless or self.hass.state is not CoreState.running:
            return frozenset()
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(self.hass)
        return frozenset(entity_id for entity_id in stateless if registry.async_get(entity_id) is None)

    def _is_presence_away(self, room: dict, settings: dict) -> bool:
        from .utils.presence_utils import is_presence_away

        return is_presence_away(self.hass, room, settings)

    def _get_active_schedule_index(self, room: dict) -> int:
        return resolve_schedule_index(self.hass, room)

    def _warn_block_temp_rejected(self, area_id: str, field: str, raw: Any) -> None:
        """Log a rejected schedule block temperature once per distinct value.

        The coordinator re-resolves every 30s, so an unthrottled warning would
        add ~2880 identical lines per day for a single typo.
        """
        key = (area_id, field, str(raw))
        if key in self._block_temp_warned:
            return
        self._block_temp_warned.add(key)
        _LOGGER.warning(
            "Room '%s': schedule block %s=%r is outside the plausible range "
            "%.1f-%.1f C and was ignored - falling back to the comfort target. "
            "Check the schedule helper for a typo.",
            area_id,
            field,
            raw,
            MIN_TARGET_TEMP,
            MAX_TARGET_TEMP,
        )

    def _resolve_target_temps(
        self,
        room: dict,
        settings: dict,
        schedule_blocks: dict | None = None,
        schedule_entity_id: str | None = None,
    ) -> TargetTemps:
        from .utils.schedule_utils import find_active_block, sanitize_block_temp

        override_heat = room.get("override_heat")
        override_cool = room.get("override_cool")
        override_until = room.get("override_until")
        if override_heat is not None or override_cool is not None:
            if override_until is None or time.time() < override_until:
                presence_away_now = not room.get("ignore_presence", False) and self._is_presence_away(room, settings)
                if not (presence_away_now and bool(settings.get("presence_clears_override", False))):
                    return TargetTemps(heat=override_heat, cool=override_cool)
            else:
                area_id = room.get("area_id", "unknown")
                store = self.hass.data[DOMAIN]["store"]
                self.hass.async_create_task(
                    store.async_update_room(
                        area_id,
                        {
                            "override_heat": None,
                            "override_cool": None,
                            "override_until": None,
                            "override_type": None,
                        },
                    )
                )

        vacation_until = settings.get("vacation_until")
        if vacation_until is not None:
            if time.time() < vacation_until:
                vacation_temp = settings.get("vacation_temp")
                if vacation_temp is not None:
                    t = float(vacation_temp)
                    eco_cool = room.get("eco_cool", DEFAULT_ECO_COOL)
                    return TargetTemps(heat=t, cool=max(t, eco_cool))
            else:
                self.hass.async_create_task(
                    self.hass.data[DOMAIN]["store"].async_save_settings(
                        {
                            "vacation_until": None,
                        }
                    )
                )

        if not room.get("ignore_presence", False) and self._is_presence_away(room, settings):
            if settings.get("presence_away_action", "eco") == "off":
                return TargetTemps(heat=None, cool=None)
            return TargetTemps(
                heat=room.get("eco_heat", room.get("eco_temp", DEFAULT_ECO_HEAT)),
                cool=room.get("eco_cool", DEFAULT_ECO_COOL),
            )

        comfort_heat = room.get("comfort_heat", room.get("comfort_temp", DEFAULT_COMFORT_HEAT))
        comfort_cool = room.get("comfort_cool", DEFAULT_COMFORT_COOL)
        eco_heat = room.get("eco_heat", room.get("eco_temp", DEFAULT_ECO_HEAT))
        eco_cool = room.get("eco_cool", DEFAULT_ECO_COOL)

        if not schedule_entity_id:
            return TargetTemps(heat=comfort_heat, cool=comfort_cool)

        state = self.hass.states.get(schedule_entity_id)
        state_unavailable = state is None or state.state in ("unavailable", "unknown")

        if state_unavailable:
            if schedule_blocks is None:
                return TargetTemps(heat=comfort_heat, cool=comfort_cool)
            if find_active_block(schedule_blocks, time.time()) is None:
                if settings.get("schedule_off_action", "eco") == "off":
                    return TargetTemps(heat=None, cool=None)
                return TargetTemps(heat=eco_heat, cool=eco_cool)

        if state_unavailable or state.state == SCHEDULE_STATE_ON:
            if schedule_blocks is not None:
                block_data = find_active_block(schedule_blocks, time.time()) or {}
                heat_temp = block_data.get("heat_temperature")
                cool_temp = block_data.get("cool_temperature")
                block_temp = block_data.get("temperature")
            else:
                heat_temp = state.attributes.get("heat_temperature")
                cool_temp = state.attributes.get("cool_temperature")
                block_temp = state.attributes.get("temperature")

            # Block values are unvalidated user YAML: a typo (110 instead of 11)
            # would be heated against for hours, so implausible values are
            # dropped in favour of the comfort fallback (#395).
            converter = partial(ha_temp_to_celsius, self.hass)
            area_id = room.get("area_id", "unknown")

            def _read(raw: Any, field: str) -> float | None:
                if raw is None:
                    return None
                val = sanitize_block_temp(raw, converter)
                if val is None:
                    self._warn_block_temp_rejected(area_id, field, raw)
                return val

            if heat_temp is not None or cool_temp is not None:
                h: float = comfort_heat
                c: float = comfort_cool
                if heat_temp is not None:
                    parsed_heat = _read(heat_temp, "heat_temperature")
                    if parsed_heat is not None:
                        h = parsed_heat
                if cool_temp is not None:
                    parsed_cool = _read(cool_temp, "cool_temperature")
                    if parsed_cool is not None:
                        c = parsed_cool
                return TargetTemps(heat=h, cool=c)
            if block_temp is not None:
                parsed_block_temp = _read(block_temp, "temperature")
                if parsed_block_temp is not None:
                    return TargetTemps(heat=parsed_block_temp, cool=parsed_block_temp)
            return TargetTemps(heat=comfort_heat, cool=comfort_cool)

        if settings.get("schedule_off_action", "eco") == "off":
            return TargetTemps(heat=None, cool=None)
        return TargetTemps(heat=eco_heat, cool=eco_cool)

    async def async_room_added(self, room: dict) -> None:
        area_id = room["area_id"]
        has_covers = bool(room.get("covers"))

        if area_id not in self._entity_areas and hasattr(self, "async_add_entities") and self.async_add_entities:
            from .sensor import _create_room_entities

            entities = _create_room_entities(self, area_id)
            self.async_add_entities(entities)
            self._entity_areas.add(area_id)

        if (
            area_id not in self._climate_entity_areas
            and hasattr(self, "async_add_climate_entities")
            and self.async_add_climate_entities
        ):
            from .climate import _create_room_climates

            self.async_add_climate_entities(_create_room_climates(self, area_id))
            self._climate_entity_areas.add(area_id)

        if (
            area_id not in self._climate_control_switch_areas
            and hasattr(self, "async_add_switch_entities")
            and self.async_add_switch_entities
        ):
            from .switch import ClimateMindClimateControlSwitch

            self.async_add_switch_entities([ClimateMindClimateControlSwitch(self, area_id)])
            self._climate_control_switch_areas.add(area_id)

        if has_covers:
            if (
                area_id not in self._switch_entity_areas
                and hasattr(self, "async_add_switch_entities")
                and self.async_add_switch_entities
            ):
                from .switch import _create_room_switches

                self.async_add_switch_entities(_create_room_switches(self, area_id))
                self._switch_entity_areas.add(area_id)
            if (
                area_id not in self._binary_sensor_entity_areas
                and hasattr(self, "async_add_binary_sensor_entities")
                and self.async_add_binary_sensor_entities
            ):
                from .binary_sensor import _create_room_binary_sensors

                self.async_add_binary_sensor_entities(_create_room_binary_sensors(self, area_id))
                self._binary_sensor_entity_areas.add(area_id)

        await self.async_request_refresh()

    async def async_room_removed(self, area_id: str) -> None:
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(self.hass)
        owned_uids = {f"{DOMAIN}_{area_id}{suffix}" for suffix in ROOM_ENTITY_SUFFIXES}
        entries_to_remove = [
            entity_entry.entity_id
            for entity_entry in registry.entities.values()
            if entity_entry.platform == DOMAIN
            and isinstance(entity_entry.unique_id, str)
            and entity_entry.unique_id in owned_uids
        ]

        for entity_id in entries_to_remove:
            registry.async_remove(entity_id)

        self._window_manager.remove_room(area_id)
        self._previous_modes.pop(area_id, None)
        self._override_seeded_at.pop(area_id, None)
        self._last_valid_temps.pop(area_id, None)
        self._had_valid_temp.discard(area_id)
        self._startup_guard_warned.discard(area_id)
        self._block_temp_warned = {k for k in self._block_temp_warned if k[0] != area_id}
        self._ekf_training.remove_room(area_id)
        self._pending_predictions.pop(area_id, None)
        self._residual_tracker.remove_room(area_id)
        self._rebound_tracker.remove_room(area_id)
        self._coil_dry_manager.remove_room(area_id)
        self._cover_orchestrator.remove_room(area_id)
        self._entity_areas.discard(area_id)
        self._mode_on_since.pop(area_id, None)
        self._mode_seeded.discard(area_id)
        self._switch_entity_areas.discard(area_id)
        self._climate_control_switch_areas.discard(area_id)
        self._binary_sensor_entity_areas.discard(area_id)
        self._climate_entity_areas.discard(area_id)
        self._model_manager.remove_room(area_id)
        self._heat_source_states.pop(area_id, None)
        self._last_sent_commands.pop(area_id, None)
        self._last_correction_ts.pop(area_id, None)
        if self._history_store:
            await self.hass.async_add_executor_job(self._history_store.remove_room, area_id)

        await self.async_request_refresh()

    def cleanup_orphaned_entities(self) -> None:
        from homeassistant.helpers import entity_registry as er

        store = self.hass.data[DOMAIN]["store"]
        rooms = store.get_rooms()
        registry = er.async_get(self.hass)

        global_uids = {f"{DOMAIN}_vacation"}

        to_remove: list[str] = []
        for entity_entry in registry.entities.values():
            # Foreign platforms (e.g. input_boolean helpers) can share the roommind_ prefix (#433).
            if entity_entry.platform != DOMAIN:
                continue
            uid = entity_entry.unique_id
            if not isinstance(uid, str) or not uid.startswith(f"{DOMAIN}_"):
                continue
            if uid in global_uids:
                continue

            parts = uid.removeprefix(f"{DOMAIN}_")
            matched = _match_room_entity(parts, rooms)

            if matched is None:
                to_remove.append(entity_entry.entity_id)
                continue

            matched_area, suffix = matched
            room = rooms[matched_area]

            if suffix in COVER_ENTITY_SUFFIXES and not room.get("covers"):
                to_remove.append(entity_entry.entity_id)

        for eid in to_remove:
            _LOGGER.info("Removing orphaned entity: %s", eid)
            registry.async_remove(eid)

    def reset_thermal_room(self, area_id: str) -> None:
        self._model_manager.remove_room(area_id)
        self._ekf_training.last_temps.pop(area_id, None)
        self._residual_tracker.clear_room(area_id)

    def reset_thermal_all(self) -> list[str]:
        room_ids = self._model_manager.get_room_ids()
        self._model_manager = RoomModelManager()
        self._ekf_training.set_model_manager(self._model_manager)
        self._cover_orchestrator.set_model_manager(self._model_manager)
        self._ekf_training.last_temps.clear()
        self._residual_tracker.clear_all()
        return room_ids

    def boost_learning(self, area_id: str) -> int:
        return self._model_manager.boost_learning(area_id)

    def clear_cover_override(self, area_id: str) -> None:
        self._cover_orchestrator.clear_user_override(area_id)

    @property
    def history_store(self) -> HistoryStore | None:
        return self._history_store

    def _collect_member_room_modes(
        self,
        members: list[str],
        room_states: dict[str, dict],
        rooms_config: dict[str, dict],
        settings: dict,
    ) -> list[str]:
        if not settings.get("climate_control_active", True):
            return []
        member_set = set(members)
        modes: list[str] = []
        for area_id, room in rooms_config.items():
            if not room.get("climate_control_enabled", True):
                continue
            if room.get("is_outdoor", False):
                continue
            device_eids = {d.get("entity_id", "") for d in room.get("devices", [])}
            if not (device_eids & member_set):
                continue
            rs = room_states.get(area_id)
            if not rs:
                continue
            commanded = rs.get("commanded_mode", rs.get("mode", MODE_IDLE))

            if (
                commanded == MODE_HEATING
                and room.get("heat_source_orchestration", False)
                and not room_contributes_to_group(
                    room.get("devices", []),
                    member_set,
                    rs.get("active_heat_sources"),
                )
            ):
                continue

            modes.append(commanded)
        return modes

    def _waiting_for_first_reading(self, area_id: str) -> bool:
        return area_id not in self._had_valid_temp and time.monotonic() - self._startup_ts < MAX_SENSOR_STALENESS

    def _any_member_room_waiting(self, members: list[str], rooms_config: dict[str, dict]) -> bool:
        member_set = set(members)
        for area_id, room in rooms_config.items():
            waiting_for_temp = bool(room.get("temperature_sensor")) and self._waiting_for_first_reading(area_id)
            if not waiting_for_temp and not self._window_manager.is_pending(area_id):
                continue
            if room.get("is_outdoor", False) or not room.get("climate_control_enabled", True):
                continue
            device_eids = {d.get("entity_id", "") for d in room.get("devices", [])}
            if device_eids & member_set:
                return True
        return False

    def _resolve_master_hvac_mode(self, master_entity: str, action: str) -> str | None:
        state = self.hass.states.get(master_entity)
        if state is None or state.state in ("unavailable", "unknown"):
            return None
        supported = state.attributes.get("hvac_modes", [])
        if action == "idle":
            if "off" in supported:
                return "off"
            return None
        if action in supported:
            return action
        if "heat_cool" in supported:
            return "heat_cool"
        if "auto" in supported:
            return "auto"
        return None

    async def _async_wake_member_zone(
        self,
        group: CompressorGroupConfig,
        room_states: dict[str, dict],
        rooms_config: dict[str, dict],
    ) -> None:
        for eid in group.members:
            state = self.hass.states.get(eid)
            if state is not None and state.state not in ("off", "unavailable", "unknown"):
                return

        member_set = set(group.members)
        wake_eid: str | None = None

        for area_id, room in rooms_config.items():
            if not room.get("climate_control_enabled", True):
                continue
            if room.get("is_outdoor", False):
                continue
            rs = room_states.get(area_id)
            if not rs:
                continue
            commanded = rs.get("commanded_mode", rs.get("mode", MODE_IDLE))
            if commanded == MODE_IDLE:
                continue
            for dev in room.get("devices", []):
                eid = dev.get("entity_id", "")
                if eid not in member_set:
                    continue
                zone_state = self.hass.states.get(eid)
                if zone_state and "fan_only" in (zone_state.attributes.get("hvac_modes") or []):
                    wake_eid = eid
                    break
            if wake_eid:
                break

        if not wake_eid:
            for eid in group.members:
                zone_state = self.hass.states.get(eid)
                if zone_state and "fan_only" in (zone_state.attributes.get("hvac_modes") or []):
                    wake_eid = eid
                    break

        if not wake_eid:
            return

        try:
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": wake_eid, "hvac_mode": "fan_only"},
                blocking=True,
                context=make_roommind_context(),
            )
            self._compressor_manager.update_member(wake_eid, True)
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Master '%s' (group '%s'): failed to pre-activate zone '%s'",
                group.master_entity,
                group.name,
                wake_eid,
                exc_info=True,
            )

    async def _async_control_master_devices(
        self,
        room_states: dict[str, dict],
        rooms_config: dict[str, dict],
        settings: dict,
    ) -> None:
        if not settings.get("climate_control_active", True):
            return
        for gid, group in self._compressor_manager.get_groups().items():
            if not group.master_entity and not group.action_script and not group.enforce_uniform_mode:
                continue
            try:
                has_master = bool(group.master_entity)
                master_state = None
                if has_master:
                    master_state = self.hass.states.get(group.master_entity)
                    if master_state is None or master_state.state in ("unavailable", "unknown"):
                        continue

                modes = self._collect_member_room_modes(
                    group.members,
                    room_states,
                    rooms_config,
                    settings,
                )

                new_action = resolve_master_action(
                    modes,
                    group.conflict_resolution,
                    self.outdoor_temp_effective,
                    settings.get("outdoor_heating_max", DEFAULT_OUTDOOR_HEATING_MAX),
                )

                if new_action == "idle" and self._any_member_room_waiting(group.members, rooms_config):
                    continue

                state = self._compressor_manager.get_state(gid)
                prev_action = state.master_action if state else None

                if has_master:
                    if not self._compressor_manager.check_master_can_switch(gid, new_action):
                        continue

                    resolved_mode = self._resolve_master_hvac_mode(group.master_entity, new_action)
                    if resolved_mode is None:
                        continue

                    if master_state is not None and master_state.state == resolved_mode:
                        self._compressor_manager.set_master_action(gid, new_action)
                        if new_action != prev_action and group.action_script:
                            await self._call_action_script(group, state, new_action)
                        continue

                    if new_action != "idle" and (prev_action is None or prev_action == "idle"):
                        await self._async_wake_member_zone(group, room_states, rooms_config)

                    try:
                        await self.hass.services.async_call(
                            "climate",
                            "set_hvac_mode",
                            {
                                "entity_id": group.master_entity,
                                "hvac_mode": resolved_mode,
                            },
                            blocking=True,
                            context=make_roommind_context(),
                        )
                    except Exception:  # noqa: BLE001
                        continue

                if new_action != prev_action and group.action_script:
                    await self._call_action_script(group, state, new_action)

                if new_action != prev_action:
                    label = group.master_entity or group.action_script or f"group:{group.id}"
                    _LOGGER.info("Master '%s' (group '%s'): %s -> %s", label, group.name, prev_action, new_action)
                self._compressor_manager.set_master_action(gid, new_action)

            except Exception:  # noqa: BLE001
                _LOGGER.warning("Master device control failed for group '%s'", group.name, exc_info=True)

    async def _call_action_script(
        self,
        group: CompressorGroupConfig,
        state: CompressorGroupState | None,
        new_action: str,
    ) -> None:
        script_state = self.hass.states.get(group.action_script)
        if script_state is None:
            return
        try:
            await self.hass.services.async_call(
                "script",
                "turn_on",
                {
                    "entity_id": group.action_script,
                    "variables": {
                        "action": new_action,
                        "master_entity": group.master_entity,
                        "members": group.members,
                        "active_members": [eid for eid in group.members if state and eid in state.active_members],
                    },
                },
                blocking=False,
                context=make_roommind_context(),
            )
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Master group '%s': action script '%s' failed", group.name, group.action_script, exc_info=True
            )


ClimateMindCoordinator = RoomMindCoordinator
