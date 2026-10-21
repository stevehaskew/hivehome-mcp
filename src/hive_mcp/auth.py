"""Hive authentication: interactive first login, unattended device re-login.

Two library bugs in pyhive-integration 1.0.10 are worked around in ManagedHive:

1. Session.login() (found in the sign-in spike) asks Cognito whether the device
   is "remembered" using an access token a fresh process does not have, and
   gives up with HiveInvalidDeviceAuthentication. Overridden with a plain
   device-SRP flow.
2. hiveRefreshTokens() holds the non-reentrant _refreshLock while falling back
   to _retryLogin(), which ends by calling hiveRefreshTokens(force_refresh=True)
   and waits on that same lock forever. Overridden so a successful re-login
   (which already yields fresh tokens) does not refresh again.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ._hivelib import Auth, Hive, hive_exceptions
from .credentials import Credentials, DeviceData, Tokens

_LOGGER = logging.getLogger(__name__)

AuthResult = dict[str, Any]


class LoginFailed(RuntimeError):
    """A login attempt failed in a way the user needs to hear about."""


class ReauthRequired(RuntimeError):
    """Unattended login is impossible; the user must run `hive-mcp login`."""


@dataclass(frozen=True)
class LoginOutcome:
    tokens: Tokens
    device_data: DeviceData
    sms_used: bool


def _challenge(result: AuthResult | None) -> str | None:
    return result.get("ChallengeName") if result else None


def _has_tokens(result: AuthResult | None) -> bool:
    return bool(result) and "AuthenticationResult" in result  # type: ignore[operator]


async def interactive_login(
    username: str,
    password: str,
    device_name: str,
    get_sms_code: Callable[[], str],
) -> LoginOutcome:
    """Password login (plus SMS code if Hive asks), then register this device."""
    auth = Auth(username=username, password=password)
    try:
        result: AuthResult | None = await auth.login()
        sms_used = False
        if _challenge(result) == Auth.SMS_MFA_CHALLENGE:
            code = await asyncio.to_thread(get_sms_code)
            result = await auth.sms_2fa(code.strip(), result)
            sms_used = True
    except hive_exceptions.HiveInvalidUsername as err:
        raise LoginFailed("Hive doesn't recognise that email address.") from err
    except hive_exceptions.HiveInvalidPassword as err:
        raise LoginFailed("Hive rejected the password.") from err
    except hive_exceptions.HiveInvalid2FACode as err:
        raise LoginFailed("Hive rejected the SMS code.") from err
    except hive_exceptions.HiveApiError as err:
        raise LoginFailed("Couldn't reach Hive. Check your internet connection.") from err

    if not _has_tokens(result):
        raise LoginFailed(f"Unexpected login response (challenge: {_challenge(result)}).")
    assert result is not None
    if not auth.device_key:
        raise LoginFailed(
            "Hive didn't issue device keys, so unattended re-login won't be possible."
        )

    await auth.device_registration(device_name)
    group_key, key, device_password = await auth.get_device_data()
    _LOGGER.info("Registered device %r (key …%s).", device_name, key[-6:])
    return LoginOutcome(
        tokens=Tokens.from_auth_result(result),
        device_data=DeviceData(group_key, key, device_password),
        sms_used=sms_used,
    )


async def device_login(username: str, password: str, device: DeviceData) -> AuthResult:
    """Log in with saved device keys. Never asks for SMS; raises ReauthRequired instead."""
    auth = Auth(username, password, *device)
    try:
        result: AuthResult | None = await auth.login()
        if _challenge(result) == Auth.DEVICE_VERIFIER_CHALLENGE:
            result = await auth.device_login()
    except (hive_exceptions.HiveInvalidUsername, hive_exceptions.HiveInvalidPassword) as err:
        raise ReauthRequired("Hive rejected the saved username or password.") from err
    except hive_exceptions.HiveInvalidDeviceAuthentication as err:
        raise ReauthRequired("Hive no longer accepts this device's keys.") from err

    if _challenge(result) == Auth.SMS_MFA_CHALLENGE:
        raise ReauthRequired("Hive wants an SMS code; the device is no longer trusted.")
    if not _has_tokens(result):
        raise ReauthRequired(f"Device login failed (challenge: {_challenge(result)}).")
    assert result is not None
    _LOGGER.info("Device login succeeded.")
    return result


async def forget_device(creds: Credentials) -> bool:
    """Deregister the stored device in Cognito. Best effort; returns success."""
    if creds.device_data is None:
        return False
    auth = Auth(creds.username, creds.password, *creds.device_data)
    try:
        access_token: str | None = None
        if creds.tokens is not None:
            refreshed = await auth.refresh_token(creds.tokens.refreshToken)
            if _has_tokens(refreshed):
                access_token = refreshed["AuthenticationResult"]["AccessToken"]
        if access_token is None:
            result = await device_login(creds.username, creds.password, creds.device_data)
            access_token = result["AuthenticationResult"]["AccessToken"]
        await auth.forget_device(access_token, creds.device_data.key)
    except Exception as err:  # deliberately broad: logout must not crash
        _LOGGER.warning("Couldn't deregister device: %s: %s", type(err).__name__, err)
        return False
    return True


class ManagedHive(Hive):  # type: ignore[misc]
    """Hive session with a working unattended re-login and token persistence."""

    def __init__(
        self,
        creds: Credentials,
        on_new_tokens: Callable[[Tokens], None] | None = None,
    ) -> None:
        if creds.device_data is None:
            raise ValueError("ManagedHive needs stored device data; run `hive-mcp login`.")
        super().__init__(username=creds.username, password=creds.password)
        self._creds = creds
        self._device: DeviceData = creds.device_data
        self._on_new_tokens = on_new_tokens
        self.relogin_count = 0

    async def login(self) -> AuthResult:
        """Replaces Session.login(); called by the library's _retryLogin()."""
        _LOGGER.info("Re-authenticating with Hive using device keys.")
        try:
            result = await device_login(self._creds.username, self._creds.password, self._device)
        except ReauthRequired as err:
            raise hive_exceptions.HiveReauthRequired(str(err)) from err
        await self.updateTokens(result)
        self.relogin_count += 1
        return result

    async def _retryLogin(self) -> None:  # noqa: N802 - overrides the library's name
        """Replaces Session._retryLogin(); see bug 2 in the module docstring."""
        last_err: Exception | None = None
        for delay_s in (0, 5, 10):
            if delay_s:
                _LOGGER.info("Retrying Hive login in %s seconds.", delay_s)
                await asyncio.sleep(delay_s)
            try:
                await self.login()
                return
            except hive_exceptions.HiveApiError as err:
                _LOGGER.warning("Hive login attempt failed: %s", err)
                last_err = err
        raise hive_exceptions.HiveReauthRequired("Hive login kept failing.") from last_err

    async def close(self) -> None:
        """Close the aiohttp session the library opens for API calls."""
        websession = getattr(self.api, "websession", None)
        if websession is not None and not websession.closed:
            await websession.close()

    async def updateTokens(  # noqa: N802 - overrides the library's name
        self, tokens: dict[str, Any], update_expiry_time: bool = True
    ) -> Any:
        previous = self.tokens.tokenData.get("refreshToken")
        out = await super().updateTokens(tokens, update_expiry_time)
        # Only fresh Cognito results are worth saving; a dict in our own
        # {"token", ...} shape is what we loaded from the keychain.
        current = self.tokens.tokenData.get("refreshToken")
        is_new = "AuthenticationResult" in tokens and current and current != previous
        if is_new and self._on_new_tokens is not None:
            self._on_new_tokens(self.current_tokens())
        return out

    def current_tokens(self) -> Tokens:
        td = self.tokens.tokenData
        return Tokens(
            token=td["token"], refreshToken=td["refreshToken"], accessToken=td["accessToken"]
        )

    async def start(self) -> dict[str, list[dict[str, Any]]]:
        """Start the session from stored tokens, logging in first if there are none."""
        tokens: dict[str, Any]
        if self._creds.tokens is not None:
            tokens = self._creds.tokens.as_session_config()
        else:
            tokens = await self.login()
        try:
            device_list: dict[str, list[dict[str, Any]]] = await self.startSession(
                {
                    "username": self._creds.username,
                    "password": self._creds.password,
                    "device_data": list(self._device),
                    "tokens": tokens,
                }
            )
        except (
            hive_exceptions.HiveReauthRequired,
            hive_exceptions.HiveInvalidDeviceAuthentication,
        ) as err:
            raise ReauthRequired(
                str(err) or "Hive session couldn't be started; sign in again."
            ) from err
        return device_list
