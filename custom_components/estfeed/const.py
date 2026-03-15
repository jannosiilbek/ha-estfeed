"""Constants for the Estfeed integration."""

DOMAIN = "estfeed"

CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_APARTMENT_AREA = "apartment_area_m2"
CONF_BUILDING_AREA = "building_area_m2"

DEFAULT_UPDATE_INTERVAL = 3600  # 1 hour

TOKEN_URL = "https://kc.elering.ee/realms/elering-sso/protocol/openid-connect/token"
BASE_URL = "https://estfeed.elering.ee"
ELERING_PRICE_URL = "https://dashboard.elering.ee/api/nps/price"
