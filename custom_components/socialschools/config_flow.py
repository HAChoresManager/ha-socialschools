"""Config, reauth and options flows for the Social Schools integration.

The user signs in with the credentials of a parent account.  The password
is used once, inside the flow, to obtain a refresh token through the native
app login; it is never stored or logged.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import logging
import time
from typing import Any

import aiohttp
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .children import parse_children
from .client import SocialSchoolsClient
from .const import (
    CONF_CLIENT_ID,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_USER_PROFILE_ID,
    CONF_USERNAME,
    DEFAULT_CALENDAR_DAYS,
    DEFAULT_POSTS_IN_LIST,
    DEFAULT_POSTS_INTERVAL,
    DOMAIN,
    MAX_CALENDAR_DAYS,
    MAX_POSTS_IN_LIST,
    MAX_POSTS_INTERVAL,
    MIN_CALENDAR_DAYS,
    MIN_POSTS_IN_LIST,
    MIN_POSTS_INTERVAL,
    OIDC_CLIENT_ID,
    OPT_CALENDAR_DAYS,
    OPT_CHILDREN,
    OPT_POSTS_IN_LIST,
    OPT_POSTS_INTERVAL,
    OPT_READERS,
)
from .errors import ApiError, CannotConnect, InvalidAuth, UnsupportedLogin
from .login import async_native_login
from .models import Account, parse_account
from .tokens import TokenManager, TokenResponse, async_exchange_code

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
        ),
    }
)
STEP_REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
        ),
    }
)
OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(OPT_POSTS_INTERVAL): NumberSelector(
            NumberSelectorConfig(
                min=MIN_POSTS_INTERVAL,
                max=MAX_POSTS_INTERVAL,
                step=1,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement=UnitOfTime.MINUTES,
            )
        ),
        vol.Required(OPT_CALENDAR_DAYS): NumberSelector(
            NumberSelectorConfig(
                min=MIN_CALENDAR_DAYS,
                max=MAX_CALENDAR_DAYS,
                step=1,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement=UnitOfTime.DAYS,
            )
        ),
        vol.Required(OPT_POSTS_IN_LIST): NumberSelector(
            NumberSelectorConfig(
                min=MIN_POSTS_IN_LIST, max=MAX_POSTS_IN_LIST, step=1, mode=NumberSelectorMode.BOX
            )
        ),
        vol.Optional(OPT_READERS): EntitySelector(
            EntitySelectorConfig(domain="person", multiple=True)
        ),
        vol.Optional(OPT_CHILDREN): TextSelector(TextSelectorConfig(multiline=True)),
    }
)
OPTION_DEFAULTS: dict[str, int] = {
    OPT_POSTS_INTERVAL: DEFAULT_POSTS_INTERVAL,
    OPT_CALENDAR_DAYS: DEFAULT_CALENDAR_DAYS,
    OPT_POSTS_IN_LIST: DEFAULT_POSTS_IN_LIST,
}


async def _async_login(
    hass: HomeAssistant, username: str, password: str
) -> tuple[TokenResponse, Account]:
    """Sign in like the native app, exchange the code and fetch the account.

    The login runs in a dedicated session with its own cookie jar that is
    detached afterwards; the account call uses the shared session.  The returned
    token pair carries the refresh token that is current after that call.  Raises
    InvalidAuth, UnsupportedLogin, CannotConnect or ApiError.
    """
    session = async_create_clientsession(hass, auto_cleanup=False, cookie_jar=aiohttp.CookieJar())
    try:
        auth = await async_native_login(session, username, password)
        tokens = await async_exchange_code(session, auth.code, auth.code_verifier)
    finally:
        session.detach()
    shared = async_get_clientsession(hass)
    token_manager = TokenManager(
        shared,
        client_id=OIDC_CLIENT_ID,
        refresh_token=tokens.refresh_token,
        access_token=tokens.access_token,
        expires_at=time.monotonic() + tokens.expires_in,
    )
    client = SocialSchoolsClient(shared, token_manager)
    account = parse_account(await client.async_get_current_account())
    # A 401 on the account call makes the client refresh; the identity provider may have
    # rotated the refresh token then, so the one to store is the manager's current one.
    return replace(tokens, refresh_token=token_manager.refresh_token), account


class SocialSchoolsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the user and reauth flows of Social Schools."""

    VERSION = 1
    MINOR_VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SocialSchoolsOptionsFlow:
        """Return the options flow handler."""
        return SocialSchoolsOptionsFlow()

    async def _async_try_login(
        self, username: str, password: str, errors: dict[str, str]
    ) -> tuple[TokenResponse, Account] | None:
        """Sign in; on failure store the error key in ``errors`` and return None."""
        try:
            tokens, account = await _async_login(self.hass, username, password)
        except InvalidAuth:
            errors["base"] = "invalid_auth"
        except (CannotConnect, ApiError):
            errors["base"] = "cannot_connect"
        except UnsupportedLogin:
            errors["base"] = "unsupported"
        except Exception:
            _LOGGER.exception("Unexpected error while signing in to Social Schools")
            errors["base"] = "unknown"
        else:
            if account.schools:
                return tokens, account
            errors["base"] = "no_parent_role"
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the credentials and create the entry for the account."""
        errors: dict[str, str] = {}
        suggested: dict[str, Any] | None = None
        if user_input is not None:
            username = user_input[CONF_USERNAME]
            suggested = {CONF_USERNAME: username}
            login = await self._async_try_login(username, user_input[CONF_PASSWORD], errors)
            if login is not None:
                tokens, account = login
                await self.async_set_unique_id(str(account.user_profile_id))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=account.display_name or username,
                    data={
                        CONF_USERNAME: username,
                        CONF_REFRESH_TOKEN: tokens.refresh_token,
                        CONF_CLIENT_ID: OIDC_CLIENT_ID,
                        CONF_USER_PROFILE_ID: account.user_profile_id,
                    },
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(STEP_USER_SCHEMA, suggested),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start the reauth flow after the stored refresh token was rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again and store the new refresh token."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        username = entry.data[CONF_USERNAME]
        if user_input is not None:
            login = await self._async_try_login(username, user_input[CONF_PASSWORD], errors)
            if login is not None:
                tokens, account = login
                await self.async_set_unique_id(str(account.user_profile_id))
                self._abort_if_unique_id_mismatch(reason="unique_id_mismatch")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_REFRESH_TOKEN: tokens.refresh_token,
                        CONF_CLIENT_ID: OIDC_CLIENT_ID,
                    },
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            description_placeholders={"username": username},
            errors=errors,
        )


class SocialSchoolsOptionsFlow(OptionsFlowWithReload):
    """Polling and display options; saving them reloads the entry."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the options with their current values and store the new ones.

        Numbers are stored as integers, ``lezers`` as a list of person entity ids
        and ``kinderen`` as the text, after checking that every line reads
        ``Naam=groep, groep``.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            children = str(user_input.get(OPT_CHILDREN, "")).strip()
            try:
                parse_children(children)
            except ValueError:
                errors[OPT_CHILDREN] = "invalid_children"
            else:
                readers = user_input.get(OPT_READERS) or []
                return self.async_create_entry(
                    data={key: int(user_input[key]) for key in OPTION_DEFAULTS}
                    | {OPT_READERS: list(dict.fromkeys(readers)), OPT_CHILDREN: children}
                )
        current = dict(self.config_entry.options) if user_input is None else user_input
        suggested = {key: current.get(key, default) for key, default in OPTION_DEFAULTS.items()}
        suggested[OPT_READERS] = current.get(OPT_READERS, [])
        suggested[OPT_CHILDREN] = current.get(OPT_CHILDREN, "")
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, suggested),
            errors=errors,
        )
