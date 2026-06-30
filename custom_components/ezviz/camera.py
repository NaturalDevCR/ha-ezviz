"""Support ezviz camera devices."""

from collections.abc import Mapping
import inspect
import logging
import re
from typing import Any
from typing import override

from pyezvizapi.exceptions import HTTPError, InvalidHost, PyEzvizError
import requests
import voluptuous as vol

from homeassistant.components import ffmpeg
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.components.stream import CONF_USE_WALLCLOCK_AS_TIMESTAMPS
from homeassistant.config_entries import SOURCE_IGNORE, SOURCE_INTEGRATION_DISCOVERY
from homeassistant.const import CONF_IP_ADDRESS, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import discovery_flow
from homeassistant.helpers.entity_platform import (
    AddConfigEntryEntitiesCallback,
    async_get_current_platform,
)

from .const import (
    ATTR_SERIAL,
    ATTR_BODY_FORMAT,
    ATTR_CHANNEL,
    ATTR_DATA,
    ATTR_HEADERS,
    ATTR_LOG_RESPONSE,
    ATTR_METHOD,
    ATTR_PARAMS,
    ATTR_PATH,
    ATTR_REDACT_RESPONSE,
    CONF_FFMPEG_ARGUMENTS,
    DEFAULT_CAMERA_USERNAME,
    DEFAULT_FFMPEG_ARGUMENTS,
    DOMAIN,
    SERVICE_DEBUG_AUTHENTICATED_REQUEST,
    SERVICE_WAKE_DEVICE,
)
from .coordinator import EzvizConfigEntry, EzvizDataUpdateCoordinator
from .entity import EzvizEntity

_LOGGER = logging.getLogger(__name__)

_RTSP_CHANNEL_RE = re.compile(r"(/Streaming/Channels/)(\d+)")
_REQUEST_METHODS = {"DELETE", "GET", "POST", "PUT"}
_BODY_FORMATS = {"form", "json"}
_REDACTED = "**REDACTED**"
_SENSITIVE_KEYS = {
    "access_token",
    "accesstoken",
    "authorization",
    "authcode",
    "encrypted_pwd_hash",
    "oldpassword",
    "password",
    "refreshsessionid",
    "rfsessionid",
    "rf_session_id",
    "sessionid",
    "session_id",
    "streamtoken",
    "ticket",
    "token",
    "validatecode",
}

DEBUG_AUTHENTICATED_REQUEST_SCHEMA = {
    vol.Required(ATTR_PATH): cv.string,
    vol.Optional(ATTR_METHOD, default="PUT"): vol.All(
        cv.string, str.upper, vol.In(_REQUEST_METHODS)
    ),
    vol.Optional(ATTR_CHANNEL, default=0): vol.All(vol.Coerce(int), vol.Range(min=0)),
    vol.Optional(ATTR_PARAMS, default=dict): dict,
    vol.Optional(ATTR_HEADERS, default=dict): dict,
    vol.Optional(ATTR_DATA, default=dict): dict,
    vol.Optional(ATTR_BODY_FORMAT, default="form"): vol.In(_BODY_FORMATS),
    vol.Optional(ATTR_LOG_RESPONSE, default=True): cv.boolean,
    vol.Optional(ATTR_REDACT_RESPONSE, default=True): cv.boolean,
}

try:
    from homeassistant.core import SupportsResponse
except ImportError:
    SupportsResponse = None


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


def _supports_service_response(register_service) -> bool:
    """Return whether this Home Assistant version supports service responses."""
    if SupportsResponse is None:
        return False
    return "supports_response" in inspect.signature(register_service).parameters


