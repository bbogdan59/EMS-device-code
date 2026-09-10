import argparse
import fcntl
import getpass
import logging
import os
from pathlib import Path
import random
import signal
import threading
import time
import tomllib
import uuid
import httpx
from .agent import Agent
from .api import API
from .readers import ModbusReader, Simulator
from .state import State

log = logging.getLogger("ems_device")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("action", choices=["identity", "enroll", "run"])
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
            print(state.get("identity"))
            return
        api = API(settings["platform_url"], state.get("credentials"))
        bound_origin = state.get("platform_origin")
        if bound_origin and bound_origin != api.origin:
            raise ValueError("State is bound to another platform; credentials will not be sent")
        if args.action == "enroll":
            if state.get("credentials"):
                raise ValueError("Already enrolled; use platform revocation/reprovisioning procedure")
            if state.get("enrollment_pending"):
                raise ValueError("Previous claim outcome unknown: reconcile in platform before retry")
            code = getpass.getpass("Station claim code (hidden): ")
            state.set("platform_origin", api.origin)
            state.set("enrollment_pending", True)
            data = api.call("POST", "/devices/claim", {
                "claim_code": code, "device_name": settings.get("device_name", "EMS edge"),
                "hardware_info": {"agent_identity": state.get("identity"),
                                  "serial": settings.get("hardware_serial", "unspecified")}}, authenticated=False)
            for key in ("device_id", "station_id"):
                uuid.UUID(data[key])
            if not isinstance(data.get("credential_secret"), str) or not data["credential_secret"]:
                raise ValueError("Invalid claim response")
            state.set("credentials", {key: data[key] for key in ("device_id", "station_id", "credential_secret")})
            state.set("enrollment_pending", False)
            print("Enrolled; credentials stored locally. Start the service.")
            return
        if not api.credentials:
            raise ValueError("Enroll before running")
        mode = settings["reader"]
        if mode == "simulator":
            if settings.get("allow_simulated_upload") is not True:
                raise ValueError("Simulator upload needs explicit allow_simulated_upload=true on a demo station")
            reader = Simulator()
        elif mode == "modbus":
            reader = ModbusReader(settings["modbus"])
        else:
            raise ValueError("reader must be simulator or modbus")
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
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        next_sync = next_upload = 0.0
        failures = 0
        while not stop.is_set():
            now = time.monotonic()
            # Local sampling continues when cloud is offline; only network retries back off.
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
