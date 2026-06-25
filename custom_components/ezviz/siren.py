"""Support for EZVIZ sirens."""

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, override

from pyezvizapi import HTTPError, PyEzvizError, SupportExt
from pyezvizapi.api_endpoints import (
    API_ENDPOINT_DEVICES,
    API_ENDPOINT_SWITCH_SOUND_ALARM,
)

from homeassistant.components.siren import (
    SirenEntity,
    SirenEntityDescription,
    SirenEntityFeature,
)
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import event as evt
from homeassistant.helpers.entity_platform import (
    AddConfigEntryEntitiesCallback,
    async_get_current_platform,
)
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import EzvizConfigEntry, EzvizDataUpdateCoordinator
from .entity import EzvizBaseEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1
OFF_DELAY = timedelta(seconds=60)  # Camera firmware has hard coded turn off.

# Channels to try for the legacy sendAlarm command (older cameras). Newer
# cameras (e.g. the H8c dual-lens) reject sendAlarm on every channel with code
# 2004 and instead use the "whistle" API; the siren falls back to that.
_ALARM_CHANNELS = (0, 1, 2)
WHISTLE_DURATION = 60  # seconds; matches the firmware hard-coded auto-off
WHISTLE_VOLUME = 100  # 0-100

SIREN_ENTITY_TYPE = SirenEntityDescription(
    key="siren",
    translation_key="siren",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up EZVIZ sensors based on a config entry."""
    coordinator = entry.runtime_data

    platform = async_get_current_platform()
    # Temporary diagnostic service to identify the correct siren mechanism on
    # cameras where it doesn't sound. Call ezviz.siren_diagnostics on the siren
    # entity, listen, and share the resulting log.
    platform.async_register_entity_service(
        "siren_diagnostics", None, "async_siren_diagnostics"
    )

    async_add_entities(
        EzvizSirenEntity(coordinator, camera, SIREN_ENTITY_TYPE)
        for camera in coordinator.data
        for capability, value in coordinator.data[camera]["supportExt"].items()
        if capability == str(SupportExt.SupportActiveDefense.value)
        if value != "0"
    )


class EzvizSirenEntity(EzvizBaseEntity, SirenEntity, RestoreEntity):
    """Representation of a EZVIZ Siren entity."""

    _attr_supported_features = SirenEntityFeature.TURN_ON | SirenEntityFeature.TURN_OFF
    _attr_should_poll = False
    _attr_assumed_state = True

    def __init__(
        self,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
        description: SirenEntityDescription,
    ) -> None:
        """Initialize the Siren."""
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{serial}_{description.key}"
        self.entity_description = description
        self._attr_is_on = False
        self._delay_listener: Callable | None = None
        # How this device's siren is driven; discovered on first use.
        # ("sendalarm", channel) for the legacy API, ("whistle", None) for the
        # modern whistle API.
        self._alarm_method: tuple[str, int | None] | None = None

    def _send_alarm(self, channel: int, enable: int) -> dict:
        """Call the legacy sendAlarm endpoint on a channel; return payload."""
        client = self.coordinator.ezviz_client
        return client._request_json(  # noqa: SLF001
            "PUT",
            f"{API_ENDPOINT_DEVICES}{self._serial}/{channel}"
            f"{API_ENDPOINT_SWITCH_SOUND_ALARM}",
            data={"enable": enable},
            retry_401=True,
        )

    def _alarm_on(self) -> None:
        """Sound the siren, discovering the mechanism the device supports.

        Runs in the executor. Tries the legacy active-defense sendAlarm across
        channels (older cameras) and falls back to the modern whistle API
        (newer cameras such as the dual-lens H8c). Caches what worked.
        """
        client = self.coordinator.ezviz_client

        # Fast path: reuse the previously-discovered mechanism.
        if self._alarm_method == ("whistle", None):
            client.set_device_whistle(
                self._serial,
                status=1,
                duration=WHISTLE_DURATION,
                volume=WHISTLE_VOLUME,
            )
            return

        errors: list[str] = []

        # 1) Legacy sendAlarm (older cameras), trying each channel.
        if hasattr(client, "_request_json") and hasattr(client, "_is_ok"):
            for channel in _ALARM_CHANNELS:
                try:
                    payload = self._send_alarm(channel, 2)
                except (HTTPError, PyEzvizError) as err:
                    errors.append(f"sendAlarm ch{channel}: {err}")
                    continue
                if client._is_ok(payload):  # noqa: SLF001
                    self._alarm_method = ("sendalarm", channel)
                    return
                errors.append(f"sendAlarm ch{channel}: {payload.get('meta')}")

        # 2) Modern whistle API (e.g. H8c dual-lens).
        try:
            client.set_device_whistle(
                self._serial,
                status=1,
                duration=WHISTLE_DURATION,
                volume=WHISTLE_VOLUME,
            )
        except (HTTPError, PyEzvizError) as err:
            errors.append(f"whistle: {err}")
        else:
            self._alarm_method = ("whistle", None)
            return

        raise PyEzvizError("Could not sound the siren: " + "; ".join(errors))

    def _alarm_off(self) -> None:
        """Stop the siren using the discovered mechanism. Runs in executor."""
        client = self.coordinator.ezviz_client
        method = self._alarm_method

        if method is not None and method[0] == "whistle":
            client.stop_whistle(self._serial)
            return

        if method is not None and method[0] == "sendalarm":
            payload = self._send_alarm(method[1] or 0, 1)
            if not client._is_ok(payload):  # noqa: SLF001
                raise PyEzvizError(f"Could not stop the siren: {payload.get('meta')}")
            return

        # Mechanism unknown (e.g. off pressed before on): best effort.
        try:
            client.stop_whistle(self._serial)
        except (HTTPError, PyEzvizError):
            client.sound_alarm(self._serial, 1)

    def _run_diagnostics(self) -> None:
        """Probe siren mechanisms and log results (temporary diagnostic).

        Reads the device's whistle status (channel structure) and then makes a
        short 5 s sound test for the device-level whistle and each channel,
        spaced 8 s apart so they can be told apart by ear.
        """
        client = self.coordinator.ezviz_client
        serial = self._serial
        lines = [f"=== EZVIZ siren diagnostics for {serial} ==="]

        def safe(label: str, func: Callable[[], Any]) -> None:
            try:
                lines.append(f"{label} -> {func()}")
            except Exception as err:  # noqa: BLE001
                lines.append(f"{label} -> EXC {type(err).__name__}: {err}")

        # Read-only: reveal channel structure and current whistle state.
        safe(
            "get_whistle_status_by_device",
            lambda: client.get_whistle_status_by_device(serial),
        )
        safe(
            "get_whistle_status_by_channel",
            lambda: client.get_whistle_status_by_channel(serial),
        )

        # Sound tests (5 s each, library-blessed volume=50). LISTEN for which
        # one actually sounds.
        safe(
            "set_device_whistle(status=1,dur=5,vol=50)",
            lambda: client.set_device_whistle(
                serial, status=1, duration=5, volume=50
            ),
        )
        time.sleep(8)
        for channel in (1, 2):
            safe(
                f"set_channel_whistle(channel={channel},status=1,dur=5,vol=50)",
                lambda ch=channel: client.set_channel_whistle(
                    serial,
                    [{"channel": ch, "status": 1, "duration": 5, "volume": 50}],
                ),
            )
            time.sleep(8)

        _LOGGER.warning("%s", "\n".join(lines))

    async def async_siren_diagnostics(self) -> None:
        """Run the temporary siren diagnostics (entity service)."""
        await self.hass.async_add_executor_job(self._run_diagnostics)

    @override
    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        if not (last_state := await self.async_get_last_state()):
            return
        self._attr_is_on = last_state.state == STATE_ON

        if self._attr_is_on:
            evt.async_call_later(self.hass, OFF_DELAY, self.off_delay_listener)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off camera siren."""
        try:
            await self.hass.async_add_executor_job(self._alarm_off)

        except (HTTPError, PyEzvizError) as err:
            raise HomeAssistantError(
                f"Failed to turn siren off for camera {self._serial}: {err}"
            ) from err

        if self._delay_listener is not None:
            self._delay_listener()
            self._delay_listener = None

        self._attr_is_on = False
        self.async_write_ha_state()

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on camera siren."""
        try:
            await self.hass.async_add_executor_job(self._alarm_on)

        except (HTTPError, PyEzvizError) as err:
            raise HomeAssistantError(
                f"Failed to turn siren on for camera {self._serial}: {err}"
            ) from err

        if self._delay_listener is not None:
            self._delay_listener()
            self._delay_listener = None

        self._attr_is_on = True
        self._delay_listener = evt.async_call_later(
            self.hass, OFF_DELAY, self.off_delay_listener
        )
        self.async_write_ha_state()

    @callback
    def off_delay_listener(self, now: datetime) -> None:
        """Switch device off after a delay.

        Camera firmware has hard coded turn off after 60 seconds.
        """
        self._attr_is_on = False
        self._delay_listener = None
        self.async_write_ha_state()
