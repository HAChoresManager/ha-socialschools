"""Tests for the user, reauth and options flows of the Social Schools integration.

The cookie-based native login and the code exchange are patched (they need a
dedicated session against the login server); the account call that follows
runs through the real client against the fake API of ``mock_api``.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import timedelta
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import aiohttp
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState, ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.socialschools.const import (
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
    OPT_POSTS_IN_LIST,
    OPT_POSTS_INTERVAL,
)
from custom_components.socialschools.errors import (
    ApiError,
    CannotConnect,
    InvalidAuth,
    UnsupportedLogin,
)
from custom_components.socialschools.login import AuthorizationResult
from custom_components.socialschools.tokens import TokenResponse
from tests.conftest import ACCOUNT_PATH, TOKEN_PATH, USER_PROFILE_ID, ApiState, make_account

USERNAME = "private-user@example.test"
PASSWORD = "private-password"
USER_INPUT = {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
ENTRY_DATA = {
    CONF_USERNAME: USERNAME,
    CONF_REFRESH_TOKEN: "private-refresh",
    CONF_CLIENT_ID: OIDC_CLIENT_ID,
    CONF_USER_PROFILE_ID: USER_PROFILE_ID,
}
NEW_TOKENS = TokenResponse("private-access-new", "private-refresh-new", 3600)
VALID_OPTIONS = {OPT_POSTS_INTERVAL: 20, OPT_CALENDAR_DAYS: 30, OPT_POSTS_IN_LIST: 5}

FLOW_MODULE = "custom_components.socialschools.config_flow"


@pytest.fixture
def mock_login() -> Generator[AsyncMock]:
    """Replace the native login with one that returns a synthetic authorization code."""
    with patch(
        f"{FLOW_MODULE}.async_native_login",
        return_value=AuthorizationResult("private-code", "private-verifier"),
    ) as mock:
        yield mock


@pytest.fixture
def mock_exchange() -> Generator[AsyncMock]:
    """Replace the code exchange with one that returns a synthetic token pair."""
    with patch(
        f"{FLOW_MODULE}.async_exchange_code",
        return_value=TokenResponse("private-access", "private-refresh", 3600),
    ) as mock:
        yield mock


async def _start_user_flow(hass: HomeAssistant) -> ConfigFlowResult:
    """Start the user flow and return its first form."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}
    return result


def _selector(result: ConfigFlowResult, key: str) -> Any:
    """Return the selector of ``key`` in the schema of a form result."""
    return next(value for marker, value in result["data_schema"].schema.items() if marker == key)


def _suggested_value(result: ConfigFlowResult, key: str) -> Any:
    """Return the suggested value of ``key`` in the schema of a form result, if any."""
    marker = next(marker for marker in result["data_schema"].schema if marker == key)
    return (marker.description or {}).get("suggested_value")


async def test_user_flow_creates_entry(
    hass: HomeAssistant,
    mock_api: AiohttpClientMocker,
    mock_login: AsyncMock,
    mock_exchange: AsyncMock,
    api_state: ApiState,
) -> None:
    """The happy path creates a loaded entry with the account's name, id and refresh token."""
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Private Parent"
    assert result["data"] == ENTRY_DATA
    assert CONF_PASSWORD not in result["data"]
    entry = result["result"]
    assert entry.unique_id == str(USER_PROFILE_ID)
    assert entry.state is ConfigEntryState.LOADED
    mock_login.assert_awaited_once_with(ANY, USERNAME, PASSWORD)
    mock_exchange.assert_awaited_once_with(ANY, "private-code", "private-verifier")
    # The flow fetched the account with the exchanged access token, without a refresh first.
    (_, _, headers), *_ = api_state.requests_for(ACCOUNT_PATH)
    assert headers["Authorization"] == "Bearer private-access"
    assert headers["User-Agent"] == "HomeAssistant-SocialSchools/0.1.0"


