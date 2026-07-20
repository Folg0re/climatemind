from __future__ import annotations

from homeassistant.core import HomeAssistant


async def notify_advice(hass: HomeAssistant, title: str, message: str) -> None:
    """Invia una notifica persistente in Home Assistant con il consiglio.

    Usa il servizio `persistent_notification.create` e aspetta il completamento.
    """
    await hass.services.async_call(
        "persistent_notification",
        "create",
        {"title": title, "message": message},
        blocking=True,
    )
