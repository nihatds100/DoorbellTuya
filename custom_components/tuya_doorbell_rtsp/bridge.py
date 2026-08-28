"""Manage the bundled LAN-only Go RTSP bridge (tuya-ipc-terminal)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import signal
import time
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

_ARCH_MAP = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64"}


class BridgeError(Exception):
    """Raised when the bridge binary fails."""


class TuyaBridge:
    """Wraps the Go binary: auth, camera discovery, and the RTSP server."""

    def __init__(self, hass, region, email, password, country_code, port):
        self._hass = hass
        self._region = region
        self._email = email
        self._password = password
        self._country = country_code
        self._port = port
        self._proc: asyncio.subprocess.Process | None = None
        base = Path(__file__).parent
        arch = _ARCH_MAP.get(platform.machine().lower())
        if arch is None:
            raise BridgeError(f"unsupported architecture: {platform.machine()}")
        self._bin = base / "bin" / f"tuya-ipc-terminal-linux-{arch}"
        # persistent data dir under /config (survives HACS updates)
        self._data = Path(hass.config.path("tuya_doorbell_rtsp"))
        self._data.mkdir(parents=True, exist_ok=True)

    @property
    def port(self):
        return self._port

    def _env(self):
        env = dict(os.environ)
        # Relay (TURN via Tuya) is always blocked by the patched binary; keep srflx
        # candidates for reliable local hole-punching (never touches Tuya relay).
        env["TUYA_PASSWORD"] = self._password
        env["TUYA_COUNTRY_CODE"] = self._country
        return env

    async def _run(self, *args, timeout=60):
        try:
            os.chmod(self._bin, 0o755)
        except OSError:
            pass
        proc = await asyncio.create_subprocess_exec(
            str(self._bin), *args,
            cwd=str(self._data), env=self._env(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(b"y\n"), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise BridgeError("bridge command timed out")
        return proc.returncode, (out or b"").decode("utf-8", "replace")

    async def async_authenticate(self):
        """Log in (password) and refresh camera list. Raises BridgeError on failure."""
        rc, out = await self._run("auth", "add", "--password", self._region, self._email)
        if "Successfully" not in out and "already exists" not in out:
            rc2, out2 = await self._run("auth", "refresh", "--password", self._region, self._email)
            if "Successfully" not in out2:
                raise BridgeError(_scrub(out2 or out))
        await self._run("cameras", "refresh")

    async def async_refresh(self, *_):
        """Periodic session refresh (keeps the QR-less session alive)."""
        rc, out = await self._run("auth", "refresh", "--password", self._region, self._email)
        if "Successfully" not in out:
            _LOGGER.warning("Session refresh failed: %s", _scrub(out))

    def list_cameras(self):
        """Return discovered cameras from the bridge's cache (deviceId, name, rtspPath, skill)."""
        f = self._data / ".tuya-data" / "cameras.json"
        if not f.exists():
            return []
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            return []
        return data.get("cameras", [])

    async def async_camera_detail(self, device_id):
        """Fetch localKey + protocolVersion for a device from the cloud (one-time)."""
        rc, out = await self._run("cameras", "info", device_id, "--json")
        for line in reversed(out.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except ValueError:
                    return None
        return None

    async def async_start(self):
        """Start the RTSP server subprocess."""
        if self._proc and self._proc.returncode is None:
            return
        try:
            os.chmod(self._bin, 0o755)
        except OSError:
            pass
        logf = open(self._data / "bridge.log", "ab", buffering=0)
        self._proc = await asyncio.create_subprocess_exec(
            str(self._bin), "rtsp", "start", "--port", str(self._port),
            cwd=str(self._data), env=self._env(),
            stdout=logf, stderr=logf,
        )
        _LOGGER.info("tuya_doorbell_rtsp: RTSP bridge started on port %s (LAN-only)", self._port)

    async def async_start_keepwarm(self, urls):
        """Hold a persistent local RTSP puller per stream so the WebRTC session
        stays warm (new viewers/NVR see video in ~1s instead of a ~10s cold-start).
        A watchdog restarts a puller whose media stalls or whose process exits, so a
        silently-dead warm session (RTSP up but no frames) self-heals in ~25s."""
        await self.async_stop_keepwarm()
        self._warm = []
        self._warm_urls = []
        self._warm_last = []
        self._warm_stop = False
        for url in urls:
            self._warm_urls.append(url)
            self._warm_last.append(time.time())
            self._warm.append(None)
        for idx in range(len(self._warm_urls)):
            await self._spawn_keepwarm(idx)
        self._warm_watchdog = asyncio.ensure_future(self._keepwarm_watchdog())
        _LOGGER.info("tuya_doorbell_rtsp: keep-warm ON for %d stream(s)", len(self._warm))

    async def _spawn_keepwarm(self, idx):
        """Launch one keep-warm puller; stream its -progress so the watchdog can
        tell live media from a zombie (RTSP session up but no frames flowing)."""
        url = self._warm_urls[idx]
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-rtsp_transport", "tcp",
            "-i", url, "-an", "-c", "copy",
            "-progress", "pipe:1", "-f", "null", "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        self._warm[idx] = proc
        self._warm_last[idx] = time.time()
        asyncio.ensure_future(self._drain_progress(idx, proc))

    async def _drain_progress(self, idx, proc):
        """Bump last-progress time whenever ffmpeg reports advancing media."""
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                if line.startswith(b"frame=") or line.startswith(b"out_time_us="):
                    if idx < len(self._warm_last):
                        self._warm_last[idx] = time.time()
        except Exception:  # noqa: BLE001
            pass

    async def _keepwarm_watchdog(self):
        """Restart a puller whose media has stalled (zombie) or whose ffmpeg died."""
        stall = 25       # seconds without progress => media dead
        establish = 40   # grace for a (re)connect to produce first frames
        await asyncio.sleep(establish)
        while not self._warm_stop:
            for idx in range(len(self._warm)):
                if self._warm_stop:
                    break
                proc = self._warm[idx]
                dead = proc is None or proc.returncode is not None
                stale = (time.time() - self._warm_last[idx]) > stall
                if dead or stale:
                    _LOGGER.warning(
                        "tuya_doorbell_rtsp: keep-warm stream %d %s; restarting",
                        idx, "exited" if dead else "media stalled",
                    )
                    if proc is not None:
                        await self._kill_keepwarm_proc(proc)
                    if self._warm_stop:
                        break
                    await self._spawn_keepwarm(idx)
                    await asyncio.sleep(establish)
            await asyncio.sleep(8)

    async def _kill_keepwarm_proc(self, proc):
        if proc.returncode is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except Exception:  # noqa: BLE001
            pass

    async def async_stop_keepwarm(self):
        self._warm_stop = True
        wd = getattr(self, "_warm_watchdog", None)
        if wd is not None:
            wd.cancel()
            self._warm_watchdog = None
        for proc in getattr(self, "_warm", []):
            if proc is not None and proc.returncode is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass
        self._warm = []
        self._warm_urls = []
        self._warm_last = []

    async def async_stop(self):
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), 10)
            except asyncio.TimeoutError:
                self._proc.kill()
        self._proc = None


def _scrub(text: str) -> str:
    """Remove anything sensitive from a message before logging."""
    return (text or "").replace("\n", " ").strip()[:200]
