# DoorbellTuya

**Local, always-warm RTSP from a Tuya / Smart Life video doorbell — for Home Assistant.**

Many Tuya video doorbells expose no native RTSP/ONVIF, so the only way to watch
them is the cloud app. This custom integration gives you a **local RTSP URL** for
the live stream that your app, NVR, or Home Assistant can pull — with the media
staying on your LAN and coming up in **~0.7 seconds**, reliably.

> Personal / hobby project. **Noncommercial use only.** Not affiliated with,
> endorsed by, or connected to Tuya, Smart Life, or any device manufacturer.

---

## What you get

- `camera.<name>_hd` / `camera.<name>_sd` — live camera entities.
- `sensor.<name>_rtsp_url` — the exact **LAN RTSP URL** to pull from your app / NVR
  (shown as the state; `hd` and `sd` URLs in its attributes).
- `event.<name>_datapoint` — fires on doorbell button press / datapoint changes.

Point your app or NVR at the URL from the sensor, e.g.
`rtsp://<home-assistant-ip>:8554/tuyadb_<deviceId>_hd`.

## How it works

```
Doorbell ──WebRTC──▶ bridge ──▶ go2rtc ──RTSP/WebRTC──▶ your app / NVR / Home Assistant
          (cloud signalling,     (single hot source,
           media stays on LAN)    keyframe cache, fan-out)
```

- A small Go **bridge** (a patched build of seydx's `tuya-ipc-terminal`) speaks the
  Tuya WebRTC protocol and re-publishes the stream as RTSP. Only **signalling** goes
  through the internet; the **media stays local** (relay/TURN is blocked).
- **go2rtc** sits in front so the bridge only ever has one client, and fans out to
  every viewer with a cached keyframe — this is what makes it fast *and* reliable.
- A built-in **health monitor** keeps one hot session alive permanently and gently
  self-heals if the stream ever stalls, so a pull is ready at all times.

Both the bridge and go2rtc binaries (linux amd64/arm64) are bundled — if your Home
Assistant already ships go2rtc (2024.11+), that one is used automatically; otherwise
the bundled one is started on a private port.

## Requirements

- Home Assistant 2025.3 or newer, with `ffmpeg` (standard on HA OS/Supervised).
- Linux host, `amd64` or `arm64` (HA OS, Supervised, or Container).
- A Tuya / Smart Life **account** that the doorbell is paired to, and the doorbell
  on the same LAN as Home Assistant.

## Install (HACS)

1. HACS → **Custom repositories** → add `https://github.com/nihatds100/DoorbellTuya`
   as an **Integration**.
2. Install **Tuya Doorbell RTSP (LAN-only)**, then restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → Tuya Doorbell RTSP**.
4. Enter your Tuya / Smart Life **email, password, phone country code** (e.g. `1`,
   `44`, `49`…) and **server region**, then pick your doorbell.

The datapoint monitor logs in locally with the device's local key, which the
integration reads from your account during setup — no manual key extraction needed.

## A note on credentials & security

You enter your own Tuya / Smart Life account details, and **you are solely
responsible** for entering, storing, and protecting them. They are used only to
talk to Tuya's own servers for stream signalling and are kept in Home Assistant's
config. This repository contains **no credentials of any kind**. Treat your Home
Assistant instance and its config as sensitive.

## Credits & attribution

This project stands entirely on the work of others — thank you:

- **[seydx/tuya-ipc-terminal](https://github.com/seydx/tuya-ipc-terminal)** (MIT) —
  the Tuya WebRTC ⭢ RTSP bridge. `bridge-src/` and the `tuya-ipc-terminal-*`
  binaries are a patched build of it (LAN-only media, password login, keyframe
  requests, clean teardown, stuck-session auto-recovery).
- **[AlexxIT/go2rtc](https://github.com/AlexxIT/go2rtc)** (MIT) — the restreamer
  used for reliable fan-out; shipped unmodified as `bin/go2rtc-linux-*`.
- **[jasonacox/tinytuya](https://github.com/jasonacox/tinytuya)** (MIT) — local
  datapoint (button-press) monitoring.
- **[Home Assistant](https://www.home-assistant.io/)** and its `stream` / `ffmpeg`
  components.

## License

- This project's own code: **PolyForm Noncommercial License 1.0.0** (noncommercial /
  personal use only) — see [`LICENSE`](LICENSE).
- Bundled third-party components keep their original **MIT** licenses (see `LICENSE`
  and `bridge-src/LICENSE`).

## Disclaimer

Provided "as is", without warranty of any kind. Reverse-engineered, unofficial, and
dependent on Tuya's cloud for signalling — it may break if Tuya changes their
protocol. Use at your own risk.
