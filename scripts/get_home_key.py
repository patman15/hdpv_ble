"""Extract PowerView Gen3 homekeys via the Hunter Douglas cloud account.

Reproduces what the Android PowerView app (com.hunterdouglas.powerview 3.8.1)
does to obtain each home's homekey:

    1. POST /api/v5/users/rcUserSignIn        -> pvkey
    2. GET  /api/v5/homes                     -> documentId per home
    3. GET  /api/v5/firebaseAuth/userToken    -> Firebase custom token
    4. Identity Toolkit signInWithCustomToken -> Firebase idToken
    5. Firestore REST: homes/{documentId}     -> home.key (the homekey)

Companion to extract_gateway3_homekey.py — that script reads the homekey from
a Gen3 gateway over the local network; this script reads it from the cloud,
which works for any home on the account whether the gateway is reachable or not.
"""

import base64
import getpass
import hashlib
from typing import Any, Final
import urllib.parse

import requests

RC_API: Final[str] = "https://homeauto.hunterdouglas.com"
FIREBASE_API_KEY: Final[str] = "AIzaSyAIfkw7TAweHwjikdahH1ZqbL7lxF6yAxQ"
FIREBASE_PROJECT: Final[str] = "powerblue-861ad"
NO_KEY_SENTINEL: Final[str] = "00112233445566778899AABBCCDDEEFF"
TIMEOUT: Final[int] = 20

# Identity values matching what the Android PowerView 3.8.1 'hd' release sends.
# Faking these makes us look like a normal app instance to Hunter Douglas;
# inventing 'python-client' style values would stand out in any server-side
# log/filter. See BuildConfig.java, BrandConfig.getAppBrandName(),
# PowerViewApplication.getUserAgentString(), SignInViewModel.doSignIn().
APP_VERSION: Final[str] = "3.8.1"
APP_BUILD: Final[str] = "7911"
APP_BRAND: Final[str] = "HD"  # BrandConfig flavor 'hd' -> "HD"
DEVICE_NAME: Final[str] = "Google Pixel 8"  # Build.MANUFACTURER + " " + Build.MODEL
LANGUAGE: Final[str] = "en"  # Locale.getDefault().getLanguage()
REGION: Final[str] = "US"  # Locale.getDefault().getCountry()
LOCALE: Final[str] = f"{LANGUAGE}_{REGION}"  # Locale.getDefault().toString()
USER_AGENT: Final[str] = f"PowerView/{APP_VERSION} {APP_BUILD} ({DEVICE_NAME} {LOCALE})"


def device_app_id(email: str) -> str:
    """Derive a stable, ANDROID_ID-shaped deviceAppId from the account email.

    The app builds this as `Settings.Secure.ANDROID_ID + "hdrelease"`, capped
    at 64 chars (BrandConfig.appDeviceId). ANDROID_ID is a 16-hex-char string.
    Hashing the email gives a value that's stable across runs for this account
    so we don't look like a brand new device on every login.
    """
    android_id: str = hashlib.sha256(email.encode()).hexdigest()[:16]
    return f"{android_id}hdrelease"


def make_session() -> requests.Session:
    """Build a requests.Session with the User-Agent the Android app sends."""
    session: requests.Session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    return session


