"""Config flow for the KEF LSX integration."""

from __future__ import annotations

import ipaddress
import logging
import re
from contextlib import suppress
from typing import Any
from uuid import uuid4

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, PERCENTAGE
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector

from .const import (
    CONF_INVERSE_ORIENTATION,
    CONF_MAX_VOLUME,
    CONF_PREFERRED_WAKE_SOURCE,
    CONF_STANDBY_TIME,
    CONF_VOLUME_STEP,
    DEFAULT_INVERSE_ORIENTATION,
    DEFAULT_MAX_VOLUME,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_STANDBY_TIME,
    DEFAULT_VOLUME_STEP,
    DOMAIN,
    STANDBY_NEVER,
    STANDBY_SELECTOR_VALUES,
    WAKE_SOURCES,
)

_LOGGER = logging.getLogger(__name__)

_HOST_LABEL = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)$")


class CannotConnect(Exception):
    """The endpoint could not be reached."""


class InvalidHost(Exception):
    """The supplied host is not a valid IP address or hostname."""


def _normalize_host(value: str) -> str:
    """Return a canonical IP address or DNS hostname."""
    host = value.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]

    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        pass

    try:
        host = host.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as err:
        raise InvalidHost from err

    if len(host) > 253 or not host:
        raise InvalidHost
    if any(_HOST_LABEL.fullmatch(label) is None for label in host.split(".")):
        raise InvalidHost
    return host


async def _async_validate_endpoint(host: str, port: int) -> None:
    """Perform one bounded, read-only source query against an endpoint."""
    from .client import LsxClient
    from .errors import LsxError

    client = LsxClient(host, port)
    try:
        await client.async_get_source()
    except LsxError as err:
        raise CannotConnect from err
    finally:
        with suppress(LsxError):
            await client.async_close()


def _endpoint_schema() -> vol.Schema:
    """Return the endpoint input schema."""
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=DEFAULT_NAME): cv.string,
            vol.Required(CONF_HOST): cv.string,
            vol.Required(CONF_PORT, default=DEFAULT_PORT): cv.port,
        }
    )


def _standby_to_selector(value: int | None) -> str:
    """Convert the stored standby value to its selector value."""
    if value is None:
        return STANDBY_NEVER
    return str(value)


def _standby_from_selector(value: str) -> int | None:
    """Convert a selector value to the protocol standby value."""
    if value == STANDBY_NEVER:
        return None
    return int(value)


def _options_schema(options: dict[str, Any]) -> vol.Schema:
    """Return the options input schema."""
    return vol.Schema(
        {
            vol.Optional(
                CONF_PREFERRED_WAKE_SOURCE,
                description={
                    "suggested_value": options.get(CONF_PREFERRED_WAKE_SOURCE)
                },
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(WAKE_SOURCES),
                    translation_key="wake_source",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_MAX_VOLUME,
                default=round(options.get(CONF_MAX_VOLUME, DEFAULT_MAX_VOLUME) * 100),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=100,
                    step=1,
                    unit_of_measurement=PERCENTAGE,
                    mode=selector.NumberSelectorMode.SLIDER,
                )
            ),
            vol.Required(
                CONF_VOLUME_STEP,
                default=round(options.get(CONF_VOLUME_STEP, DEFAULT_VOLUME_STEP) * 100),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=25,
                    step=1,
                    unit_of_measurement=PERCENTAGE,
                    mode=selector.NumberSelectorMode.SLIDER,
                )
            ),
            vol.Required(
                CONF_INVERSE_ORIENTATION,
                default=options.get(
                    CONF_INVERSE_ORIENTATION, DEFAULT_INVERSE_ORIENTATION
                ),
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_STANDBY_TIME,
                default=_standby_to_selector(
                    options.get(CONF_STANDBY_TIME, DEFAULT_STANDBY_TIME)
                ),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(STANDBY_SELECTOR_VALUES),
                    translation_key="standby_time",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


class KefLsxConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for KEF LSX."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> KefLsxOptionsFlow:
        """Return the options flow."""
        return KefLsxOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial user step."""
        return await self._async_step_endpoint("user", user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle endpoint reconfiguration."""
        entry = self._get_reconfigure_entry()
        if user_input is None:
            current_name = entry.title
            if current_name == entry.data[CONF_HOST]:
                current_name = DEFAULT_NAME
            user_input = {
                CONF_NAME: current_name,
                CONF_HOST: entry.data[CONF_HOST],
                CONF_PORT: entry.data.get(CONF_PORT, DEFAULT_PORT),
            }
            return self.async_show_form(
                step_id="reconfigure",
                data_schema=self.add_suggested_values_to_schema(
                    _endpoint_schema(), user_input
                ),
            )
        return await self._async_step_endpoint("reconfigure", user_input, entry)

    async def _async_step_endpoint(
        self,
        step_id: str,
        user_input: dict[str, Any] | None,
        entry: config_entries.ConfigEntry | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Validate and store one endpoint step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                host = _normalize_host(user_input[CONF_HOST])
            except InvalidHost:
                errors["base"] = "invalid_host"
            else:
                name = user_input[CONF_NAME].strip() or DEFAULT_NAME
                endpoint = {
                    CONF_HOST: host,
                    CONF_PORT: user_input[CONF_PORT],
                }
                self._async_abort_entries_match(endpoint)

                unchanged = entry is not None and all(
                    entry.data.get(key) == value for key, value in endpoint.items()
                )
                try:
                    if not unchanged:
                        await _async_validate_endpoint(host, endpoint[CONF_PORT])
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected exception validating KEF LSX")
                    errors["base"] = "unknown"
                else:
                    title = self._unique_title(name, entry)
                    if entry is None:
                        await self.async_set_unique_id(str(uuid4()))
                        return self.async_create_entry(title=title, data=endpoint)
                    return self.async_update_reload_and_abort(
                        entry,
                        title=title,
                        data_updates=endpoint,
                        reason="reconfigure_successful",
                        reload_even_if_entry_is_unchanged=False,
                    )

        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                _endpoint_schema(), user_input
            ),
            errors=errors,
        )

    def _unique_title(
        self, requested: str, current: config_entries.ConfigEntry | None
    ) -> str:
        """Return a readable config-entry title with a deterministic suffix."""
        used = {
            entry.title.casefold()
            for entry in self._async_current_entries()
            if current is None or entry.entry_id != current.entry_id
        }
        if requested.casefold() not in used:
            return requested
        suffix = 2
        while f"{requested}_{suffix}".casefold() in used:
            suffix += 1
        return f"{requested}_{suffix}"


class KefLsxOptionsFlow(config_entries.OptionsFlow):
    """Handle useful KEF LSX options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Manage KEF LSX options."""
        if user_input is not None:
            options = dict(user_input)
            if not options.get(CONF_PREFERRED_WAKE_SOURCE):
                options.pop(CONF_PREFERRED_WAKE_SOURCE, None)
            options[CONF_MAX_VOLUME] = options[CONF_MAX_VOLUME] / 100
            options[CONF_VOLUME_STEP] = options[CONF_VOLUME_STEP] / 100
            options[CONF_STANDBY_TIME] = _standby_from_selector(
                options[CONF_STANDBY_TIME]
            )
            return self.async_create_entry(title="", data=options)

        return self.async_show_form(
            step_id="init", data_schema=_options_schema(dict(self.config_entry.options))
        )
