"""Diagnostics support for EZVIZ."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import CONF_ENC_KEY, CONF_RFSESSION_ID, CONF_SESSION_ID, DOMAIN
from .coordinator import EzvizConfigEntry

TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_ENC_KEY,
    CONF_SESSION_ID,
    CONF_RFSESSION_ID,
    "mac_address",
    "local_ip",
    "wan_ip",
    "last_alarm_pic",
    "encrypted_pwd_hash",
    "picUrl",
    "deviceSerial",
}


def _entry_info(entry: EzvizConfigEntry) -> dict[str, Any]:
    """Return the redacted config entry data and options."""
    return {
        "data": async_redact_data(entry.data, TO_REDACT),
        "options": dict(entry.options),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EzvizConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    diagnostics: dict[str, Any] = {"entry": _entry_info(entry)}

    # Only the cloud account entry carries the coordinator with device data.
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is not None and getattr(coordinator, "data", None) is not None:
        diagnostics["devices"] = async_redact_data(coordinator.data, TO_REDACT)

    return diagnostics


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: EzvizConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for a single device."""
    diagnostics: dict[str, Any] = {"entry": _entry_info(entry)}

    coordinator = getattr(entry, "runtime_data", None)
    serial = next(
        (identifier for domain, identifier in device.identifiers if domain == DOMAIN),
        None,
    )
    if (
        coordinator is not None
        and getattr(coordinator, "data", None) is not None
        and serial in coordinator.data
    ):
        diagnostics["device"] = async_redact_data(coordinator.data[serial], TO_REDACT)

    return diagnostics
