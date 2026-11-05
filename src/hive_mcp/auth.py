"""Hive authentication: interactive first login, unattended device re-login.

ManagedHive hooks into pyhive-integration 2.0's session by overriding two of its
snake_case methods. The library calls these internally; its camelCase names are
only aliases for outside callers, so overriding those would silently do nothing.

- login(): the library's _retry_login() calls it when a token refresh fails.
  Ours does a plain device-SRP login with our stored device keys. In 1.0.10 the
  library's own login() couldn't re-login a fresh process (it asked Cognito
  whether the device was "remembered" using an access token it didn't have);
  2.0 appears to fix that, but ours is kept because it doesn't depend on the
  session's Auth holding the device keys, and it counts re-logins.
- update_tokens(): every new set of tokens passes through here, so this is
  where fresh Cognito tokens are saved to the keychain.

1.0.10 also deadlocked when the refresh token expired (hiveRefreshTokens held
its lock while _retryLogin called back into it). 2.0's _retry_login doesn't
refresh again, so that override is gone; the test for it still runs.
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
    # 2.0 returns (group_key, key, password, token_created). The timestamp isn't
    # kept: without it the session refreshes once at start, which is harmless.
    group_key, key, device_password, *_ = await auth.get_device_data()
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
        """Replaces the library's login(); called by its _retry_login()."""
        _LOGGER.info("Re-authenticating with Hive using device keys.")
        try:
            result = await device_login(self._creds.username, self._creds.password, self._device)
        except ReauthRequired as err:
            raise hive_exceptions.HiveReauthRequired(str(err)) from err
        await self.update_tokens(result)
        self.relogin_count += 1
        return result

    async def update_tokens(self, tokens: dict[str, Any], update_expiry_time: bool = True) -> Any:
        previous = self.tokens.token_data.get("refreshToken")
        out = await super().update_tokens(tokens, update_expiry_time)
        # Only fresh Cognito results are worth saving; a dict in our own
        # {"token", ...} shape is what we loaded from the keychain.
        current = self.tokens.token_data.get("refreshToken")
        is_new = "AuthenticationResult" in tokens and current and current != previous
        if is_new and self._on_new_tokens is not None:
            self._on_new_tokens(self.current_tokens())
        return out

    def current_tokens(self) -> Tokens:
        td = self.tokens.token_data
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
            device_list: dict[str, list[dict[str, Any]]] = await self.start_session(
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
