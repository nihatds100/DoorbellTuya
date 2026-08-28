"""Diagnostic sensor exposing the local RTSP URLs (for NVR / app / info)."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory

from ._net import host_ip
from .const import CONF_CAMERAS, CONF_RTSP_PORT, DEFAULT_RTSP_PORT, DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    store = hass.data[DOMAIN][entry.entry_id]
    bridge = store["bridge"]
    mgr = store["go2rtc"] if store.get("go2rtc_ok") else None
    port = entry.data.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT)
    cams = {c["deviceId"]: c for c in bridge.list_cameras()}
    entities = []
    for dev_id in entry.data.get(CONF_CAMERAS, []):
        cam = cams.get(dev_id)
        if cam:
            entities.append(RtspInfoSensor(cam, port, mgr))
    async_add_entities(entities)


class RtspInfoSensor(SensorEntity):
    """Shows the LAN RTSP URLs the app/NVR should pull (HD state, HD+SD attrs)."""

    _attr_has_entity_name = True
    _attr_name = "RTSP URL"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:link-variant"

    def __init__(self, cam, port, mgr):
        device_id = cam["deviceId"]
        path = cam.get("rtspPath", "")
        ip = host_ip()
        if mgr is not None:
            hd = mgr.rtsp_url(ip, device_id, "hd")
            sd = mgr.rtsp_url(ip, device_id, "sd")
            via = "go2rtc"
        else:
            hd = f"rtsp://{ip}:{port}{path}/hd"
            sd = f"rtsp://{ip}:{port}{path}/sd"
            via = "bridge"
        self._attr_unique_id = f"{device_id}_rtsp_url"
        self._attr_native_value = hd
        self._attr_extra_state_attributes = {"hd": hd, "sd": sd, "via": via}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=cam.get("deviceName", "Tuya Doorbell"),
            manufacturer="Tuya",
            model=cam.get("productId"),
        )
