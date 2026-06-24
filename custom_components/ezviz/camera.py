"""Support ezviz camera devices."""

import logging
import re
from typing import override

from pyezvizapi.exceptions import HTTPError, InvalidHost, PyEzvizError

from homeassistant.components import ffmpeg
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.components.stream import CONF_USE_WALLCLOCK_AS_TIMESTAMPS
from homeassistant.config_entries import SOURCE_IGNORE, SOURCE_INTEGRATION_DISCOVERY
from homeassistant.const import CONF_IP_ADDRESS, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import discovery_flow
from homeassistant.helpers.entity_platform import (
    AddConfigEntryEntitiesCallback,
    async_get_current_platform,
)

from .const import (
    ATTR_SERIAL,
    CONF_FFMPEG_ARGUMENTS,
    DEFAULT_CAMERA_USERNAME,
    DEFAULT_FFMPEG_ARGUMENTS,
    DOMAIN,
    SERVICE_WAKE_DEVICE,
)
from .coordinator import EzvizConfigEntry, EzvizDataUpdateCoordinator
from .entity import EzvizEntity

_LOGGER = logging.getLogger(__name__)

_RTSP_CHANNEL_RE = re.compile(r"(/Streaming/Channels/)(\d+)")


def _camera_channel_count(value: dict) -> int:
    """Return the number of video channels a device exposes (at least 1)."""
    try:
        channels = int(value.get("supported_channels") or 1)
    except (TypeError, ValueError):
        return 1
    return max(channels, 1)


