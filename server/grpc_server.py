"""
gRPC server for coordinating an NI SMU (nidcpower) and an NI Digital Pattern
Instrument (nidigital).

Workflow
--------
1. Client calls ConfigureSMU  -> opens & configures nidcpower session
2. Client calls ConfigureDigital -> opens nidigital session, loads pin-map /
   pattern / levels / timing files
3. Client calls ConfigureHRAM -> sets HRAM trigger type and sample limits
4. Client calls RunTest ->
      a. Enables SMU output
      b. Bursts the pattern and waits for completion
      c. Fetches HRAM failure data from every site
      d. Writes all results to a TDMS file
      e. Returns a RunTestResponse with per-site pass/fail + failure details
5. Client may call AbortTest at any time to stop a running burst
6. Client calls Shutdown to close both instrument sessions cleanly

Run
---
    python grpc_server.py [--host 0.0.0.0] [--port 50051]
"""

import argparse
import logging
import os
import sys
import threading
from concurrent import futures
from datetime import datetime

import grpc

# ---------------------------------------------------------------------------
# Add the generated-stubs directory to the path
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_GENERATED = os.path.join(_HERE, "..", "generated")
sys.path.insert(0, _GENERATED)

try:
    import instrument_test_pb2 as pb2
    import instrument_test_pb2_grpc as pb2_grpc
except ModuleNotFoundError:
    print(
        "[ERROR] Generated gRPC stubs not found in 'generated/'.\n"
        "        Run generate_stubs.bat first."
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Optional NI driver imports (server still starts in simulation mode if absent)
# ---------------------------------------------------------------------------
try:
    import nidcpower

    _NIDCPOWER_OK = True
except ImportError:
    _NIDCPOWER_OK = False
    logging.warning("nidcpower not installed – SMU calls will fail unless simulate=True")

try:
    import nidigital

    _NIDIGITAL_OK = True
except ImportError:
    _NIDIGITAL_OK = False
    logging.warning("nidigital not installed – Digital calls will fail unless simulate=True")

try:
    import numpy as np
    from nptdms import ChannelObject, GroupObject, RootObject, TdmsWriter

    _NPTDMS_OK = True
except ImportError:
    _NPTDMS_OK = False
    logging.warning("nptdms/numpy not installed – TDMS logging disabled")

# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("grpc_server")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_HRAM_TRIGGER_MAP = {
    "FIRST_FAILURE": lambda: nidigital.HistoryRAMTriggerType.FIRST_FAILURE
    if _NIDIGITAL_OK
    else None,
    "CYCLE_NUMBER": lambda: nidigital.HistoryRAMTriggerType.CYCLE_NUMBER
    if _NIDIGITAL_OK
    else None,
    "PATTERN_LABEL": lambda: nidigital.HistoryRAMTriggerType.PATTERN_LABEL
    if _NIDIGITAL_OK
    else None,
}

_HRAM_CYCLES_MAP = {
    "FAILED": lambda: nidigital.HistoryRAMCyclesToAcquire.FAILED
    if _NIDIGITAL_OK
    else None,
    "ALL": lambda: nidigital.HistoryRAMCyclesToAcquire.ALL if _NIDIGITAL_OK else None,
}


def _ok(msg="OK") -> pb2.StatusResponse:
    return pb2.StatusResponse(success=True, message=msg)


def _err(msg) -> pb2.StatusResponse:
    log.error(msg)
    return pb2.StatusResponse(success=False, message=str(msg))


