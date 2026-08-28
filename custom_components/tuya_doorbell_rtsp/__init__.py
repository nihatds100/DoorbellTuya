"""Tuya Doorbell RTSP (LAN-only) integration."""
from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .bridge import BridgeError, TuyaBridge
from .const import (
    CONF_CAMERAS, CONF_DEVICE_IP, CONF_LOCAL_KEYS, CONF_COUNTRY_CODE, CONF_EMAIL, CONF_PASSWORD, CONF_REGION,
    CONF_RTSP_PORT, DEFAULT_RTSP_PORT, DOMAIN, EVENT_DP, LOCAL_PROTOCOL_VERSION,
)
from . import go2rtc as g2
from . import health as hm
from .dp import TuyaDPMonitor
from .event import signal

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["camera", "event", "sensor"]


async def async_setup_entry(hass, entry):
    d = entry.data
    bridge = TuyaBridge(
        hass, d[CONF_REGION], d[CONF_EMAIL], d[CONF_PASSWORD],
        d[CONF_COUNTRY_CODE], d.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT),
    )
    try:
        await bridge.async_refresh()
    except BridgeError as err:
        _LOGGER.warning("Initial session refresh failed: %s", err)

    # Local DP monitors use localKeys captured at config time (stored in the entry).
    details = dict(d.get(CONF_LOCAL_KEYS, {}))
    monitors = []
    for dev_id, detail in details.items():
        local_key = (detail or {}).get("localKey")
        if local_key:
            monitors.append(_make_monitor(hass, entry, dev_id, local_key))

    await bridge.async_start()

    # Route every stream through HA's bundled go2rtc so the fragile bridge only
    # ever has ONE client (go2rtc), which fans out to viewers/NVR/app reliably.
    g2mgr = g2.Go2rtc(hass, Path(__file__).parent / "bin")
    go2rtc_ok = False
    health_monitors = []
    if await g2mgr.async_start():
        cams = {c["deviceId"]: c for c in bridge.list_cameras()}
        go2rtc_ok = True
        for dev_id in d.get(CONF_CAMERAS, []):
            cam = cams.get(dev_id)
            if cam and not await g2mgr.async_register(bridge.port, cam["rtspPath"], dev_id):
                go2rtc_ok = False
        _LOGGER.info("tuya_doorbell_rtsp: go2rtc routing %s", "enabled" if go2rtc_ok else "partial")
        # Bulletproof: a permanent, self-healing health monitor per camera keeps one
        # hot session and repairs any zombie/stuck source before a viewer notices.
        for dev_id in d.get(CONF_CAMERAS, []):
            cam = cams.get(dev_id)
            if cam:
                mon = hm.HealthMonitor(hass, g2mgr, bridge, dev_id, cam["rtspPath"])
                mon.start()
                health_monitors.append(mon)
        _LOGGER.info("tuya_doorbell_rtsp: %d health monitor(s) started (always-warm + self-heal)", len(health_monitors))
    else:
        _LOGGER.warning("tuya_doorbell_rtsp: go2rtc unavailable; using direct bridge URLs (less warm-reliable)")

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "bridge": bridge, "details": details, "monitors": monitors,
        "go2rtc": g2mgr, "go2rtc_ok": go2rtc_ok, "health": health_monitors,
    }
    _LOGGER.warning("tuya_doorbell_rtsp: %d DP monitor(s) from %d detail(s)", len(monitors), len(details))
    for mon in monitors:
        mon.start()

    entry.async_on_unload(
        async_track_time_interval(hass, bridge.async_refresh, timedelta(hours=12))
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _make_monitor(hass, entry, device_id, local_key):
    def _cb(dp, value):
        hass.bus.async_fire(EVENT_DP, {"device_id": device_id, "dp": str(dp), "value": value})
        async_dispatcher_send(hass, signal(entry.entry_id, device_id), dp, value)

    ip = entry.data.get(CONF_DEVICE_IP) or None
    return TuyaDPMonitor(hass, device_id, local_key, _cb, ip=ip, version=LOCAL_PROTOCOL_VERSION)


async def async_unload_entry(hass, entry):
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    store = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if store:
        for mon in store.get("monitors", []):
            mon.stop()
        for mon in store.get("health", []):
            await mon.async_stop()
        bridge = store.get("bridge")
        if bridge:
            await bridge.async_stop_keepwarm()
            await bridge.async_stop()
        mgr = store.get("go2rtc")
        if mgr:
            await mgr.async_stop()
    return ok
