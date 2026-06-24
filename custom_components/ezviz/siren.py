"""Support for EZVIZ sirens."""

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
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import EzvizConfigEntry, EzvizDataUpdateCoordinator
from .entity import EzvizBaseEntity

PARALLEL_UPDATES = 1
OFF_DELAY = timedelta(seconds=60)  # Camera firmware has hard coded turn off.

# Channels to try for the sendAlarm command. Single-lens cameras use the
# device-level channel 0; dual-lens / multi-channel devices (e.g. the H8c)
# reject channel 0 with code 2004 and need their 1-indexed channel instead.
_ALARM_CHANNELS = (0, 1, 2)

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
        # Channel the device accepts for sendAlarm; discovered on first use.
        self._alarm_channel: int | None = None

    def _sound_alarm(self, enable: int) -> int:
        """Send the sendAlarm command, trying channels until one is accepted.

        Returns the channel that worked. Raises if none are accepted. Runs in
        the executor.
        """
        client = self.coordinator.ezviz_client

        # Fall back to the library's public method if its internals change.
        if not (hasattr(client, "_request_json") and hasattr(client, "_is_ok")):
            client.sound_alarm(self._serial, enable)
            return 0

        # Try the previously-working channel first, then the rest.
        channels: tuple[int, ...] = _ALARM_CHANNELS
        if self._alarm_channel is not None:
            channels = (
                self._alarm_channel,
                *(c for c in _ALARM_CHANNELS if c != self._alarm_channel),
            )

        last: object = None
        for channel in channels:
            try:
                payload = client._request_json(  # noqa: SLF001
                    "PUT",
                    f"{API_ENDPOINT_DEVICES}{self._serial}/{channel}"
                    f"{API_ENDPOINT_SWITCH_SOUND_ALARM}",
                    data={"enable": enable},
                    retry_401=True,
                )
            except (HTTPError, PyEzvizError) as err:
                last = err
                continue
            if client._is_ok(payload):  # noqa: SLF001
                return channel
            last = payload

        raise PyEzvizError(
            f"Could not set the alarm sound on any channel (tried {channels}): {last}"
        )

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
            self._alarm_channel = await self.hass.async_add_executor_job(
                self._sound_alarm, 1
            )

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
            self._alarm_channel = await self.hass.async_add_executor_job(
                self._sound_alarm, 2
            )

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