# ---------------------------------------------------------------------------
# Service Implementation
# ---------------------------------------------------------------------------
class InstrumentTestServicer(pb2_grpc.InstrumentTestServiceServicer):
    """Implements InstrumentTestService defined in instrument_test.proto."""

    def __init__(self):
        self._lock = threading.Lock()

        # nidcpower session and config state
        self._smu_session = None
        self._smu_config: pb2.SMUConfig | None = None

        # nidigital session and config state
        self._dig_session = None
        self._dig_config: pb2.DigitalConfig | None = None

        # HRAM config state
        self._hram_config: pb2.HRAMConfig | None = None

        # Abort flag for burst
        self._abort_event = threading.Event()

    # =======================================================================
    # RPC: ConfigureSMU
    # =======================================================================
    def ConfigureSMU(self, request: pb2.SMUConfig, context):
        log.info("ConfigureSMU: resource=%s channel=%s V=%.4f A_lim=%.4f",
                 request.resource_name, request.channel,
                 request.voltage_level, request.current_limit)
        try:
            with self._lock:
                self._close_smu()
                self._smu_config = request

                if not _NIDCPOWER_OK:
                    return _err("nidcpower package not installed on server")

                opts = (
                    "Simulate=1, DriverSetup=Model:4162;BoardType:PXI"
                    if request.simulate
                    else ""
                )

                channel = request.channel or "0"
                session = nidcpower.Session(
                    resource_name=request.resource_name,
                    channels=channel,
                    options=opts,
                )

                # Output function
                if request.output_function == "DC_CURRENT":
                    session.channels[channel].output_function = (
                        nidcpower.OutputFunction.DC_CURRENT
                    )
                    session.channels[channel].current_level = request.voltage_level
                    if request.current_limit_range:
                        session.channels[channel].voltage_limit_range = (
                            request.current_limit_range
                        )
                else:
                    session.channels[channel].output_function = (
                        nidcpower.OutputFunction.DC_VOLTAGE
                    )
                    session.channels[channel].voltage_level = request.voltage_level
                    if request.voltage_level_range:
                        session.channels[channel].voltage_level_range = (
                            request.voltage_level_range
                        )

                session.channels[channel].current_limit = request.current_limit
                if request.current_limit_range:
                    session.channels[channel].current_limit_range = (
                        request.current_limit_range
                    )

                # Sense mode
                sense_str = (request.sense or "LOCAL").upper()
                session.channels[channel].sense = (
                    nidcpower.Sense.REMOTE
                    if sense_str == "REMOTE"
                    else nidcpower.Sense.LOCAL
                )

                # Source delay
                if request.source_delay > 0:
                    session.channels[channel].source_delay = request.source_delay

                session.channels[channel].output_enabled = False
                session.commit()

                self._smu_session = session

            return _ok(f"SMU configured: {request.resource_name}/{channel} @ "
                       f"{request.voltage_level:.4f} V, lim {request.current_limit:.6f} A")

        except Exception as exc:  # noqa: BLE001
            return _err(f"ConfigureSMU failed: {exc}")

    # =======================================================================
    # RPC: ConfigureDigital
    # =======================================================================
    def ConfigureDigital(self, request: pb2.DigitalConfig, context):
        log.info("ConfigureDigital: resource=%s pinmap=%s pattern=%s",
                 request.resource_name,
                 os.path.basename(request.pin_map_file),
                 os.path.basename(request.pattern_file))
        try:
            with self._lock:
                self._close_digital()
                self._dig_config = request

                if not _NIDIGITAL_OK:
                    return _err("nidigital package not installed on server")

                opts = (
                    "Simulate=1, DriverSetup=Model:6570"
                    if request.simulate
                    else ""
                )

                session = nidigital.Session(
                    resource_name=request.resource_name, options=opts
                )

                # Load pin map (must come first)
                if request.pin_map_file:
                    session.load_pin_map(request.pin_map_file)

                # Load pattern
                if request.pattern_file:
                    session.load_pattern(request.pattern_file)

                # Load levels + timing
                if request.levels_file or request.timing_file:
                    session.load_specifications_levels_and_timing(
                        specifications_file_paths="",
                        levels_file_paths=request.levels_file or "",
                        timing_file_paths=request.timing_file or "",
                    )
                    if request.levels_file and request.timing_file:
                        session.apply_levels_and_timing(
                            levels_sheet=os.path.splitext(
                                os.path.basename(request.levels_file))[0],
                            timing_sheet=os.path.splitext(
                                os.path.basename(request.timing_file))[0],
                        )

                # Configure active sites
                if request.sites:
                    all_sites = [f"site{s}" for s in request.sites]
                    site_str = ",".join(all_sites)
                    session.sites[site_str].enable_sites()

                self._dig_session = session

            return _ok(f"Digital configured: {request.resource_name}")

        except Exception as exc:  # noqa: BLE001
            return _err(f"ConfigureDigital failed: {exc}")

    # =======================================================================
    # RPC: ConfigureHRAM
    # =======================================================================
    def ConfigureHRAM(self, request: pb2.HRAMConfig, context):
        log.info("ConfigureHRAM: trigger=%s max_samples=%d cycles=%s",
                 request.trigger_type, request.max_samples_per_site,
                 request.cycles_to_acquire)
        try:
            with self._lock:
                self._hram_config = request
                session = self._dig_session

                if session is None:
                    return _err("Digital session not initialized – call ConfigureDigital first")

                trigger_str = (request.trigger_type or "FIRST_FAILURE").upper()
                trigger_fn = _HRAM_TRIGGER_MAP.get(trigger_str)
                if trigger_fn:
                    session.history_ram_trigger_type = trigger_fn()

                cycles_str = (request.cycles_to_acquire or "FAILED").upper()
                cycles_fn = _HRAM_CYCLES_MAP.get(cycles_str)
                if cycles_fn:
                    session.history_ram_cycles_to_acquire = cycles_fn()

                max_samp = request.max_samples_per_site or 8192
                session.history_ram_max_samples_to_acquire_per_site = max_samp

                if request.pretrigger_samples:
                    session.history_ram_pretrigger_samples = request.pretrigger_samples

                session.history_ram_number_of_samples_is_finite = (
                    request.number_of_samples_finite
                )

            return _ok("HRAM configured")
        except Exception as exc:  # noqa: BLE001
            return _err(f"ConfigureHRAM failed: {exc}")

    # =======================================================================
    # RPC: RunTest
    # =======================================================================
    def RunTest(self, request: pb2.RunTestRequest, context):
        log.info("RunTest: pattern=%s tdms=%s",
                 request.start_label or "(from config)", request.tdms_log_file)
        self._abort_event.clear()
        try:
            with self._lock:
                if self._smu_session is None:
                    return pb2.RunTestResponse(
                        success=False,
                        message="SMU not initialized – call ConfigureSMU first",
                    )
                if self._dig_session is None:
                    return pb2.RunTestResponse(
                        success=False,
                        message="Digital not initialized – call ConfigureDigital first",
                    )

                smu = self._smu_session
                dig = self._dig_session
                smu_cfg = self._smu_config
                dig_cfg = self._dig_config

                channel = smu_cfg.channel or "0"
                start_label = (
                    request.start_label
                    or (dig_cfg.start_label if dig_cfg else "")
                    or "new_pattern"
                )
                timeout = request.timeout if request.timeout > 0 else 10.0

                # ---- Enable SMU output ----------------------------------------
                log.info("Enabling SMU output on channel %s", channel)
                smu.channels[channel].output_enabled = True
                smu.initiate()

                # ---- Burst the pattern ----------------------------------------
                log.info("Bursting pattern '%s' ...", start_label)
                dig.burst_pattern(
                    start_label=start_label,
                    select_digital_function=True,
                    wait_until_done=True,
                    timeout=timeout,
                )

                if self._abort_event.is_set():
                    smu.channels[channel].output_enabled = False
                    return pb2.RunTestResponse(
                        success=True, message="Aborted", test_passed=False
                    )

                # ---- Collect per-site pass/fail --------------------------------
                site_pass_fail_raw: dict[int, bool] = dig.get_site_pass_fail()
                test_passed = all(site_pass_fail_raw.values())

                # ---- Collect HRAM failures ------------------------------------
                failures: list[pb2.HRAMFailure] = []
                for site_num, site_passed in site_pass_fail_raw.items():
                    site_str = f"site{site_num}"
                    try:
                        sample_count = (
                            dig.sites[site_str].get_history_ram_sample_count()
                        )
                    except Exception:  # noqa: BLE001
                        sample_count = 0

                    if sample_count <= 0:
                        continue

                    cycle_infos = dig.sites[site_str].fetch_history_ram_cycle_information(
                        sample_index=0, samples_to_read=sample_count
                    )

                    # Retrieve pin names for this pattern (best-effort)
                    try:
                        pin_names = dig.get_pattern_pin_names(start_label)
                    except Exception:  # noqa: BLE001
                        pin_names = []

                    for ci in cycle_infos:
                        # Build per-pin state strings
                        expected = [str(s) for s in (ci.expected_pin_states or [])]
                        actual = [str(s) for s in (ci.actual_pin_states or [])]
                        ppf = list(ci.per_pin_pass_fail or [])

                        failures.append(
                            pb2.HRAMFailure(
                                site_number=site_num,
                                cycle_number=ci.cycle_number,
                                pattern_name=ci.pattern_name,
                                time_set_name=ci.time_set_name,
                                vector_number=ci.vector_number_in_pattern,
                                scan_cycle_number=ci.scan_cycle_number,
                                pin_names=pin_names,
                                expected_states=expected,
                                actual_states=actual,
                                per_pin_pass_fail=ppf,
                            )
                        )

                # ---- Turn off SMU output --------------------------------------
                smu.channels[channel].output_enabled = False
                smu.abort()

                # ---- Write TDMS log -------------------------------------------
                tdms_path = ""
                if request.tdms_log_file:
                    tdms_path = self._write_tdms(
                        path=request.tdms_log_file,
                        failures=failures,
                        site_pass_fail=site_pass_fail_raw,
                        smu_cfg=smu_cfg,
                        dig_cfg=dig_cfg,
                        start_label=start_label,
                    )

                log.info(
                    "Test complete: %s | sites: %s | failures: %d",
                    "PASS" if test_passed else "FAIL",
                    site_pass_fail_raw,
                    len(failures),
                )

                return pb2.RunTestResponse(
                    success=True,
                    message="PASS" if test_passed else f"FAIL – {len(failures)} failures",
                    test_passed=test_passed,
                    failures=failures,
                    tdms_file_path=tdms_path,
                    site_pass_fail=site_pass_fail_raw,
                    total_failures=len(failures),
                )

        except Exception as exc:  # noqa: BLE001
            # Best-effort: turn SMU off on error
            try:
                if self._smu_session and self._smu_config:
                    self._smu_session.channels[
                        self._smu_config.channel or "0"
                    ].output_enabled = False
            except Exception:  # noqa: BLE001
                pass
            log.exception("RunTest failed")
            return pb2.RunTestResponse(success=False, message=f"RunTest failed: {exc}")

    # =======================================================================
    # RPC: AbortTest
    # =======================================================================
    def AbortTest(self, request, context):
        log.info("AbortTest requested")
        self._abort_event.set()
        try:
            with self._lock:
                if self._dig_session:
                    self._dig_session.abort()
                if self._smu_session and self._smu_config:
                    self._smu_session.channels[
                        self._smu_config.channel or "0"
                    ].output_enabled = False
            return _ok("Abort sent")
        except Exception as exc:  # noqa: BLE001
            return _err(f"AbortTest error: {exc}")

    # =======================================================================
    # RPC: GetStatus
    # =======================================================================
    def GetStatus(self, request, context):
        with self._lock:
            hram_trigger = ""
            if self._hram_config:
                hram_trigger = self._hram_config.trigger_type
            return pb2.SystemStatus(
                smu_initialized=self._smu_session is not None,
                digital_initialized=self._dig_session is not None,
                smu_resource=self._smu_config.resource_name if self._smu_config else "",
                digital_resource=self._dig_config.resource_name if self._dig_config else "",
                configured_voltage=(
                    self._smu_config.voltage_level if self._smu_config else 0.0
                ),
                hram_trigger_type=hram_trigger,
            )

    # =======================================================================
    # RPC: Shutdown
    # =======================================================================
    def Shutdown(self, request, context):
        log.info("Shutdown requested – closing instrument sessions")
        try:
            with self._lock:
                self._close_smu()
                self._close_digital()
            return _ok("Sessions closed")
        except Exception as exc:  # noqa: BLE001
            return _err(f"Shutdown error: {exc}")

    # =======================================================================
    # Internal helpers
    # =======================================================================
    def _close_smu(self):
        if self._smu_session is not None:
            try:
                self._smu_session.close()
            except Exception:  # noqa: BLE001
                pass
            self._smu_session = None

    def _close_digital(self):
        if self._dig_session is not None:
            try:
                self._dig_session.close()
            except Exception:  # noqa: BLE001
                pass
            self._dig_session = None

    def _write_tdms(
        self,
        path: str,
        failures: list,
        site_pass_fail: dict,
        smu_cfg,
        dig_cfg,
        start_label: str,
    ) -> str:
        """Write test results to a TDMS file.  Returns the written file path."""
        if not _NPTDMS_OK:
            log.warning("nptdms not installed – skipping TDMS write")
            return ""

        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

        timestamp = datetime.now().isoformat(timespec="seconds")

        with TdmsWriter(path) as writer:
            # ---- Root metadata ------------------------------------------------
            root_props = {
                "Timestamp": timestamp,
                "Pattern": start_label,
                "SMU Resource": smu_cfg.resource_name if smu_cfg else "",
                "SMU Channel": smu_cfg.channel if smu_cfg else "",
                "SMU Voltage (V)": smu_cfg.voltage_level if smu_cfg else 0.0,
                "SMU Current Limit (A)": smu_cfg.current_limit if smu_cfg else 0.0,
                "Digital Resource": dig_cfg.resource_name if dig_cfg else "",
                "Pin Map": dig_cfg.pin_map_file if dig_cfg else "",
                "Levels File": dig_cfg.levels_file if dig_cfg else "",
                "Timing File": dig_cfg.timing_file if dig_cfg else "",
            }
            root_obj = RootObject(properties=root_props)

            # ---- Site Pass/Fail summary group ---------------------------------
            spf_group = "Site Pass Fail"
            site_nums = np.array(sorted(site_pass_fail.keys()), dtype=np.int32)
            site_results = np.array(
                [int(site_pass_fail[s]) for s in sorted(site_pass_fail.keys())],
                dtype=np.int32,
            )
            writer.write_segment([
                root_obj,
                GroupObject(spf_group, properties={"Test Timestamp": timestamp}),
                ChannelObject(spf_group, "Site Number", site_nums),
                ChannelObject(spf_group, "Passed (1=Pass 0=Fail)", site_results),
            ])

            # ---- Failure detail group -----------------------------------------
            if failures:
                fail_group = "HRAM Failures"
                f_site = np.array([f.site_number for f in failures], dtype=np.int32)
                f_cycle = np.array([f.cycle_number for f in failures], dtype=np.int64)
                f_vector = np.array([f.vector_number for f in failures], dtype=np.int64)
                f_scan = np.array([f.scan_cycle_number for f in failures], dtype=np.int64)
                f_pattern = [f.pattern_name for f in failures]
                f_timeset = [f.time_set_name for f in failures]

                writer.write_segment([
                    GroupObject(fail_group, properties={"Total Failures": len(failures)}),
                    ChannelObject(fail_group, "Site Number", f_site),
                    ChannelObject(fail_group, "Cycle Number", f_cycle),
                    ChannelObject(fail_group, "Vector Number", f_vector),
                    ChannelObject(fail_group, "Scan Cycle Number", f_scan),
                    ChannelObject(fail_group, "Pattern Name", np.array(f_pattern)),
                    ChannelObject(fail_group, "Time Set Name", np.array(f_timeset)),
                ])

        log.info("TDMS written to: %s", path)
        return path


# ---------------------------------------------------------------------------
# Server entry-point
# ---------------------------------------------------------------------------
def serve(host: str = "0.0.0.0", port: int = 50051):
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=4),
        options=[
            ("grpc.max_send_message_length", 64 * 1024 * 1024),
            ("grpc.max_receive_message_length", 64 * 1024 * 1024),
        ],
    )
    pb2_grpc.add_InstrumentTestServiceServicer_to_server(
        InstrumentTestServicer(), server
    )
    address = f"{host}:{port}"
    server.add_insecure_port(address)
    server.start()
    log.info("gRPC server listening on %s", address)
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        log.info("Keyboard interrupt – stopping server")
        server.stop(grace=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NI Instrument Test gRPC Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=50051, help="Bind port")
    args = parser.parse_args()
    serve(host=args.host, port=args.port)
