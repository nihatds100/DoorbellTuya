"""HD + SD camera entities, served via go2rtc (falls back to the bridge)."""
from __future__ import annotations

import json

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.helpers.device_registry import DeviceInfo

from ._net import host_ip
from .const import CONF_CAMERAS, DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    store = hass.data[DOMAIN][entry.entry_id]
    bridge = store["bridge"]
    mgr = store["go2rtc"] if store.get("go2rtc_ok") else None
    selected = entry.data.get(CONF_CAMERAS, [])
    cams = {c["deviceId"]: c for c in bridge.list_cameras()}
    entities = []
    for dev_id in selected:
        cam = cams.get(dev_id)
        if not cam:
            continue
        for quality in ("hd", "sd"):
            entities.append(TuyaDoorbellCamera(cam, quality, bridge.port, mgr))
    async_add_entities(entities)


class TuyaDoorbellCamera(Camera):
    """A single quality (HD or SD) stream of a Tuya camera."""

    _attr_has_entity_name = True
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, cam, quality, port, mgr):
        super().__init__()
        self._cam = cam
        self._quality = quality
        self._port = port
        self._attr_unique_id = f"{cam['deviceId']}_{quality}"
        self._attr_name = quality.upper()
        w = h = None
        try:
            sk = json.loads(cam.get("skill", "{}"))
            st = 2 if quality == "hd" else 4
            for v in sk.get("videos", []):
                if v.get("streamType") == st:
                    w, h = v.get("width"), v.get("height")
        except (ValueError, TypeError):
            pass
        dev_id = cam["deviceId"]
        if mgr is not None:
            lan = mgr.rtsp_url(host_ip(), dev_id, quality)
            self._src = mgr.rtsp_url("127.0.0.1", dev_id, quality)
        else:
            lan = f"rtsp://{host_ip()}:{port}{cam['rtspPath']}/{quality}"
            self._src = f"rtsp://127.0.0.1:{port}{cam['rtspPath']}/{quality}"
        self._attr_extra_state_attributes = {
            "resolution": f"{w}x{h}" if w else "unknown",
            "rtsp_url": lan,
        }
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, dev_id)},
            name=cam.get("deviceName", "Tuya Doorbell"),
            manufacturer="Tuya",
            model=cam.get("productId"),
        )

    async def stream_source(self):
        return self._src