def _render_template_value(value: Any, *, serial: str, channel: int) -> Any:
    """Render service placeholders in nested request values."""
    if isinstance(value, str):
        return value.format(serial=serial, channel=channel)
    if isinstance(value, Mapping):
        return {
            _render_template_value(key, serial=serial, channel=channel): (
                _render_template_value(item, serial=serial, channel=channel)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _render_template_value(item, serial=serial, channel=channel)
            for item in value
        ]
    return value


def _secret_values(token: Mapping[str, Any]) -> set[str]:
    """Return known secret token values."""
    return {
        value
        for key, value in token.items()
        if key in {"session_id", "rf_session_id"}
        if isinstance(value, str) and len(value) > 4
    }


def _redact_data(value: Any, secret_values: set[str]) -> Any:
    """Redact sensitive keys from nested structures."""
    if isinstance(value, Mapping):
        redacted = {}
        for key, item in value.items():
            if str(key).replace("-", "_").lower() in _SENSITIVE_KEYS:
                redacted[key] = _REDACTED
            else:
                redacted[key] = _redact_data(item, secret_values)
        return redacted
    if isinstance(value, list):
        return [_redact_data(item, secret_values) for item in value]
    if isinstance(value, str):
        return _redact_text(value, secret_values)
    return value


def _redact_text(text: str, secret_values: set[str]) -> str:
    """Redact known token values from text responses."""
    redacted = text
    for value in secret_values:
        redacted = redacted.replace(value, _REDACTED)
    return redacted


def _validate_debug_request_path(path: str) -> None:
    """Validate a debug request path before appending it to the EZVIZ API host."""
    if not path.startswith("/") or path.startswith("//") or "://" in path:
        raise HomeAssistantError(
            "EZVIZ debug request path must be a relative API path starting with /"
        )
    if any(char in path for char in ("\r", "\n")):
        raise HomeAssistantError("EZVIZ debug request path cannot contain newlines")


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

    response_kwargs: dict[str, Any] = {}
    if _supports_service_response(platform.async_register_entity_service):
        response_kwargs["supports_response"] = SupportsResponse.OPTIONAL

    platform.async_register_entity_service(
        SERVICE_DEBUG_AUTHENTICATED_REQUEST,
        DEBUG_AUTHENTICATED_REQUEST_SCHEMA,
        "async_perform_debug_authenticated_request",
        **response_kwargs,
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

    async def async_perform_debug_authenticated_request(
        self,
        method: str,
        path: str,
        channel: int,
        params: dict[str, Any],
        headers: dict[str, Any],
        data: dict[str, Any],
        body_format: str,
        log_response: bool,
        redact_response: bool,
    ) -> dict[str, Any]:
        """Perform an authenticated EZVIZ API request for endpoint discovery."""
        try:
            result = await self.hass.async_add_executor_job(
                self._perform_debug_authenticated_request,
                method,
                path,
                channel,
                params,
                headers,
                data,
                body_format,
                redact_response,
            )
        except (HTTPError, PyEzvizError) as err:
            raise HomeAssistantError(
                f"EZVIZ debug request failed for camera {self._serial}: {err}"
            ) from err

        if log_response:
            _LOGGER.warning("EZVIZ debug authenticated request result: %s", result)

        return result

    def _perform_debug_authenticated_request(
        self,
        method: str,
        path: str,
        channel: int,
        params: dict[str, Any],
        headers: dict[str, Any],
        data: dict[str, Any],
        body_format: str,
        redact_response: bool,
        max_retries: int = 1,
    ) -> dict[str, Any]:
        """Perform an authenticated EZVIZ API request."""
        client = self.coordinator.ezviz_client
        session = getattr(client, "_session", None)
        token = getattr(client, "_token", {})

        if session is None or not token.get("api_url"):
            raise PyEzvizError("Authenticated EZVIZ client is not ready")

        try:
            rendered_path = _render_template_value(
                path, serial=self._serial, channel=channel
            )
            rendered_params = _render_template_value(
                params, serial=self._serial, channel=channel
            )
            rendered_headers = _render_template_value(
                headers, serial=self._serial, channel=channel
            )
            rendered_data = _render_template_value(
                data, serial=self._serial, channel=channel
            )
        except (KeyError, ValueError) as err:
            raise PyEzvizError(f"Could not render request placeholders: {err}") from err

        _validate_debug_request_path(rendered_path)

        url = f"https://{token['api_url']}{rendered_path}"
        request_kwargs: dict[str, Any] = {
            "url": url,
            "params": rendered_params or None,
            "timeout": getattr(client, "_timeout", 25),
        }
        if rendered_headers:
            request_kwargs["headers"] = rendered_headers

        if body_format == "json":
            request_kwargs["json"] = rendered_data or None
        else:
            request_kwargs["data"] = rendered_data or None

        try:
            response = session.request(method, **request_kwargs)
            if (
                response.status_code == 401
                and max_retries > 0
            ):
                client.login()
                return self._perform_debug_authenticated_request(
                    method,
                    path,
                    channel,
                    params,
                    headers,
                    data,
                    body_format,
                    redact_response,
                    max_retries - 1,
                )
        except requests.RequestException as err:
            raise PyEzvizError(f"Could not perform EZVIZ debug request: {err}") from err

        try:
            response_body: Any = response.json()
        except ValueError:
            response_body = response.text[:4000]

        result = {
            "method": method,
            "path": rendered_path,
            "status_code": response.status_code,
            "ok": response.ok,
            "request": {
                "params": rendered_params,
                "headers": rendered_headers,
                "data": rendered_data,
                "body_format": body_format,
            },
            "response": response_body,
        }

        if not redact_response:
            return result

        return _redact_data(result, _secret_values(token))
