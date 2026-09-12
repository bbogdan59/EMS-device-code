import argparse
import fcntl
import logging
import os
from pathlib import Path
import random
import signal
import threading
import time
import tomllib
import httpx
from .agent import Agent
from .api import API
from .readers import DisabledReader, ModbusReader, Simulator
from .provisioning import accept_enrollment_response, enrollment_payload
from .state import State

log = logging.getLogger("ems_device")


def _retry_delay(response, failures):
    retry_after = response.headers.get("Retry-After") if response is not None else None
    if retry_after and retry_after.isdigit():
        return max(5, min(300, int(retry_after)))
    return min(300, 2 ** min(failures, 8)) + random.random()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("action", choices=["identity", "provision", "run"])
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = tomllib.loads(args.config.read_text())
    path = Path(settings["state_dir"])
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Held across enrollment and run: no concurrent claims, queue consumers or serial masters.
    lock = (path / "agent.lock").open("a")
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
        api = API(settings["platform_url"], state.get("credentials"))
        bound_origin = state.get("platform_origin")
        if bound_origin and bound_origin != api.origin:
            raise ValueError("State is bound to another platform; credentials will not be sent")
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
        else:
            raise ValueError("reader must be disabled, simulator or modbus")
        interval = settings.get("sample_seconds", 10)
        if type(interval) not in (int, float) or not 5 <= interval <= 3600:
            raise ValueError("sample_seconds must be 5..3600")
        agent = Agent(state, api, reader)
        # Fetch cloud policy before the first serial transaction. A cached policy
        # permits read-only monitoring during an outage; it never enables writes.
        try:
            agent.sync()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403) or not state.get("station_config"):
                raise
            log.warning("startup_offline: using cached policy for read-only monitoring")
        except Exception:
            if not state.get("station_config"):
                raise
            log.warning("startup_offline: using cached policy for read-only monitoring")
        next_sync = next_upload = 0.0
        failures = 0
        while not stop.is_set():
            now = time.monotonic()
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
                        raise ValueError("Credential inactive: operator action required") from None
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
