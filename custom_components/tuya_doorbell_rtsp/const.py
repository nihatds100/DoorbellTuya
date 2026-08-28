"""Constants for Tuya Doorbell RTSP (LAN-only)."""
DOMAIN = "tuya_doorbell_rtsp"

CONF_REGION = "region"
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_COUNTRY_CODE = "country_code"
CONF_CAMERAS = "cameras"
CONF_RTSP_PORT = "rtsp_port"

DEFAULT_RTSP_PORT = 28554

REGIONS = {
    "eu-central": "Central Europe",
    "eu-east": "East Europe",
    "us-west": "West America",
    "us-east": "East America",
    "china": "China",
    "india": "India",
}

CONF_LOCAL_KEYS = "local_keys"
LOCAL_PROTOCOL_VERSION = 3.3
EVENT_DP = "tuya_doorbell_rtsp_dp"
CONF_DEVICE_IP = "device_ip"
CONF_KEEPWARM = "keep_warm"
