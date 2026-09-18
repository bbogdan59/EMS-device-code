"""Factory provisioning and enrollment helpers.

The public serial is inventory metadata.  The activation code printed inside
the package and the provisioning secret kept on the device are separate
secrets; neither is derived from Raspberry Pi hardware identifiers.
"""

from __future__ import annotations

import base64
import secrets
import uuid


def new_serial() -> str:
    """Return a human-readable, random 80-bit inventory serial."""
    encoded = base64.b32encode(secrets.token_bytes(10)).decode("ascii").rstrip("=")
    return "EMS-" + "-".join(encoded[index : index + 4] for index in range(0, len(encoded), 4))


def new_activation_code() -> str:
    """Return the sealed-package bearer code used by the final customer."""
    encoded = base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=")
    return "ACT-" + "-".join(encoded[index : index + 5] for index in range(0, len(encoded), 5))


def new_identity() -> dict[str, str]:
    return {
        "installation_uuid": str(uuid.uuid4()),
        "serial_number": new_serial(),
        "activation_code": new_activation_code(),
        "provisioning_secret": secrets.token_urlsafe(32),
    }


def enrollment_payload(identity: dict[str, str], settings: dict) -> dict:
    hardware_info = {
        "device_name": settings.get("device_name", "EMS edge"),
        "platform": settings.get("hardware_platform", "raspberry-pi"),
    }
    configured_serial = settings.get("hardware_serial")
    if configured_serial:
        hardware_info["hardware_serial"] = configured_serial
    return {
        "installation_uuid": identity["installation_uuid"],
        "provisioning_secret": identity["provisioning_secret"],
        "serial_number": identity["serial_number"],
        "activation_code": identity["activation_code"],
        "hardware_info": hardware_info,
    }


def accept_enrollment_response(state, response: dict) -> str:
    """Validate and persist an enrollment response; return its state."""
    status = response.get("status")
    if status == "pending":
        state.set("enrollment_status", "pending")
        return status
    if status == "revoked":
        state.set("enrollment_status", "revoked")
        raise ValueError("Device enrollment revoked: operator action required")
    if status != "assigned":
        raise ValueError("Invalid enrollment status")

    try:
        device_id = str(uuid.UUID(str(response["device_id"])))
        station_id = str(uuid.UUID(str(response["station_id"])))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid assigned enrollment response") from exc
    existing = state.get("credentials")
    if existing and (existing.get("device_id") != device_id or existing.get("station_id") != station_id):
        # Defense in depth against a race/replay handing this device a
        # DIFFERENT station's assignment (issue #3 "two accounts" case) --
        # in practice this path is never reached in normal operation (both
        # `run` and `provision` only call enroll() while no credentials are
        # stored yet), but accept_enrollment_response must never silently
        # switch tenants if it ever is.
        raise ValueError("assignment_identity_mismatch: refusing to overwrite an existing different assignment")
    secret = response.get("credential_secret")
    if not isinstance(secret, str) or not secret:
        # Once the bootstrap credential was used, the server intentionally no
        # longer returns it. A device that already persisted it is fine.
        if existing and existing.get("device_id") == device_id and existing.get("station_id") == station_id:
            return "assigned"
        raise ValueError("Assigned enrollment response has no bootstrap credential")

    state.set(
        "credentials",
        {"device_id": device_id, "station_id": station_id, "credential_secret": secret},
    )
    state.set("enrollment_status", "assigned")
    # The customer activation code is one-use. Remove the local copy after a
    # successful assignment; the provisioning secret remains for lifecycle
    # recovery and must never be printed.
    state.set("activation_code", None)
    return "assigned"
