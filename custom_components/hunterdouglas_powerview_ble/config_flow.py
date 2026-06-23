"""Config flow for Hunter Douglas PowerView BLE integration."""

import asyncio
import hashlib
import struct
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import CONF_HOME_KEY, CONF_HUB_URL, DOMAIN, LOGGER, MFCT_ID

_DEFAULT_HUB_URL = "http://powerview-g3.local"


def _hub_unique_id(home_key: str) -> str:
    """Derive a stable unique ID for a hub entry from the home key."""
    if home_key:
        digest = hashlib.sha256(home_key.encode()).hexdigest()[:16]
        return f"pvhome_{digest}"
    return "pvhome_unencrypted"


def _parse_key_response(ble_name: str, result: dict) -> bytes | None:  # noqa: PLR0911
    """Parse a shade exec response and return the 16-byte key, or None."""
    if result.get("err"):
        err_msg = (result.get("responses") or [{}])[0].get("errMsg", "unknown")
        LOGGER.warning(
            "Shade %s: hub BLE command failed (err=%s: %s)",
            ble_name,
            result["err"],
            err_msg,
        )
        return None

    responses = result.get("responses", [])
    if len(responses) != 1 or "hex" not in responses[0]:
        LOGGER.warning(
            "Shade %s returned unexpected response structure: %s",
            ble_name,
            result,
        )
        return None

    response_bytes = bytes.fromhex(responses[0]["hex"])
    if len(response_bytes) < 5:
        LOGGER.warning(
            "Shade %s response too short (%d bytes)", ble_name, len(response_bytes)
        )
        return None
    _s, _c, _q, length = struct.unpack("<BBBB", response_bytes[0:4])
    if len(response_bytes) != 4 + length:
        LOGGER.warning(
            "Shade %s frame length mismatch (header=%d, actual=%d)",
            ble_name,
            4 + length,
            len(response_bytes),
        )
        return None
    if response_bytes[4] != 0:
        LOGGER.warning("Shade %s returned error status %d", ble_name, response_bytes[4])
        return None
    key_data = response_bytes[5:]
    if len(key_data) != 16:
        LOGGER.warning(
            "Shade %s returned key of wrong length (%d, expected 16)",
            ble_name,
            len(key_data),
        )
        return None
    return key_data


# The hub connects to shades over BLE on demand, so the first request to each
# shade usually times out while that connection is established.  Retry the whole
# shade list a few times, pausing between passes to let the connections settle.
_KEY_FETCH_PASSES = 3
_KEY_FETCH_PASS_DELAY = 5  # seconds


async def _fetch_key_from_hub(hass: HomeAssistant, hub_url: str) -> bytes:
    """Fetch 16-byte homekey from a PowerView G3 hub.

    Tries each shade on the hub until one returns a valid key.
    The key is network-wide so any reachable shade returns the same value.

    The hub must establish a BLE connection to each shade before it can proxy
    the key request.  On the first pass that connection is often not yet open,
    so the hub returns "command timed out" (err=8).  We retry the whole shade
    list up to _KEY_FETCH_PASSES times, pausing between passes to let the hub
    complete the BLE connections the earlier requests kicked off.

    Raises ValueError on protocol/key errors.
    Raises aiohttp.ClientError on network errors.
    Raises asyncio.TimeoutError on timeout.
    """
    session = async_get_clientsession(hass)
    timeout = aiohttp.ClientTimeout(total=10)

    async with session.get(f"{hub_url}/home/shades", timeout=timeout) as resp:
        resp.raise_for_status()
        shades = await resp.json(content_type=None)

    if not shades:
        raise ValueError("No shades found on the hub")

    # Sort by signal strength (strongest first) — a stronger signal means the
    # hub is more likely to have an active BLE connection to that shade.
    shades.sort(key=lambda s: s.get("signalStrength", -100), reverse=True)
    ble_names = [s["bleName"] for s in shades if s.get("bleName")]
    if not ble_names:
        raise ValueError("No BLE-capable shades found on the hub")

    # GetShadeKey BLE request: sid=251, cid=18, seqId=1, data_len=0
    request_frame = struct.pack("<BBBB", 251, 18, 1, 0)

    last_error: Exception = ValueError("No shades responded")
    for attempt in range(_KEY_FETCH_PASSES):
        if attempt:
            # The previous pass's requests kick the hub into opening its BLE
            # connections (the first attempt usually returns "command timed
            # out").  Pause to let those connections settle, then retry.
            LOGGER.debug(
                "Hub key fetch pass %d failed; retrying after %ds",
                attempt,
                _KEY_FETCH_PASS_DELAY,
            )
            await asyncio.sleep(_KEY_FETCH_PASS_DELAY)

        for ble_name in ble_names:
            try:
                async with session.post(
                    f"{hub_url}/home/shades/exec?shades={ble_name}",
                    json={"hex": request_frame.hex()},
                    timeout=timeout,
                ) as resp:
                    resp.raise_for_status()
                    result = await resp.json(content_type=None)
            except (TimeoutError, aiohttp.ClientError) as ex:
                LOGGER.warning("Shade %s unreachable: %s", ble_name, ex)
                last_error = ex
                continue

            key_data = _parse_key_response(ble_name, result)
            if key_data is not None:
                return key_data

    raise ValueError(f"No reachable shade returned a valid key: {last_error}")