async def test_user_flow_title_falls_back_to_username(
    hass: HomeAssistant,
    mock_api: AiohttpClientMocker,
    mock_login: AsyncMock,
    mock_exchange: AsyncMock,
    api_state: ApiState,
) -> None:
    """An account without a display name is titled after the username."""
    api_state.account = make_account(display_name="")
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == USERNAME


async def test_password_never_leaves_the_login(
    hass: HomeAssistant,
    mock_api: AiohttpClientMocker,
    mock_login: AsyncMock,
    mock_exchange: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only the native login sees the password: not the exchange, the API, the entry or the log."""
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    mock_login.assert_awaited_once_with(ANY, USERNAME, PASSWORD)
    assert PASSWORD not in str(mock_exchange.call_args)
    assert PASSWORD not in str(dict(result["result"].data))
    assert all(PASSWORD not in str(call) for call in mock_api.mock_calls)
    assert PASSWORD not in caplog.text


@pytest.mark.parametrize(
    ("error", "key"),
    [
        pytest.param(InvalidAuth("credentials rejected"), "invalid_auth", id="invalid_auth"),
        pytest.param(CannotConnect("timeout"), "cannot_connect", id="cannot_connect"),
        pytest.param(ApiError("unexpected status", 418), "cannot_connect", id="api_error"),
        pytest.param(UnsupportedLogin("extra sign-in step"), "unsupported", id="unsupported"),
        pytest.param(RuntimeError("boom"), "unknown", id="unknown"),
    ],
)
async def test_user_flow_login_errors(
    hass: HomeAssistant,
    mock_api: AiohttpClientMocker,
    mock_login: AsyncMock,
    mock_exchange: AsyncMock,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    key: str,
) -> None:
    """A login failure shows the form again with the mapped error; a retry can succeed."""
    mock_login.side_effect = error
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": key}
    assert _suggested_value(result, CONF_USERNAME) == USERNAME
    assert _suggested_value(result, CONF_PASSWORD) is None
    assert PASSWORD not in caplog.text
    assert not hass.config_entries.async_entries(DOMAIN)

    mock_login.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == ENTRY_DATA


@pytest.mark.parametrize(
    ("status", "key", "account_calls"),
    [
        pytest.param(503, "cannot_connect", 1, id="server_error"),
        pytest.param(404, "cannot_connect", 1, id="api_error"),
        pytest.param(401, "invalid_auth", 2, id="token_rejected"),
    ],
)
async def test_user_flow_account_errors(
    hass: HomeAssistant,
    mock_api: AiohttpClientMocker,
    mock_login: AsyncMock,
    mock_exchange: AsyncMock,
    api_state: ApiState,
    status: int,
    key: str,
    account_calls: int,
) -> None:
    """Failures of the account call are mapped through the real client; a 401 is retried once."""
    api_state.account_status = status
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": key}
    assert len(api_state.requests_for(ACCOUNT_PATH)) == account_calls
    assert len(api_state.requests_for(TOKEN_PATH)) == account_calls - 1

    api_state.account_status = 200
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == ENTRY_DATA


async def test_user_flow_stores_the_refresh_token_rotated_by_the_account_call(
    hass: HomeAssistant,
    mock_api: AiohttpClientMocker,
    mock_login: AsyncMock,
    mock_exchange: AsyncMock,
    api_state: ApiState,
) -> None:
    """A 401 on the account call forces a refresh; the token it rotated to is what gets stored."""
    api_state.account_status_queue.append(401)
    api_state.token_rotates_to = "private-rotated"
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {**ENTRY_DATA, CONF_REFRESH_TOKEN: "private-rotated"}
    assert result["result"].state is ConfigEntryState.LOADED
    # Two account calls by the flow (401, then 200 after the refresh) and one by the setup.
    assert len(api_state.requests_for(ACCOUNT_PATH)) == 3
    # The flow refreshed with the exchanged token; the setup then used the rotated one.
    assert [call[2]["refresh_token"] for call in mock_api.mock_calls if call[0] == "POST"] == [
        "private-refresh",
        "private-rotated",
    ]


async def test_user_flow_without_parent_role(
    hass: HomeAssistant,
    mock_api: AiohttpClientMocker,
    mock_login: AsyncMock,
    mock_exchange: AsyncMock,
    api_state: ApiState,
) -> None:
    """An account without a parent role is refused until it has one."""
    api_state.account = make_account(schools=[])
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_parent_role"}

    api_state.account = make_account()
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == ENTRY_DATA


async def test_user_flow_aborts_when_already_configured(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    mock_login: AsyncMock,
    mock_exchange: AsyncMock,
) -> None:
    """A second entry for the same user profile id is refused."""
    mock_config_entry.add_to_hass(hass)
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_login_session_is_dedicated_and_detached(
    hass: HomeAssistant,
    mock_api: AiohttpClientMocker,
    mock_login: AsyncMock,
    mock_exchange: AsyncMock,
) -> None:
    """The login uses its own cookie session, detached after success and after failure alike."""
    session = MagicMock(name="login_session")
    with patch(f"{FLOW_MODULE}.async_create_clientsession", return_value=session) as create:
        mock_login.side_effect = CannotConnect("timeout")
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
        assert result["errors"] == {"base": "cannot_connect"}
        session.detach.assert_called_once()

        mock_login.side_effect = None
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert session.detach.call_count == 2
    session.close.assert_not_called()
    assert create.call_count == 2
    create.assert_called_with(hass, auto_cleanup=False, cookie_jar=ANY)
    assert isinstance(create.call_args.kwargs["cookie_jar"], aiohttp.CookieJar)
    assert all(call.args[0] is session for call in mock_login.call_args_list)
    assert all(call.args[0] is session for call in mock_exchange.call_args_list)


async def test_reauth_flow(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_login: AsyncMock,
    mock_exchange: AsyncMock,
) -> None:
    """Reauth asks for the password only, stores the new refresh token and reloads the entry."""
    mock_exchange.return_value = NEW_TOKENS
    result = await init_integration.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {}
    assert result["description_placeholders"]["username"] == USERNAME
    assert list(result["data_schema"].schema) == [CONF_PASSWORD]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: PASSWORD}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert init_integration.data == {**ENTRY_DATA, CONF_REFRESH_TOKEN: "private-refresh-new"}
    assert init_integration.state is ConfigEntryState.LOADED
    assert not hass.config_entries.flow.async_progress()
    mock_login.assert_awaited_once_with(ANY, USERNAME, PASSWORD)


async def test_reauth_stores_the_refresh_token_rotated_by_the_account_call(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    mock_login: AsyncMock,
    mock_exchange: AsyncMock,
    api_state: ApiState,
) -> None:
    """Reauth stores the refresh token that is current after the account call, not the exchanged one."""
    mock_exchange.return_value = NEW_TOKENS
    api_state.account_status_queue.append(401)
    api_state.token_rotates_to = "private-rotated"
    result = await init_integration.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: PASSWORD}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert init_integration.data == {**ENTRY_DATA, CONF_REFRESH_TOKEN: "private-rotated"}
    assert init_integration.state is ConfigEntryState.LOADED
    assert [call[2]["refresh_token"] for call in mock_api.mock_calls if call[0] == "POST"] == [
        "private-refresh",  # the first setup
        "private-refresh-new",  # the flow's refresh after the 401
        "private-rotated",  # the reload
    ]


async def test_reauth_rejects_another_account(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    mock_login: AsyncMock,
    mock_exchange: AsyncMock,
    api_state: ApiState,
) -> None:
    """Credentials of a different user profile abort the reauth and change nothing."""
    mock_config_entry.add_to_hass(hass)
    mock_exchange.return_value = NEW_TOKENS
    api_state.account = make_account(user_profile_id=9999)
    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    assert mock_config_entry.data == ENTRY_DATA
    assert mock_config_entry.unique_id == str(USER_PROFILE_ID)


async def test_reauth_shows_login_errors(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: AiohttpClientMocker,
    mock_login: AsyncMock,
    mock_exchange: AsyncMock,
) -> None:
    """A wrong password keeps the reauth form open with the error; a retry can succeed."""
    mock_config_entry.add_to_hass(hass)
    mock_exchange.return_value = NEW_TOKENS
    mock_login.side_effect = InvalidAuth("credentials rejected")
    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "invalid_auth"}
    assert result["description_placeholders"]["username"] == USERNAME
    assert mock_config_entry.data == ENTRY_DATA

    mock_login.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: PASSWORD}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data == {**ENTRY_DATA, CONF_REFRESH_TOKEN: "private-refresh-new"}
    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_options_flow_shows_defaults(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Without stored options the form suggests the defaults in box-mode number fields."""
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert _suggested_value(result, OPT_POSTS_INTERVAL) == DEFAULT_POSTS_INTERVAL
    assert _suggested_value(result, OPT_CALENDAR_DAYS) == DEFAULT_CALENDAR_DAYS
    assert _suggested_value(result, OPT_POSTS_IN_LIST) == DEFAULT_POSTS_IN_LIST
    interval = _selector(result, OPT_POSTS_INTERVAL).config
    assert interval["unit_of_measurement"] == "min"
    assert interval["mode"] == "box"
    assert (interval["min"], interval["max"]) == (MIN_POSTS_INTERVAL, MAX_POSTS_INTERVAL)
    days = _selector(result, OPT_CALENDAR_DAYS).config
    assert days["unit_of_measurement"] == "d"
    assert (days["min"], days["max"]) == (MIN_CALENDAR_DAYS, MAX_CALENDAR_DAYS)
    posts = _selector(result, OPT_POSTS_IN_LIST).config
    assert (posts["min"], posts["max"]) == (MIN_POSTS_IN_LIST, MAX_POSTS_IN_LIST)


@pytest.mark.parametrize("entry_options", [VALID_OPTIONS])
async def test_options_flow_shows_stored_values(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Stored options are suggested instead of the defaults."""
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    assert {key: _suggested_value(result, key) for key in VALID_OPTIONS} == VALID_OPTIONS


async def test_options_flow_stores_integers_and_reloads(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Saving stores integer options and reloads the entry with the new poll interval."""
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], VALID_OPTIONS)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == VALID_OPTIONS
    assert init_integration.options == VALID_OPTIONS
    assert all(type(value) is int for value in init_integration.options.values())
    assert init_integration.state is ConfigEntryState.LOADED
    assert init_integration.runtime_data.update_interval == timedelta(minutes=20)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        (OPT_POSTS_INTERVAL, MIN_POSTS_INTERVAL - 1),
        (OPT_POSTS_INTERVAL, MAX_POSTS_INTERVAL + 1),
        (OPT_CALENDAR_DAYS, MIN_CALENDAR_DAYS - 1),
        (OPT_CALENDAR_DAYS, MAX_CALENDAR_DAYS + 1),
        (OPT_POSTS_IN_LIST, MIN_POSTS_IN_LIST - 1),
        (OPT_POSTS_IN_LIST, MAX_POSTS_IN_LIST + 1),
    ],
)
async def test_options_flow_rejects_values_out_of_range(
    hass: HomeAssistant, init_integration: MockConfigEntry, key: str, value: int
) -> None:
    """The selectors reject values outside their range; the flow then still accepts valid ones."""
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    with pytest.raises(InvalidData) as excinfo:
        await hass.config_entries.options.async_configure(
            result["flow_id"], {**VALID_OPTIONS, key: value}
        )
    assert list(excinfo.value.schema_errors) == [key]
    assert init_integration.options == {}

    result = await hass.config_entries.options.async_configure(result["flow_id"], VALID_OPTIONS)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert init_integration.options == VALID_OPTIONS
