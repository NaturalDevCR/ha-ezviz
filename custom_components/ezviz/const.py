"""Constants for the ezviz integration."""

DOMAIN = "ezviz"
MANUFACTURER = "EZVIZ"

# Configuration
ATTR_SERIAL = "serial"
CONF_FFMPEG_ARGUMENTS = "ffmpeg_arguments"
ATTR_TYPE_CLOUD = "EZVIZ_CLOUD_ACCOUNT"
ATTR_TYPE_CAMERA = "CAMERA_ACCOUNT"
CONF_SESSION_ID = "session_id"
CONF_RFSESSION_ID = "rf_session_id"
CONF_EZVIZ_ACCOUNT = "ezviz_account"
# Verification code (printed on the device label) used to decrypt
# encrypted alarm/motion images. May differ from the RTSP password.
CONF_ENC_KEY = "enc_key"

# Services data
DIR_UP = "up"
DIR_DOWN = "down"
DIR_LEFT = "left"
DIR_RIGHT = "right"
ATTR_ENABLE = "enable"
ATTR_DIRECTION = "direction"
ATTR_SPEED = "speed"
ATTR_LEVEL = "level"
ATTR_TYPE = "type_value"
ATTR_METHOD = "method"
ATTR_PATH = "path"
ATTR_DATA = "data"
ATTR_PARAMS = "params"
ATTR_CHANNEL = "channel"
ATTR_BODY_FORMAT = "body_format"
ATTR_LOG_RESPONSE = "log_response"
ATTR_REDACT_RESPONSE = "redact_response"

# Service names
SERVICE_WAKE_DEVICE = "wake_device"
SERVICE_DETECTION_SENSITIVITY = "set_alarm_detection_sensibility"
SERVICE_DEBUG_AUTHENTICATED_REQUEST = "debug_authenticated_request"

# Defaults
EU_URL = "apiieu.ezvizlife.com"
RUSSIA_URL = "apirus.ezvizru.com"
DEFAULT_CAMERA_USERNAME = "admin"
DEFAULT_TIMEOUT = 25
DEFAULT_FFMPEG_ARGUMENTS = "/Streaming/Channels/102"
