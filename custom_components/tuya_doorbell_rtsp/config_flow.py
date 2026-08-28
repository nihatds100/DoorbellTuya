"""Config flow: Tuya credentials -> camera selection."""
from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
import homeassistant.helpers.config_validation as cv

from .bridge import TuyaBridge, BridgeError
from .const import (
    CONF_CAMERAS, CONF_COUNTRY_CODE, CONF_DEVICE_IP, CONF_EMAIL, CONF_PASSWORD,
    CONF_LOCAL_KEYS, CONF_REGION, CONF_RTSP_PORT, DEFAULT_RTSP_PORT, DOMAIN, REGIONS,
)


class TuyaDoorbellRtspFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the setup flow."""

    VERSION = 1

    def __init__(self):
        self._data = {}
        self._cameras = []

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            bridge = TuyaBridge(
                self.hass, user_input[CONF_REGION], user_input[CONF_EMAIL],
                user_input[CONF_PASSWORD], user_input[CONF_COUNTRY_CODE],
                user_input.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT),
            )
            try:
                await bridge.async_authenticate()
            except BridgeError:
                errors["base"] = "auth_failed"
            else:
                self._cameras = bridge.list_cameras()
                if not self._cameras:
                    errors["base"] = "no_cameras"
                else:
                    self._data = dict(user_input)
                    return await self.async_step_cameras()
        schema = vol.Schema({
            vol.Required(CONF_EMAIL): str,
            vol.Required(CONF_PASSWORD): str,
            vol.Required(CONF_COUNTRY_CODE): str,
            vol.Required(CONF_REGION, default="eu-central"): vol.In(REGIONS),
            vol.Optional(CONF_RTSP_PORT, default=DEFAULT_RTSP_PORT): int,
            vol.Optional(CONF_DEVICE_IP): str,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_cameras(self, user_input=None):
        if user_input is not None:
            self._data[CONF_CAMERAS] = user_input[CONF_CAMERAS]
            bridge = TuyaBridge(
                self.hass, self._data[CONF_REGION], self._data[CONF_EMAIL],
                self._data[CONF_PASSWORD], self._data[CONF_COUNTRY_CODE],
                self._data.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT),
            )
            local_keys = {}
            for dev_id in user_input[CONF_CAMERAS]:
                try:
                    detail = await bridge.async_camera_detail(dev_id)
                except BridgeError:
                    detail = None
                if detail:
                    local_keys[dev_id] = detail
            self._data[CONF_LOCAL_KEYS] = local_keys
            await self.async_set_unique_id(
                f"{self._data[CONF_EMAIL]}_{self._data[CONF_REGION]}"
            )
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"Tuya Doorbell RTSP ({self._data[CONF_EMAIL]})",
                data=self._data,
            )
        choices = {c["deviceId"]: c.get("deviceName", c["deviceId"]) for c in self._cameras}
        schema = vol.Schema({
            vol.Required(CONF_CAMERAS, default=list(choices)): cv.multi_select(choices)
        })
        return self.async_show_form(step_id="cameras", data_schema=schema)
