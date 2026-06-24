# ha-ezviz

A **drop-in replacement** for the official [Home Assistant EZVIZ integration](https://www.home-assistant.io/integrations/ezviz/), distributed through [HACS](https://hacs.xyz/) as a custom integration.

It keeps the same domain (`ezviz`), so once installed it **overrides** the built-in integration and reuses your existing config entries, entities, and history — no reconfiguration needed. The goal is to ship bug fixes and features faster than the Home Assistant release cycle, while staying API-compatible with the official version.

> ⚠️ This is a community fork, not affiliated with EZVIZ or the Home Assistant project. When Home Assistant loads it, you'll see the normal "you are using a custom integration" warning in the log — that's expected.

---

## Why this fork?

The official integration had several issues this fork addresses, plus a couple of new capabilities:

### Bug fixes

| Area | Problem | Fix |
| --- | --- | --- |
| **Siren** | Turning the siren **on** never reached the API (missing `await`), spamming `Future exception was never retrieved` in the log. | The call is now awaited, so the siren actually fires and errors surface cleanly. |
| **Config flow** | A Python-2-style `except A, B:` was a real `SyntaxError`. The integration still loaded, but **every discovery / reauth / camera-setup flow crashed**, silently blocking credential changes (a common cause of RTSP `401` errors). | Corrected to `except (A, B):`. |
| **Motion images** | Encrypted alarm images failed to decrypt (`encrypted with other password`). EZVIZ encrypts them with the device **verification code**, not the RTSP password. | A dedicated, optional verification-code field was added (see below); decryption falls back to the RTSP password for backwards compatibility. |
| **Last-alarm-picture sensor** | The signed image URL exceeds Home Assistant's 255-character state limit, so the sensor showed `unknown`. | The state is capped and the full URL is exposed via the `full_value` attribute. |
| **Streams** | The RTSP port was cached at setup and never refreshed. | The port is now re-read from the cloud API on every `stream_source`. |

### New features

- **Dual-lens / multi-channel cameras** — devices that report more than one video channel (e.g. dual-lens cameras) now expose **one camera entity per channel** under the same device. The primary lens keeps its original entity (and entity ID); additional lenses appear as `Channel 2`, `Channel 3`, … Channel RTSP paths are derived from your configured stream path (`/Streaming/Channels/102` → `/Streaming/Channels/202`), preserving your main/sub-stream choice.
- **Reconfigure flow** — you can update a camera's RTSP username/password and add or change its verification code from the entry's **⋮ → Reconfigure** menu, without deleting and re-adding the camera.
- **Up-to-date library** — bumped [`pyezvizapi`](https://github.com/RenierM26/pyEzvizApi) to `1.0.5.0`, which brings improved multi-channel / DVR support.

---

## Installation (HACS)

1. In Home Assistant, go to **HACS → ⋮ (top right) → Custom repositories**.
2. Add the repository URL: `https://github.com/NaturalDevCR/ha-ezviz`
   Category: **Integration**.
3. Search for **EZVIZ (ha-ezviz)** in HACS, install it.
4. **Restart Home Assistant.**

Because the domain is `ezviz`, your existing EZVIZ setup is picked up automatically after the restart — the custom integration takes precedence over the built-in one.

> To go back to the official integration, remove this repository from HACS, delete `custom_components/ezviz/`, and restart.

### Manual installation

Copy the `custom_components/ezviz` folder into your Home Assistant `config/custom_components/` directory and restart.

---

## Configuration notes

### Verification code (encrypted motion images)

If your camera's **last motion image** fails to decrypt, set its **verification code** (the code printed on the device label / sticker):

- **New cameras:** the field appears in the camera setup dialog (optional).
- **Existing cameras:** open the camera entry → **⋮ → Reconfigure** and fill in *Verification code*.

Leave it empty if your verification code is the same as the RTSP password — decryption falls back to the password automatically.

### Dual-lens cameras

No extra configuration is required. As long as the device reports multiple channels and your RTSP credentials are set, the additional lens entities are created automatically. The second lens uses the same IP, port, and credentials as the first — only the RTSP channel differs.

---

## Compatibility

- Requires a recent Home Assistant (`2025.1.0`+) running on Python 3.12+.
- Depends on `pyezvizapi==1.0.5.0` (installed automatically).

## Credits

- Maintained by [@NaturalDevCR](https://github.com/NaturalDevCR).
- Based on the official [Home Assistant EZVIZ integration](https://www.home-assistant.io/integrations/ezviz/) (Apache-2.0), originally authored and maintained by [@RenierM26](https://github.com/RenierM26).
- Powered by the [`pyezvizapi`](https://github.com/RenierM26/pyEzvizApi) library, also by [@RenierM26](https://github.com/RenierM26).

## License

Apache-2.0 — see [LICENSE](LICENSE).
