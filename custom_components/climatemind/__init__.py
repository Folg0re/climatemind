from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api.services import async_setup_services
from .calibration.offset_manager import OffsetManager
from .calibration.offset_store import OffsetStore
from .const import DOMAIN
from .forecast.forecast_feeder import ForecastFeeder
from .forecast.weather_provider import WeatherProvider
from .global_mode.scene_store import SceneStore
from .global_mode.scenes import SceneEngine


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Inizializzazione nodi e moduli ClimateMind."""

    hass.data.setdefault(DOMAIN, {})

    offset_store = OffsetStore(hass)
    offset_manager = OffsetManager(hass, offset_store)
    await offset_manager.async_load()

    scene_store = SceneStore(hass)
    scene_engine = SceneEngine(hass, scene_store)
    await scene_engine.async_load()

    weather_provider = WeatherProvider(hass, None)
    forecast_feeder = ForecastFeeder(hass, weather_provider)

    hass.data[DOMAIN]["offset_manager"] = offset_manager
    hass.data[DOMAIN]["scene_engine"] = scene_engine
    hass.data[DOMAIN]["forecast_feeder"] = forecast_feeder

    await async_setup_services(hass)

    return True