def sign_in(session: requests.Session, email: str, password: str) -> str:
    """Sign in to the PowerView account API and return the pvkey token."""
    body: Final[dict[str, dict[str, str]]] = {
        "user": {
            "email": email,
            "password": password,
            "deviceAppBrand": APP_BRAND,
            "deviceAppId": device_app_id(email),
            "deviceAppVersion": APP_VERSION,
            "deviceName": DEVICE_NAME,
            "deviceType": DEVICE_NAME,
            "language": LANGUAGE,
            "region": REGION,
        }
    }
    resp: requests.Response = session.post(
        f"{RC_API}/api/v5/users/rcUserSignIn", json=body, timeout=TIMEOUT
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    if data.get("error"):
        raise RuntimeError(f"Sign-in failed: {data['error']}")
    pvkey: str = data["pvkey"]
    return pvkey


def basic_auth_header(email: str, pvkey: str) -> dict[str, str]:
    """Build the Basic auth header that the app uses on every cloud request."""
    token: Final[str] = base64.b64encode(f"{email}:{pvkey}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def list_homes(session: requests.Session, auth: dict[str, str]) -> list[dict[str, Any]]:
    """Return the list of homes (RHome objects) on this account."""
    resp: requests.Response = session.get(
        f"{RC_API}/api/v5/homes", headers=auth, timeout=TIMEOUT
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    homes: list[dict[str, Any]] = data.get("homes", [])
    return homes


def firebase_custom_token(session: requests.Session, auth: dict[str, str]) -> str:
    """Trade the pvkey for a single-use Firebase custom token."""
    resp: requests.Response = session.get(
        f"{RC_API}/api/v5/firebaseAuth/userToken", headers=auth, timeout=TIMEOUT
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    token: str = data["token"]
    return token


def firebase_id_token(custom_token: str) -> str:
    """Exchange a Firebase custom token for a long-lived idToken."""
    url: Final[str] = (
        "https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken"
        f"?key={FIREBASE_API_KEY}"
    )
    resp: requests.Response = requests.post(
        url,
        json={"token": custom_token, "returnSecureToken": True},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    id_token: str = data["idToken"]
    return id_token


def firestore_get_home(id_token: str, document_id: str) -> dict[str, Any]:
    """Read the homes/{documentId} document from Firestore."""
    doc_path: Final[str] = f"homes/{urllib.parse.quote(document_id, safe='')}"
    url: Final[str] = (
        f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}"
        f"/databases/(default)/documents/{doc_path}"
    )
    resp: requests.Response = requests.get(
        url, headers={"Authorization": f"Bearer {id_token}"}, timeout=TIMEOUT
    )
    resp.raise_for_status()
    doc: dict[str, Any] = resp.json()
    return doc


def extract_home_key(doc: dict[str, Any]) -> str | None:
    """Pull the homekey out of a Firestore home document, or None if not set.

    The Firestore document mirrors FSHomeDoc in the Android app; the homekey
    is the `home.key` string field. The app treats both the empty string and
    the sentinel '00112233445566778899AABBCCDDEEFF' as 'no key set'.
    """
    fields: dict[str, Any] = doc.get("fields", {})
    home: dict[str, Any] = fields.get("home", {}).get("mapValue", {}).get("fields", {})
    key: str | None = home.get("key", {}).get("stringValue")
    if not key or key == NO_KEY_SENTINEL:
        return None
    return key


def home_display_name(home: dict[str, Any]) -> str:
    """Pick the most user-friendly name for a home from an RHome record."""
    return (
        home.get("name")
        or home.get("gen3DisplayName")
        or home.get("gen2DisplayName")
        or "(unnamed)"
    )


def main(email: str, password: str | None) -> int:
    """Sign in, then print the homekey for every home on the account."""
    pwd: Final[str] = password or getpass.getpass(f"Password for {email}: ")
    session: Final[requests.Session] = make_session()

    print("Signing in...")
    pvkey: Final[str] = sign_in(session, email, pwd)
    auth: Final[dict[str, str]] = basic_auth_header(email, pvkey)

    homes: Final[list[dict[str, Any]]] = list_homes(session, auth)
    if not homes:
        print("No homes are associated with this account.")
        return 1

    print("Authenticating to Firebase...")
    id_token: Final[str] = firebase_id_token(firebase_custom_token(session, auth))

    print(f"Found {len(homes)} home(s), interrogating")
    for home in homes:
        name: str = home_display_name(home)
        doc_id: str | None = home.get("documentId")
        role: str = home.get("role") or "?"
        hub_id: str = home.get("hubId") or "-"

        print(f"Home '{name}':")
        print(f"\trole: {role}")
        print(f"\thub id: {hub_id}")
        print(f"\tdocument id: {doc_id or '(none — Gen2-only home, no Gen3 homekey)'}")

        if not doc_id:
            continue

        try:
            doc: dict[str, Any] = firestore_get_home(id_token, doc_id)
        except requests.HTTPError as ex:
            print(
                f"\tHomeKey: error — Firestore returned HTTP {ex.response.status_code}"
            )
            continue

        key: str | None = extract_home_key(doc)
        if key is None:
            print("\tHomeKey: not set (this home was created without encryption)")
        else:
            print(f"\tHomeKey: {key.lower()}")

    return 0


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Extract PowerView Gen3 homekeys from a Hunter Douglas account",
    )
    parser.add_argument("-e", "--email", required=True, help="account email")
    parser.add_argument(
        "-p", "--password", default=None, help="account password (prompted if omitted)"
    )
    args = parser.parse_args()
    sys.exit(main(**vars(args)))
