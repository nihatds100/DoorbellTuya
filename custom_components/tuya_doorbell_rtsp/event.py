"""Event entities: fire on local datapoint pushes (button press, motion, etc.)."""
from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import CONF_CAMERAS, CONF_DEVICE_IP, DOMAIN


def signal(entry_id, device_id):
    return f"{DOMAIN}_{entry_id}_{device_id}_dp"


async def async_setup_entry(hass, entry, async_add_entities):
    store = hass.data[DOMAIN][entry.entry_id]
    details = store["details"]
    entities = []
    if entry.data.get(CONF_DEVICE_IP):
        for dev_id in entry.data.get(CONF_CAMERAS, []):
            detail = details.get(dev_id)
            if not detail:
                continue
            entities.append(TuyaDPEvent(entry.entry_id, detail))
    async_add_entities(entities)


class TuyaDPEvent(EventEntity):
    """Fires whenever the device pushes a datapoint update (async event)."""

    _attr_has_entity_name = True
    _attr_name = "Datapoint"
    _attr_event_types = ["dp"]

    def __init__(self, entry_id, detail):
        self._entry_id = entry_id
        self._device_id = detail["deviceId"]
        self._attr_unique_id = f"{self._device_id}_dp_event"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=detail.get("deviceName", "Tuya Doorbell"),
            manufacturer="Tuya",
            model=detail.get("productId"),
        )

    async def async_added_to_hass(self):
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal(self._entry_id, self._device_id), self._handle
            )
        )

    def _handle(self, dp, value):
        # dispatcher may run us in an executor thread; hop to the loop.
        self.hass.loop.call_soon_threadsafe(self._update, str(dp), value)

    @callback
    def _update(self, dp, value):
        self._trigger_event("dp", {"dp": dp, "value": value})
        self.async_write_ha_state()