def _derive_channel_args(base_args: str, channel: int) -> str:
    """Derive the RTSP path for a channel from the primary channel's args.

    EZVIZ/Hikvision paths encode the channel and stream type as
    ``/Streaming/Channels/<N>`` where ``N = channel * 100 + stream_type``
    (``01`` mainstream, ``02`` substream). The configured args belong to
    channel 1; this swaps in the requested channel while preserving the
    chosen stream type and any other arguments. Returns ``base_args``
    unchanged when it doesn't match the expected pattern.
    """
    match = _RTSP_CHANNEL_RE.search(base_args or "")
    if not match:
        return base_args
    stream_type = int(match.group(2)) % 100
    new_path = f"{match.group(1)}{channel * 100 + stream_type}"
    return base_args[: match.start()] + new_path + base_args[match.end() :]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EzvizConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up EZVIZ cameras based on a config entry."""

    coordinator = entry.runtime_data

    camera_entities: list[EzvizCamera] = []

    for camera, value in coordinator.data.items():
        camera_rtsp_entry = [
            item
            for item in hass.config_entries.async_entries(DOMAIN)
            if item.unique_id == camera and item.source != SOURCE_IGNORE
        ]

        if camera_rtsp_entry:
            ffmpeg_arguments = camera_rtsp_entry[0].options[CONF_FFMPEG_ARGUMENTS]
            camera_username = camera_rtsp_entry[0].data[CONF_USERNAME]
            camera_password = camera_rtsp_entry[0].data[CONF_PASSWORD]
            # Dual-lens / multi-channel devices expose more than one video
            # channel. Each gets its own camera entity under the same device.
            num_channels = _camera_channel_count(value)

        else:
            discovery_flow.async_create_flow(
                hass,
                DOMAIN,
                context={"source": SOURCE_INTEGRATION_DISCOVERY},
                data={
                    ATTR_SERIAL: camera,
                    CONF_IP_ADDRESS: value["local_ip"],
                },
            )

            _LOGGER.warning(
                (
                    "Found camera with serial %s without configuration. Please go to"
                    " integration to complete setup"
                ),
                camera,
            )

            ffmpeg_arguments = DEFAULT_FFMPEG_ARGUMENTS
            camera_username = DEFAULT_CAMERA_USERNAME
            camera_password = None
            # Without credentials there is no stream, so only expose the
            # primary channel until the camera is configured.
            num_channels = 1

        for channel in range(1, num_channels + 1):
            if channel == 1:
                channel_arguments = ffmpeg_arguments
                unique_id = camera
            else:
                channel_arguments = _derive_channel_args(ffmpeg_arguments, channel)
                if channel_arguments == ffmpeg_arguments:
                    # Path doesn't match the expected pattern; we cannot build
                    # a distinct stream for additional channels.
                    break
                unique_id = f"{camera}_channel_{channel}"

            if camera_password:
                channel_rtsp_stream = (
                    f"rtsp://{camera_username}:{camera_password}@"
                    f"{value['local_ip']}:{value['local_rtsp_port']}{channel_arguments}"
                )
            else:
                channel_rtsp_stream = ""

            _LOGGER.debug(
                "Configuring camera %s channel %s with ip: %s rtsp port: %s"
                " ffmpeg arguments: %s",
                camera,
                channel,
                value["local_ip"],
                value["local_rtsp_port"],
                channel_arguments,
            )

            camera_entities.append(
                EzvizCamera(
                    hass,
                    coordinator,
                    camera,
                    camera_username,
                    camera_password,
                    channel_rtsp_stream,
                    value["local_rtsp_port"],
                    channel_arguments,
                    unique_id,
                    channel,
                )
            )

    async_add_entities(camera_entities)

    platform = async_get_current_platform()

    platform.async_register_entity_service(
        SERVICE_WAKE_DEVICE, None, "perform_wake_device"
    )


class EzvizCamera(EzvizEntity, Camera):
    """An implementation of a EZVIZ security camera."""

    _attr_name = None

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: EzvizDataUpdateCoordinator,
        serial: str,
        camera_username: str,
        camera_password: str | None,
        camera_rtsp_stream: str | None,
        local_rtsp_port: int,
        ffmpeg_arguments: str | None,
        unique_id: str | None = None,
        channel: int = 1,
    ) -> None:
        """Initialize a EZVIZ security camera."""
        super().__init__(coordinator, serial)
        Camera.__init__(self)
        self.stream_options[CONF_USE_WALLCLOCK_AS_TIMESTAMPS] = True
        self._username = camera_username
        self._password = camera_password
        self._rtsp_stream = camera_rtsp_stream
        self._local_rtsp_port = local_rtsp_port
        self._ffmpeg_arguments = ffmpeg_arguments
        self._ffmpeg = get_ffmpeg_manager(hass)
        self._channel = channel
        self._attr_unique_id = unique_id or serial
        # The primary channel keeps the device name; additional channels
        # (e.g. the second lens of a dual-lens camera) get a "Channel N" suffix.
        if channel > 1:
            self._attr_name = f"Channel {channel}"
        if camera_password:
            self._attr_supported_features = CameraEntityFeature.STREAM

    @property
    @override
    def is_on(self) -> bool:
        """Return true if on."""
        return bool(self.data["status"])

    @property
    @override
    def is_recording(self) -> bool:
        """Return true if the device is recording."""
        return self.data["alarm_notify"]

    @property
    @override
    def motion_detection_enabled(self) -> bool:
        """Camera Motion Detection Status."""
        return self.data["alarm_notify"]

    @override
    def enable_motion_detection(self) -> None:
        """Enable motion detection in camera."""
        try:
            self.coordinator.ezviz_client.set_camera_defence(self._serial, 1)

        except InvalidHost as err:
            raise InvalidHost("Error enabling motion detection") from err

    @override
    def disable_motion_detection(self) -> None:
        """Disable motion detection."""
        try:
            self.coordinator.ezviz_client.set_camera_defence(self._serial, 0)

        except InvalidHost as err:
            raise InvalidHost("Error disabling motion detection") from err

    @override
    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a frame from the camera stream."""
        if self._rtsp_stream is None:
            return None
        return await ffmpeg.async_get_image(
            self.hass, self._rtsp_stream, width=width, height=height
        )

    @override
    async def stream_source(self) -> str | None:
        """Return the stream source."""
        if self._password is None:
            return None
        local_ip = self.data["local_ip"]
        # Refresh the RTSP port from the coordinator: the device may report a
        # different port than the one cached at setup time.
        self._local_rtsp_port = self.data["local_rtsp_port"]
        self._rtsp_stream = (
            f"rtsp://{self._username}:{self._password}@"
            f"{local_ip}:{self._local_rtsp_port}{self._ffmpeg_arguments}"
        )
        _LOGGER.debug(
            "Configuring Camera %s with ip: %s rtsp port: %s ffmpeg arguments: %s",
            self._serial,
            local_ip,
            self._local_rtsp_port,
            self._ffmpeg_arguments,
        )

        return self._rtsp_stream

    def perform_wake_device(self) -> None:
        """Basically wakes the camera by querying the device."""
        try:
            self.coordinator.ezviz_client.get_detection_sensibility(self._serial)
        except (HTTPError, PyEzvizError) as err:
            raise PyEzvizError("Cannot wake device") from err
