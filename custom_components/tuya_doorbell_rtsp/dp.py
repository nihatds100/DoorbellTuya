"""Local datapoint monitor (button press / motion) via tinytuya.

These doorbell/camera devices do not answer the legacy DP_QUERY (status()),
so we use updatedps() (DP_QUERY_NEW) like the tuya-local integration, then
keep a persistent connection to receive asynchronous DP pushes (button/motion).
"""
from __future__ import annotations

import logging
import threading
import time

import tinytuya

_LOGGER = logging.getLogger(__name__)

# Broad set of datapoints seen on Tuya doorbells/cameras (button, motion,
# notifications, sd, battery...). updatedps() with an explicit list is what
# makes these devices respond.
_POLL_DPS = [
    104, 106, 108, 109, 110, 111, 115, 117, 134, 136, 145, 146, 147, 149,
    150, 151, 152, 154, 159, 160, 165, 168, 169, 170, 185, 186, 188, 212,
    231, 233, 234, 235, 238, 239, 241, 242, 245, 246,
]


class TuyaDPMonitor:
    """Persistent local connection streaming datapoint updates."""

    def __init__(self, hass, device_id, local_key, callback, ip=None, version=3.3):
        self._hass = hass
        self._device_id = device_id
        self._local_key = local_key
        self._callback = callback
        self._ip = ip
        self._version = version
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(
            target=self._run, name=f"tuya_dp_{self._device_id}", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _find_ip(self):
        if self._ip:
            return self._ip
        try:
            devices = tinytuya.deviceScan(False, 12)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("tinytuya scan failed: %s", err)
            return None
        for ip, info in (devices or {}).items():
            if info.get("gwId") == self._device_id or info.get("id") == self._device_id:
                return ip
        return None

    def _emit(self, dps):
        for dp, val in dps.items():
            self._hass.loop.call_soon_threadsafe(self._callback, dp, val)

    def _run(self):
        _LOGGER.info("tuya_doorbell_rtsp: DP monitor thread started for %s (ip=%s)", self._device_id, self._ip)
        while not self._stop.is_set():
            ip = self._find_ip()
            if not ip:
                _LOGGER.debug("No IP for %s yet; retrying", self._device_id)
                time.sleep(30)
                continue
            try:
                dev = tinytuya.Device(
                    self._device_id, ip, self._local_key, version=self._version
                )
                # Persistent FIRST so the socket stays open and async pushes
                # (button/motion) arrive immediately, like tuya-local does.
                dev.set_socketPersistent(True)
                dev.set_socketTimeout(3)
                # Establish + initial values (DP_QUERY_NEW) on the persistent socket.
                initial = dev.updatedps(_POLL_DPS)
                if isinstance(initial, dict) and "dps" in initial:
                    self._emit(initial["dps"])
                _LOGGER.info(
                    "tuya_doorbell_rtsp: DP monitor connected to %s at %s",
                    self._device_id, ip,
                )
                # Gentle: establish once, then listen for asynchronous pushes only
                # (button/motion). No periodic re-polling, infrequent heartbeat,
                # so we do not stress the device while it is also streaming.
                last_hb = time.time()
                errors = 0
                while not self._stop.is_set():
                    try:
                        data = dev.receive()  # blocks; returns instantly on a push
                        errors = 0
                    except Exception:  # noqa: BLE001
                        # Usually just a read timeout with no data: KEEP the socket
                        # open so the next async push arrives immediately. Only a
                        # real disconnect (many in a row) triggers a reconnect.
                        data = None
                        errors += 1
                        if errors > 8:
                            raise
                    if isinstance(data, dict) and "dps" in data:
                        self._emit(data["dps"])
                    if time.time() - last_hb > 3:
                        try:
                            dev.heartbeat(nowait=True)  # keep-alive so device holds the socket
                        except Exception:  # noqa: BLE001
                            raise
                        last_hb = time.time()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("DP monitor error for %s: %r", self._device_id, err)
                time.sleep(15)
