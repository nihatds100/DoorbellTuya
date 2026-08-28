"""Bulletproof layer: keep the app-facing RTSP permanently warm and self-heal
gently. Warms the lighter SD stream (HD stays on-demand).

Robust + simple: one persistent plain keep-warm consumer (no -progress, which could
misfire); restart it if the process exits; a periodic real one-frame probe verifies
the stream actually delivers video, and only a genuine probe failure (twice) forces a
fresh go2rtc producer. Recovery is spaced so it never churns the device.
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
        # Keep the lighter SD stream warm (instant); HD stays on-demand.
        self._quality = "sd"
        self._url = mgr.rtsp_url("127.0.0.1", device_id, self._quality)
        self._task = None
        self._stop = False
        self._consumer = None
        self._grace = 40          # settle time after a (re)start before judging
        self._check_every = 10
        self._probe_every = 45    # seconds between real one-frame verifications
        self._probe_timeout = 18
        self._last_probe = 0.0
        self._probe_fails = 0
        self._last_recover = 0.0
        self._min_gap = 45        # min seconds between producer refreshes

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
        self._consumer = await asyncio.create_subprocess_exec(
            "ffmpeg", "-rtsp_transport", "tcp", "-i", self._url,
            "-an", "-c", "copy", "-f", "null", "-",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )

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

    async def _probe(self) -> bool:
        """Pull one frame from the real URL. True only if video actually arrives."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-rtsp_transport", "tcp", "-i", self._url,
                "-frames:v", "1", "-f", "null", "-",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(proc.wait(), self._probe_timeout)
            except asyncio.TimeoutError:
                proc.kill()
                return False
            return proc.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    async def _run(self):
        await self._start_consumer()
        await asyncio.sleep(self._grace)
        while not self._stop:
            try:
                # 1) keep the consumer process alive (cheap, non-destructive)
                if self._consumer is None or self._consumer.returncode is not None:
                    _LOGGER.debug("tuya_doorbell_rtsp: keep-warm consumer exited; restarting")
                    await self._start_consumer()
                    await asyncio.sleep(self._grace)
                    continue
                # 2) periodically verify real media; only a genuine failure refreshes
                if time.time() - self._last_probe >= self._probe_every:
                    self._last_probe = time.time()
                    if await self._probe():
                        self._probe_fails = 0
                    else:
                        self._probe_fails += 1
                        if self._probe_fails >= 2 and time.time() - self._last_recover >= self._min_gap:
                            _LOGGER.warning("tuya_doorbell_rtsp: keep-warm probe failed -> refresh go2rtc producer")
                            self._last_recover = time.time()
                            self._probe_fails = 0
                            await self._mgr.async_register(
                                self._bridge.port, self._path, self._dev, qualities=(self._quality,), force=True)
                            await self._start_consumer()
                            await asyncio.sleep(self._grace)
            except asyncio.CancelledError:
                break
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("tuya_doorbell_rtsp: health loop error: %s", err)
            await asyncio.sleep(self._check_every)