# Maps exceptions raised by _fetch_key_from_hub to config-flow error keys.
_HUB_ERROR_MAP: dict[type[Exception], str] = {
    aiohttp.ClientResponseError: "hub_http_error",
    aiohttp.ClientConnectionError: "hub_connection_error",
    TimeoutError: "hub_timeout",
    ValueError: "hub_protocol_error",
}


def _homekey_schema(hub_url_default: str = _DEFAULT_HUB_URL) -> vol.Schema:
    """Build the homekey form schema, defaulting the hub URL field.

    When a gateway has been discovered via zeroconf, ``hub_url_default`` is the
    discovered URL so the manual-entry fallback comes pre-filled.
    """
    return vol.Schema(
        {
            vol.Required("key_method", default="hub"): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(
                            value="hub",
                            label="Fetch automatically from PowerView hub",
                        ),
                        SelectOptionDict(
                            value="manual",
                            label="Enter key manually (32 hex characters)",
                        ),
                        SelectOptionDict(
                            value="skip",
                            label=(
                                "Skip (no key — controls disabled for "
                                "encrypted shades)"
                            ),
                        ),
                    ]
                )
            ),
            vol.Optional(CONF_HUB_URL, default=hub_url_default): TextSelector(
                TextSelectorConfig(type=TextSelectorType.URL)
            ),
            vol.Optional("home_key", default=""): TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT)
            ),
        }
    )


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Hunter Douglas PowerView BLE."""

    VERSION = 2
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._home_key: str = ""
        self._hub_url: str = ""

    def _existing_hub_entry(self) -> config_entries.ConfigEntry | None:
        """Return the existing hub (v2+) config entry, if any."""
        for entry in self._async_current_entries():
            if entry.version >= 2:
                return entry
        return None

    async def _create_entry(self) -> ConfigFlowResult:
        """Set the stable unique ID and create the hub config entry."""
        await self.async_set_unique_id(
            _hub_unique_id(self._home_key), raise_on_progress=False
        )
        self._abort_if_unique_id_configured()
        data: dict[str, str] = {CONF_HOME_KEY: self._home_key}
        if self._hub_url:
            data[CONF_HUB_URL] = self._hub_url
        return self.async_create_entry(title="PowerView Home", data=data)

    def _show_homekey_form(
        self, step_id: str, errors: dict[str, str], **placeholders: str
    ) -> ConfigFlowResult:
        """Render the key-source form, pre-filling the hub URL when discovered."""
        return self.async_show_form(
            step_id=step_id,
            data_schema=_homekey_schema(self._hub_url or _DEFAULT_HUB_URL),
            errors=errors,
            description_placeholders={
                "hub_url_example": _DEFAULT_HUB_URL,
                **placeholders,
            },
        )

    def _validate_manual_key(
        self, user_input: dict[str, Any], errors: dict[str, str]
    ) -> bool:
        """Validate a manually entered hex key.

        Returns True on success, False on validation error.
        """
        raw = user_input.get("home_key", "").strip()
        if "\\x" in raw:
            raw = raw.replace("\\x", "")
        if len(raw) != 32:
            errors["home_key"] = "invalid_key_length"
            return False
        try:
            bytes.fromhex(raw)
        except ValueError:
            errors["home_key"] = "invalid_key_format"
            return False
        self._home_key = raw.lower()
        return True

    async def _validate_homekey_input(
        self, user_input: dict[str, Any], errors: dict[str, str]
    ) -> bool:
        """Parse and validate homekey user_input, populating self state.

        Returns True on success, False on validation error (errors dict is populated).
        """
        method = user_input.get("key_method", "skip")

        if method == "skip":
            self._home_key = ""
            return True

        if method == "manual":
            # Still capture a hub URL if the user provided one — the integration
            # uses it to fetch shade metadata (friendly names, power_type) even
            # when the homekey itself was supplied manually.
            hub_url = user_input.get(CONF_HUB_URL, "").rstrip("/")
            if hub_url:
                self._hub_url = hub_url
            return self._validate_manual_key(user_input, errors)

        if method != "hub":
            return False

        hub_url = user_input.get(CONF_HUB_URL, "").rstrip("/")
        try:
            key = await _fetch_key_from_hub(self.hass, hub_url)
        except tuple(_HUB_ERROR_MAP) as ex:
            LOGGER.warning("Hub key fetch failed: %s", ex)
            errors[CONF_HUB_URL] = _HUB_ERROR_MAP[type(ex)]
            return False

        self._home_key = key.hex()
        self._hub_url = hub_url
        return True

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a flow initialized by Bluetooth discovery."""
        LOGGER.debug("Bluetooth device detected: %s", discovery_info)

        # Derive a home-wide unique ID from the home_id embedded in the BLE
        # advertisement (bytes 0-1 of the manufacturer payload).  All shades on
        # the same network share the same home_id, so HA deduplicates every
        # subsequent shade discovery into this single flow via
        # "already_in_progress" rather than spawning one notification per shade.
        mfr_data = bytearray(discovery_info.manufacturer_data.get(MFCT_ID, b""))
        if len(mfr_data) >= 2:
            home_id = int.from_bytes(mfr_data[0:2], byteorder="little")
            unique_id = f"pvhome_{home_id}"
        else:
            unique_id = DOMAIN

        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        # If a hub entry already exists (unique_id may differ), shades are
        # auto-discovered internally — nothing more for the user to do.
        if self._existing_hub_entry():
            return self.async_abort(reason="already_configured")

        # No hub entry yet — redirect to user setup
        return await self.async_step_user()

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a flow initialized by zeroconf discovery of a G3 gateway."""
        LOGGER.debug("Zeroconf gateway detected: %s", discovery_info)

        host = discovery_info.hostname.rstrip(".") or str(discovery_info.ip_address)
        self._hub_url = f"http://{host}"

        # Dedup repeated mDNS announcements while a flow is in progress.
        await self.async_set_unique_id(f"gateway_{host}")

        # If the single hub entry already exists, just record the (possibly
        # changed) gateway URL so metadata/key fetches keep working after a
        # DHCP/hostname change, then abort — there is nothing for the user to do.
        entry = self._existing_hub_entry()
        if entry:
            if entry.data.get(CONF_HUB_URL) != self._hub_url:
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, CONF_HUB_URL: self._hub_url},
                )
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_abort(reason="already_configured")

        self.context["title_placeholders"] = {"name": host}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a discovered gateway and choose how to obtain the key.

        Offers the same options as the manual user step (fetch from the hub,
        enter the key manually, or skip) with the discovered URL pre-filled, so
        the user can bypass the automatic hub fetch if they prefer.
        """
        errors: dict[str, str] = {}

        if user_input is not None and await self._validate_homekey_input(
            user_input, errors
        ):
            return await self._create_entry()

        return self._show_homekey_form(
            "zeroconf_confirm", errors, name=self._hub_url
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the user step — create a hub entry."""
        LOGGER.debug("user step")

        # Only one hub entry allowed (per key, but for simplicity one total)
        if self._existing_hub_entry():
            return self.async_abort(reason="single_instance_allowed")

        errors: dict[str, str] = {}

        if user_input is not None and await self._validate_homekey_input(
            user_input, errors
        ):
            return await self._create_entry()

        return self._show_homekey_form("user", errors)
