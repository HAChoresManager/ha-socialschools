"""The Social Schools integration: read-only school announcements and calendar."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, UnknownEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .client import SocialSchoolsClient
from .const import CONF_CLIENT_ID, CONF_REFRESH_TOKEN, DOMAIN, STORAGE_KEY_TEMPLATE, STORAGE_VERSION
from .coordinator import SocialSchoolsCoordinator
from .errors import ApiError, CannotConnect, InvalidAuth
from .models import parse_account
from .tokens import TokenManager

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.CALENDAR, Platform.EVENT, Platform.SENSOR]

type SocialSchoolsConfigEntry = ConfigEntry[SocialSchoolsCoordinator]


def _create_store(hass: HomeAssistant, entry: SocialSchoolsConfigEntry) -> Store[dict[str, Any]]:
    """Return the private store that keeps the new-post bookkeeping of ``entry``."""
    return Store[dict[str, Any]](
        hass, STORAGE_VERSION, STORAGE_KEY_TEMPLATE.format(entry_id=entry.entry_id), private=True
    )


async def async_setup_entry(hass: HomeAssistant, entry: SocialSchoolsConfigEntry) -> bool:
    """Set up Social Schools from a config entry."""
    session = async_get_clientsession(hass)

    @callback
    def _async_refresh_token_updated(token: str) -> None:
        """Persist a rotated refresh token; this fires listeners but never reloads.

        A refresh outlives a cancelled coordinator (see TokenManager), so it can report a
        rotation after the entry was removed: nothing is left to store it in then.
        """
        try:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_REFRESH_TOKEN: token}
            )
        except UnknownEntry:
            _LOGGER.debug("Entry %s was removed during a token refresh", entry.entry_id)

    token_manager = TokenManager(
        session,
        client_id=entry.data[CONF_CLIENT_ID],
        refresh_token=entry.data[CONF_REFRESH_TOKEN],
        on_refresh_token_update=_async_refresh_token_updated,
    )
    client = SocialSchoolsClient(session, token_manager)
    try:
        account = parse_account(await client.async_get_current_account())
    except InvalidAuth as err:
        raise ConfigEntryAuthFailed("The stored Social Schools login is no longer valid") from err
    except (CannotConnect, ApiError) as err:
        raise ConfigEntryNotReady(f"Cannot fetch the Social Schools account: {err}") from err
    if not account.schools:
        raise ConfigEntryError(translation_domain=DOMAIN, translation_key="no_parent_role")

    coordinator = SocialSchoolsCoordinator(hass, entry, client, account, _create_store(hass, entry))
    await coordinator.async_load_store()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _LOGGER.debug("Entry %s set up for %d school(s)", entry.entry_id, len(account.schools))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SocialSchoolsConfigEntry) -> bool:
    """Unload a config entry; the coordinator stops polling through the entry's unload hooks.

    A token refresh that is in flight is awaited first (at most REQUEST_TIMEOUT): Home
    Assistant cancels the coordinator right after this returns, and a refresh token that
    the identity provider rotated meanwhile must be stored before a reload starts over
    from the stored one.
    """
    await entry.runtime_data.client.token_manager.async_wait_for_refresh()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: SocialSchoolsConfigEntry) -> None:
    """Delete the stored new-post bookkeeping of a removed config entry."""
    await _create_store(hass, entry).async_remove()
    _LOGGER.debug("Removed the stored bookkeeping of entry %s", entry.entry_id)
