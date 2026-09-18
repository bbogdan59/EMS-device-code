import argparse
import fcntl
import json
import logging
import os
from pathlib import Path
import random
import signal
import threading
import time
import tomllib
import httpx
from . import __version__
from . import system_stats
from .agent import Agent
from .api import API
from .log_buffer import CompactLogBuffer
from .readers import DisabledReader, ModbusReader, Simulator
from .provisioning import accept_enrollment_response, enrollment_payload
from .state import State

log = logging.getLogger("ems_device")


class CredentialInactiveError(Exception):
    """Distinct type so `type(exc).__name__` alone (the only thing logged --
    see the security note by the outer handler) is enough for an operator to
    grep journalctl and find docs/PROTOCOL.md's recovery steps, without the
    log ever needing to carry exception text/response bodies."""


def _clock_sync_status(marker=Path("/run/systemd/timesync/synchronized")):
    """Conservator: absenta markerului nu este echivalenta cu ceas nesincronizat."""
    return "synchronized" if marker.exists() else "unknown"


def _retry_delay(response, failures):
    retry_after = response.headers.get("Retry-After") if response is not None else None
    if retry_after and retry_after.isdigit():
        return max(5, min(300, int(retry_after)))
    return min(300, 2 ** min(failures, 8)) + random.random()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "action", choices=["dead-letter", "health", "identity", "provision", "reset", "rotate-credential", "run"]
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--factory", action="store_true",
                        help="with 'reset': also issue a brand new device identity (repurposing hardware)")
    parser.add_argument("--confirm-serial",
                        help="required for 'reset': must exactly match the serial printed by 'identity'")
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # Compact WARNING+/ERROR+ buffer for the platform's per-device debug log
    # (see log_buffer.py) -- attached to the whole logger so any log.warning/
    # log.error call site below is captured without threading it through
    # explicitly. Losable: never a substitute for `journalctl` on the unit.
    log_buffer = CompactLogBuffer()
    log.addHandler(log_buffer)
    settings = tomllib.loads(args.config.read_text())
    path = Path(settings["state_dir"])
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Held across enrollment and run: no concurrent claims, queue consumers or serial masters.
    lock = (path / "agent.lock").open("a")
    if args.action not in ("dead-letter", "health", "identity"):
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another agent/enrollment process uses this state directory")
    state = State(path)
    api = reader = None
    try:
        if args.action == "identity":
            identity = state.identity()
            print(f"serial_number={identity['serial_number']}")
            print(f"installation_uuid={identity['installation_uuid']}")
            print(f"enrollment_status={state.get('enrollment_status') or 'new'}")
            return
        if args.action == "health":
            snapshot = state.health_snapshot(agent_version=__version__, clock_sync=_clock_sync_status())
            snapshot["system_stats"] = system_stats.collect()
            print(json.dumps(snapshot, sort_keys=True))
            return
        if args.action == "dead-letter":
            print(json.dumps(state.dead_letters(), sort_keys=True))
            return
        if args.action == "reset":
            # Purely local: no server-initiated revoke/transfer/factory-reset
            # ever reaches the device directly, only a 401 on its NEXT
            # authenticated call. This is the operator's explicit recovery
            # action after that 401, after a transfer, or before repurposing
            # hardware for a different customer -- never triggered
            # automatically. See docs/PROTOCOL.md.
            identity = state.identity()
            if args.confirm_serial != identity["serial_number"]:
                raise SystemExit(
                    "Refused: --confirm-serial must exactly match the serial printed by 'identity' "
                    f"({identity['serial_number']!r}). This releases the current station assignment"
                    + (" and issues a brand new device identity." if args.factory else ".")
                )
            if args.factory:
                state.factory_reset()
                identity = state.identity()
                print(f"Factory reset complete. New serial: {identity['serial_number']}")
            else:
                state.clear_assignment()
                identity = state.identity()
                print(f"Assignment cleared. Serial unchanged: {identity['serial_number']}")
            print(f"New device code (keep sealed until next setup): {identity['activation_code']}")
            return
        api = API(settings["platform_url"], state.get("credentials"))
        bound_origin = state.get("platform_origin")
        if bound_origin and bound_origin != api.origin:
            raise ValueError("State is bound to another platform; credentials will not be sent")
        if args.action == "rotate-credential":
            if not state.get("credentials"):
                raise SystemExit("No active credentials to rotate; enroll first")
            # Set BEFORE the network call: if the response never arrives (crash,
            # network drop after the server already processed it), this flag
            # survives to the next run/health check instead of silently
            # vanishing with the failed request.
            state.set("credential_rotation_pending", True)
            response = api.rotate_credential()
            new_secret = response.get("credential_secret")
            if not isinstance(new_secret, str) or not new_secret:
                raise SystemExit(
                    "Rotation response missing credential_secret; treating as failed. "
                    "Do not retry blindly -- check 'ems-device health' (credential_rotation_pending) "
                    "and if heartbeats start failing with 401, run 'ems-device reset'."
                )
            credentials = dict(state.get("credentials"))
            credentials["credential_secret"] = new_secret
            state.set("credentials", credentials)
            state.set("credential_rotation_pending", False)
            print("Credential rotated.")
            return
        if args.action == "provision":
            identity = state.identity()
            state.set("platform_origin", api.origin)
            if state.get("credentials"):
                status = "assigned"
                state.set("activation_code", None)
                identity = state.identity()
            else:
                try:
                    status = accept_enrollment_response(state, api.enroll(enrollment_payload(identity, settings)))
                except httpx.HTTPError:
                    # Factory provisioning remains useful during a platform
                    # outage. The systemd service retries enrollment later.
                    status = "created-locally; enrollment will retry"
            print(f"Serial: {identity['serial_number']}")
            if identity.get("activation_code"):
                print(f"Device code (keep sealed until customer setup): {identity['activation_code']}")
            print(f"Enrollment: {status}")
            return

        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        enrollment_failures = 0
        while not state.get("credentials") and not stop.is_set():
            try:
                state.set("platform_origin", api.origin)
                status = accept_enrollment_response(
                    state, api.enroll(enrollment_payload(state.identity(), settings))
                )
                if status == "assigned":
                    api.credentials = state.get("credentials")
                    break
                enrollment_failures = 0
                delay = 30
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (401, 403, 409):
                    raise ValueError("Enrollment rejected: operator action required") from None
                enrollment_failures += 1
                delay = _retry_delay(exc.response, enrollment_failures)
                log.warning("enrollment_http_error status=%s", exc.response.status_code)
            except httpx.HTTPError:
                enrollment_failures += 1
                delay = _retry_delay(None, enrollment_failures)
                log.warning("enrollment_offline")
            if args.once:
                return
            stop.wait(delay)
        if stop.is_set():
            return
        mode = settings["reader"]
        if mode == "disabled":
            reader = DisabledReader()
        elif mode == "simulator":
            if settings.get("allow_simulated_upload") is not True:
                raise ValueError("Simulator upload needs explicit allow_simulated_upload=true on a demo station")
            reader = Simulator()
        elif mode == "modbus":
            reader = ModbusReader(settings["modbus"])
            # O singura citire de verificare, inainte de bucla periodica --
            # refuza devreme un profil incompatibil (vezi ModbusReader.check_ready).
            reader.check_ready()
        else:
            raise ValueError("reader must be disabled, simulator or modbus")
        interval = settings.get("sample_seconds", 10)
        if type(interval) not in (int, float) or not 5 <= interval <= 3600:
            raise ValueError("sample_seconds must be 5..3600")
        agent = Agent(state, api, reader, log_buffer)
        # Fetch cloud policy before the first serial transaction. A cached policy
        # permits read-only monitoring during an outage; it never enables writes.
        try:
            agent.sync()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                raise CredentialInactiveError("credential_inactive") from None
            if not state.get("station_config"):
                raise
            log.warning("startup_offline: using cached policy for read-only monitoring")
        except Exception:
            if not state.get("station_config"):
                raise
            log.warning("startup_offline: using cached policy for read-only monitoring")
        next_sync = next_upload = next_log_upload = 0.0
        failures = 0
        while not stop.is_set():
            now = time.monotonic()
            if now >= next_log_upload:
                agent.upload_logs()  # best-effort, never raises (see Agent.upload_logs)
                next_log_upload = time.monotonic() + 60
            # Local sampling continues when cloud is offline; only network retries back off.
            if reader.telemetry_available:
                try:
                    agent.sample()
                except Exception as exc:
                    log.warning("sample_failed type=%s", type(exc).__name__)
            if now >= next_upload:
                try:
                    if now >= next_sync:
                        agent.sync()
                        next_sync = time.monotonic() + 60
                    agent.upload()
                    failures = 0
                    next_upload = time.monotonic() + interval
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code in (401, 403):
                        # Server-side revoke/transfer/factory-reset never reaches this
                        # device directly -- this 401/403 is the only signal. Recovery
                        # is `ems-device reset` (see docs/PROTOCOL.md "Recuperare dupa
                        # revocare/transfer"). The outer handler only ever logs the
                        # exception TYPE name, never its message (no token/response
                        # body in logs) -- so the guidance lives in docs, not here.
                        raise CredentialInactiveError("credential_inactive") from None
                    failures += 1
                    next_upload = time.monotonic() + min(300, 2 ** min(failures, 8)) + random.random()
                    log.warning("cloud_http_error status=%s", exc.response.status_code)
                except Exception as exc:
                    failures += 1
                    next_upload = time.monotonic() + min(300, 2 ** min(failures, 8)) + random.random()
                    log.warning("cloud_failed type=%s", type(exc).__name__)
            if args.once:
                break
            stop.wait(interval)
    except Exception as exc:
        # No exception body/HTTP response/token in logs.
        log.error("agent_stopped type=%s; check configuration and platform state", type(exc).__name__)
        raise SystemExit(1) from None
    finally:
        if reader:
            reader.close()
        if api:
            api.close()
        state.close()
        lock.close()
