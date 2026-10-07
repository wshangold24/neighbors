"""Secrets (API keys, tokens) live in the operating system's credential store:
macOS Keychain, Windows Credential Manager, or the Linux Secret Service
(GNOME Keyring / KWallet), via the `keyring` library. They are never written
to a plain file.

Lookup order everywhere: an explicit value (e.g. a CLI flag), then the
environment variable, then the credential store. On a machine with no
credential store (a headless server, CI), use the environment variable.
"""

from __future__ import annotations

import os

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

SERVICE = "neighbors"

# account names under the "neighbors" service, paired with their env var
RENTCAST = "rentcast-api-key"
NYC_APP_TOKEN = "nyc-app-token"
ENV_VARS = {RENTCAST: "RENTCAST_API_KEY", NYC_APP_TOKEN: "NYC_APP_TOKEN"}


class KeystoreUnavailable(RuntimeError):
    """No usable credential store on this machine."""


def resolve(name: str, explicit: str | None = None) -> str | None:
    """First non-blank of: `explicit`, the env var, the credential store."""
    for value in (explicit, os.environ.get(ENV_VARS[name])):
        if value and value.strip():
            return value.strip()
    try:
        stored = keyring.get_password(SERVICE, name)
    except KeyringError:          # no backend / locked / denied → just "not set"
        return None
    return stored.strip() if stored and stored.strip() else None


def save(name: str, value: str) -> None:
    """Store `value`, or remove the entry when it's blank."""
    value = (value or "").strip()
    try:
        if value:
            keyring.set_password(SERVICE, name, value)
        else:
            try:
                keyring.delete_password(SERVICE, name)
            except PasswordDeleteError:
                pass                  # nothing stored — already removed
    except KeyringError as e:
        raise KeystoreUnavailable(
            f"no secure credential store is available on this system ({e}). "
            f"Set the {ENV_VARS[name]} environment variable instead.") from e


def store_name() -> str:
    """Human-readable name of the active credential store, for messages."""
    backend = type(keyring.get_keyring()).__module__
    for marker, label in (("macOS", "macOS Keychain"),
                          ("Windows", "Windows Credential Manager"),
                          ("SecretService", "Secret Service keyring"),
                          ("kwallet", "KWallet"),
                          ("libsecret", "Secret Service keyring")):
        if marker.lower() in backend.lower():
            return label
    return "system credential store"
