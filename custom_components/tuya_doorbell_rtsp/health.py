"""Bulletproof layer: keep the app-facing RTSP permanently warm and self-heal
GENTLY (churn is what sticks the device, so recovery must never hammer).

One monitor per camera owns a single persistent keep-warm consumer and watches its
real media flow via ffmpeg -progress. If media dies it restarts the consumer softly
(reconnects to go2rtc without touching the source); only after several soft restarts
fail does it force-refresh the go2rtc producer as a last resort. Every recovery is
spaced >=45s apart, so the one hot session stays stable like the official web client.
"""
from __future__ import annotations

import asyncio
import logging
import time

_LOGGER = logging.getLogger(__name__)


class HealthMonitor:
    def __init__(self, hass, mgr, bridge, device_id, rtsp_path):
        self._hass = hass
        self._mgr = mgr
        self._bridge = bridge
        self._dev = device_id
        self._path = rtsp_path
        self._url = mgr.rtsp_url("127.0.0.1", device_id, "hd")
        self._task = None
        self._stop = False
        self._consumer = None
        self._last_progress = 0.0
        # Timing chosen to never churn: a cold/post-hiccup establish can take ~17s,
        # so give generous grace and space recoveries well apart.
        self._grace = 45         # seconds after a (re)start before judging health
        self._stall = 40         # seconds without media flow => unhealthy
        self._check_every = 15
        self._min_gap = 45       # min seconds between recovery actions
        self._fails = 0
        self._last_recover = 0.0

    def start(self):
        self._stop = False
        self._task = asyncio.ensure_future(self._run())

    async def async_stop(self):
        self._stop = True
        if self._task is not None:
            self._task.cancel()
            self._task = None
        await self._stop_consumer()

    async def _start_consumer(self):
        await self._stop_consumer()
        self._last_progress = time.time()
        self._consumer = await asyncio.create_subprocess_exec(
            "ffmpeg", "-rtsp_transport", "tcp", "-i", self._url,
            "-an", "-c", "copy", "-progress", "pipe:1", "-f", "null", "-",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        asyncio.ensure_future(self._drain(self._consumer))

    async def _stop_consumer(self):
        proc = self._consumer
        self._consumer = None
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), 5)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass

    async def _drain(self, proc):
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                if line.startswith(b"frame=") or line.startswith(b"out_time_us="):
                    self._last_progress = time.time()
        except Exception:  # noqa: BLE001
            pass

    def _healthy(self):
        proc = self._consumer
        if proc is None or proc.returncode is not None:
            return False, "consumer exited"
        if time.time() - self._last_progress > self._stall:
            return False, "media stalled"
        return True, ""

    async def _recover(self, reason):
        self._fails += 1
        self._last_recover = time.time()
        if self._fails >= 3:
            # Soft restarts didn't help: the go2rtc producer/source is likely dead.
            # Force a fresh producer once (heavier), then reset the counter.
            _LOGGER.warning("tuya_doorbell_rtsp: health (%s, x%d) -> force-refresh go2rtc producer",
                            reason, self._fails)
            await self._mgr.async_register(self._bridge.port, self._path, self._dev, qualities=("hd",), force=True)
            self._fails = 0
        else:
            _LOGGER.warning("tuya_doorbell_rtsp: health (%s) -> soft restart keep-warm consumer", reason)
        await self._start_consumer()

    async def _run(self):
        await self._start_consumer()
        await asyncio.sleep(self._grace)
        while not self._stop:
            try:
                ok, reason = self._healthy()
                if ok:
                    self._fails = 0
                elif time.time() - self._last_recover >= self._min_gap:
                    await self._recover(reason)
                    await asyncio.sleep(self._grace)  # let it re-establish before next judgement
            except asyncio.CancelledError:
                break
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("tuya_doorbell_rtsp: health loop error: %s", err)
            await asyncio.sleep(self._check_every)
