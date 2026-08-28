"""Route camera streams through go2rtc so the fragile WebRTC->RTSP bridge only
ever has ONE client (go2rtc), which fans out to viewers/NVR/app with keyframe
caching + source reconnect. That is what makes the warm stream fast AND reliable
(no concurrent-client churn, no zombie sessions).

Prefers Home Assistant's bundled go2rtc (present since HA 2024.11). If that is not
reachable, starts the go2rtc binary shipped with this integration on private ports,
so the integration is fully self-contained on any HA.
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
from pathlib import Path
from urllib.parse import quote

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)
_T = aiohttp.ClientTimeout(total=8)
_ARCH_MAP = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64"}

# Home Assistant's built-in go2rtc.
_HA_API = "http://127.0.0.1:1984"
_HA_RTSP = 8554
# Our bundled instance (used only when HA's go2rtc is absent).
_OWN_API = "http://127.0.0.1:11984"
_OWN_RTSP = 18554
_OWN_WEBRTC = 18555


def stream_name(device_id: str, quality: str) -> str:
    """go2rtc stream name for a camera+quality (unique, URL-safe)."""
    return f"tuyadb_{device_id}_{quality}"


def _source(bridge_port: int, rtsp_path: str, quality: str) -> str:
    # go2rtc runs its own lenient ffmpeg to read the bridge's quirky RTSP, then
    # fans out with keyframe caching. copy = no transcode (H264 + PCMU passthrough).
    # Video only: the doorbell audio is not useful and dropping it lightens
    # the stream for viewers / NVR / app.
    return f"ffmpeg:rtsp://127.0.0.1:{bridge_port}{rtsp_path}/{quality}#video=copy#raw=-an"


class Go2rtc:
    """Manages the go2rtc endpoint (HA's, or our bundled instance)."""

    def __init__(self, hass, bin_dir: Path):
        self._hass = hass
        self._bin_dir = Path(bin_dir)
        self._data = Path(hass.config.path("tuya_doorbell_rtsp"))
        self._proc = None
        self._api = None
        self._rtsp_port = None

    @property
    def available(self) -> bool:
        return self._api is not None

    @property
    def rtsp_port(self):
        return self._rtsp_port

    def rtsp_url(self, host: str, device_id: str, quality: str) -> str:
        """The RTSP URL viewers/NVR/app pull from (served by go2rtc)."""
        return f"rtsp://{host}:{self._rtsp_port}/{stream_name(device_id, quality)}"

    async def _ping(self, api: str) -> bool:
        session = async_get_clientsession(self._hass)
        try:
            async with session.get(f"{api}/api", timeout=_T) as resp:
                return resp.status == 200
        except Exception:  # noqa: BLE001
            return False

    async def async_start(self) -> bool:
        """Pick HA's go2rtc if reachable, else start our bundled one. Returns
        True if a go2rtc endpoint is available."""
        if await self._ping(_HA_API):
            self._api, self._rtsp_port = _HA_API, _HA_RTSP
            _LOGGER.info("tuya_doorbell_rtsp: using Home Assistant's built-in go2rtc")
            return True
        return await self._start_bundled()

    async def _start_bundled(self) -> bool:
        arch = _ARCH_MAP.get(platform.machine().lower())
        if arch is None:
            _LOGGER.error("tuya_doorbell_rtsp: no go2rtc binary for arch %s", platform.machine())
            return False
        binp = self._bin_dir / f"go2rtc-linux-{arch}"
        self._data.mkdir(parents=True, exist_ok=True)
        cfg = self._data / "go2rtc.yaml"
        cfg.write_text(
            "log:\n  level: warn\n"
            f"api:\n  listen: \"127.0.0.1:11984\"\n"
            f"rtsp:\n  listen: \":{_OWN_RTSP}\"\n"
            f"webrtc:\n  listen: \":{_OWN_WEBRTC}\"\n"
        )
        try:
            os.chmod(binp, 0o755)
        except OSError:
            pass
        logf = open(self._data / "go2rtc.log", "ab")  # noqa: SIM115
        self._proc = await asyncio.create_subprocess_exec(
            str(binp), "-config", str(cfg),
            cwd=str(self._data),
            stdout=logf, stderr=logf, start_new_session=True,
        )
        for _ in range(20):
            if await self._ping(_OWN_API):
                self._api, self._rtsp_port = _OWN_API, _OWN_RTSP
                _LOGGER.info("tuya_doorbell_rtsp: started bundled go2rtc (rtsp :%d)", _OWN_RTSP)
                return True
            await asyncio.sleep(0.5)
        _LOGGER.error("tuya_doorbell_rtsp: bundled go2rtc did not come up")
        return False

    async def async_register(self, bridge_port, rtsp_path, device_id, qualities=("hd", "sd"), force=False) -> bool:
        """(Re)register hd+sd streams for one camera via the go2rtc API."""
        if not self.available:
            return False
        session = async_get_clientsession(self._hass)
        ok = True
        for quality in qualities:
            name = stream_name(device_id, quality)
            src = _source(bridge_port, rtsp_path, quality)
            if force:
                # Drop any existing (possibly zombie) producer so the re-register
                # forces a fresh source connection.
                try:
                    async with session.delete(f"{self._api}/api/streams?name={quote(name, safe='')}", timeout=_T):
                        pass
                except Exception:  # noqa: BLE001
                    pass
            url = f"{self._api}/api/streams?name={quote(name, safe='')}&src={quote(src, safe='')}"
            try:
                async with session.put(url, timeout=_T) as resp:
                    if resp.status not in (200, 201):
                        _LOGGER.warning("tuya_doorbell_rtsp: go2rtc register %s -> HTTP %s", name, resp.status)
                        ok = False
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("tuya_doorbell_rtsp: go2rtc register %s failed: %s", name, err)
                ok = False
        return ok

    async def async_stop(self):
        """Stop our bundled go2rtc (never touches HA's)."""
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), 5)
            except Exception:  # noqa: BLE001
                self._proc.kill()
        self._proc = None
