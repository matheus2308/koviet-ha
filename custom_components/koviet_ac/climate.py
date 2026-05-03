from __future__ import annotations

import json
import logging
import threading
import uuid
from typing import Any

import paho.mqtt.client as mqtt
import voluptuous as vol

from homeassistant.components.climate import (
    PLATFORM_SCHEMA,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_DEVICE_SN,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_USERNAME,
    CONF_NAME,
    CMD_GET_STATE,
    CMD_SET_STATE,
    MODE_COOL,
    MODE_DRY,
    MODE_FAN,
    MQTT_BROKER,
    MQTT_PORT,
    MQTT_WS_PATH,
    WIND_HIGH,
    WIND_LOW,
    WIND_MEDIUM,
)

_LOGGER = logging.getLogger(__name__)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_MQTT_USERNAME): cv.string,
        vol.Required(CONF_MQTT_PASSWORD): cv.string,
        vol.Required(CONF_DEVICE_SN): cv.string,
        vol.Optional(CONF_NAME, default="KOVIET AC"): cv.string,
    }
)

_HVAC_MODES = [HVACMode.OFF, HVACMode.COOL, HVACMode.DRY, HVACMode.FAN_ONLY]
_FAN_MODES = ["low", "medium", "high"]

_MODE_HA_TO_DEV = {HVACMode.COOL: MODE_COOL, HVACMode.DRY: MODE_DRY, HVACMode.FAN_ONLY: MODE_FAN}
_MODE_DEV_TO_HA = {v: k for k, v in _MODE_HA_TO_DEV.items()}

_FAN_HA_TO_DEV = {"low": WIND_LOW, "medium": WIND_MEDIUM, "high": WIND_HIGH}
_FAN_DEV_TO_HA = {v: k for k, v in _FAN_HA_TO_DEV.items()}

_SUPPORTED = (
    ClimateEntityFeature.TARGET_TEMPERATURE
    | ClimateEntityFeature.FAN_MODE
)
# TURN_ON / TURN_OFF added in HA 2024.2 — add if available
try:
    _SUPPORTED |= ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
except AttributeError:
    pass


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info=None,
) -> None:
    entity = KovietClimate(
        hass,
        name=config[CONF_NAME],
        device_sn=config[CONF_DEVICE_SN],
        mqtt_username=config[CONF_MQTT_USERNAME],
        mqtt_password=config[CONF_MQTT_PASSWORD],
    )
    async_add_entities([entity])
    await hass.async_add_executor_job(entity.mqtt_connect)


class KovietClimate(ClimateEntity):
    _attr_hvac_modes = _HVAC_MODES
    _attr_fan_modes = _FAN_MODES
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_supported_features = _SUPPORTED
    _attr_target_temperature_step = 1.0
    _attr_min_temp = 60.0
    _attr_max_temp = 86.0
    _attr_should_poll = False

    def __init__(self, hass, name, device_sn, mqtt_username, mqtt_password):
        self.hass = hass
        self._attr_name = name
        self._attr_unique_id = f"koviet_{device_sn}"
        self._sn = device_sn
        self._mqtt_user = mqtt_username
        self._mqtt_pass = mqtt_password

        self._req_topic = f"dev/I4SEASON/{device_sn}/command/request"
        self._rep_topic = f"dev/I4SEASON/{device_sn}/command/reply"

        # State
        self._attr_hvac_mode = None
        self._attr_current_temperature = None
        self._attr_target_temperature = None
        self._attr_fan_mode = None
        self._last_dev_mode = MODE_FAN  # remembered mode when AC is off

        self._client: mqtt.Client | None = None

    # ------------------------------------------------------------------
    # MQTT connection (runs in executor thread)
    # ------------------------------------------------------------------

    def mqtt_connect(self) -> None:
        client_id = f"ha_{uuid.uuid4().hex[:8]}"
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            transport="websockets",
        )
        self._client.username_pw_set(self._mqtt_user, self._mqtt_pass)
        self._client.tls_set()
        self._client.ws_set_options(path=MQTT_WS_PATH)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect
        self._client.reconnect_delay_set(5, 60)
        try:
            self._client.connect(MQTT_BROKER, MQTT_PORT)
            self._client.loop_start()
        except Exception as exc:
            _LOGGER.error("KOVIET MQTT connect failed: %s", exc)

    def _on_connect(self, client, userdata, flags, rc, props=None):
        _LOGGER.info("KOVIET MQTT connected (rc=%s)", rc)
        client.subscribe(self._rep_topic, qos=0)
        self._publish({"cmd": CMD_GET_STATE, "user": "ha"})

    def _on_disconnect(self, client, userdata, disconnect_flags, rc=None, props=None):
        _LOGGER.warning("KOVIET MQTT disconnected (rc=%s) — will reconnect", rc)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload)
        except json.JSONDecodeError:
            return
        if payload.get("cmd") in (3, 4):  # full state or partial update
            self._apply_state(payload.get("result", {}))

    # ------------------------------------------------------------------
    # State update (called from MQTT thread → schedule HA update safely)
    # ------------------------------------------------------------------

    def _apply_state(self, result: dict) -> None:
        changed = False

        if "mode" in result:
            self._last_dev_mode = result["mode"]
            if self._attr_hvac_mode not in (None, HVACMode.OFF):
                new = _MODE_DEV_TO_HA.get(result["mode"], HVACMode.COOL)
                if self._attr_hvac_mode != new:
                    self._attr_hvac_mode = new
                    changed = True

        if "poweron" in result:
            if not result["poweron"]:
                new_mode = HVACMode.OFF
            else:
                new_mode = _MODE_DEV_TO_HA.get(self._last_dev_mode, HVACMode.COOL)
            if self._attr_hvac_mode != new_mode:
                self._attr_hvac_mode = new_mode
                changed = True

        if "temperature" in result:
            v = float(result["temperature"])
            if self._attr_current_temperature != v:
                self._attr_current_temperature = v
                changed = True

        if "templevel" in result:
            v = float(result["templevel"])
            if self._attr_target_temperature != v:
                self._attr_target_temperature = v
                changed = True

        if "windlevel" in result:
            v = _FAN_DEV_TO_HA.get(result["windlevel"], "medium")
            if self._attr_fan_mode != v:
                self._attr_fan_mode = v
                changed = True

        if changed:
            self.hass.loop.call_soon_threadsafe(self.schedule_update_ha_state)

    # ------------------------------------------------------------------
    # HA ClimateEntity interface
    # ------------------------------------------------------------------

    def set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            self._send_state({"poweron": False})
        else:
            dev_mode = _MODE_HA_TO_DEV.get(hvac_mode)
            if dev_mode is None:
                return
            if self._attr_hvac_mode == HVACMode.OFF:
                self._send_state({"poweron": True})
            self._send_state({"mode": dev_mode})
        self._attr_hvac_mode = hvac_mode
        self.schedule_update_ha_state()

    def set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        self._send_state({"templevel": int(temp)})
        self._attr_target_temperature = float(temp)
        self.schedule_update_ha_state()

    def set_fan_mode(self, fan_mode: str) -> None:
        dev = _FAN_HA_TO_DEV.get(fan_mode)
        if dev is None:
            return
        self._send_state({"windlevel": dev})
        self._attr_fan_mode = fan_mode
        self.schedule_update_ha_state()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _send_state(self, state: dict) -> None:
        self._publish({"cmd": CMD_SET_STATE, "user": "ha", "data": {"state": state}})

    def _publish(self, payload: dict) -> None:
        if self._client is None:
            return
        self._client.publish(self._req_topic, json.dumps(payload), qos=0)
