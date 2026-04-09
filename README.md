# NI SMU + Digital Pattern Test  —  gRPC Application

Controls an **NI SMU (nidcpower)** and an **NI Digital Pattern Instrument
(nidigital)** over a custom gRPC service.  A Tkinter GUI lets you configure
every parameter, run a test, and view HRAM failures logged to a TDMS file.

---

## Project Layout

```
BuildanSMU/
├── proto/
│   └── instrument_test.proto     ← gRPC service + message definitions
├── generated/                    ← auto-generated Python stubs (see step 2)
│   ├── __init__.py
│   ├── instrument_test_pb2.py
│   └── instrument_test_pb2_grpc.py
├── server/
│   └── grpc_server.py            ← gRPC server (runs on the instrument host)
├── client/
│   └── gui_client.py             ← Tkinter GUI client
├── generate_stubs.bat            ← regenerate stubs from the proto file
├── requirements.txt
└── README.md
```

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.10 + | |
| NI-DCPower driver | https://www.ni.com/downloads/ |
| NI-Digital Pattern driver | https://www.ni.com/downloads/ |
| Python packages | `pip install -r requirements.txt` |

---

## Quick Start

### 1 – Install Python dependencies

```cmd
pip install -r requirements.txt
```

### 2 – Generate gRPC stubs (needed once, or after changing the proto file)

```cmd
generate_stubs.bat
```

This writes `generated/instrument_test_pb2.py` and
`generated/instrument_test_pb2_grpc.py`.

### 3 – Start the gRPC server on the instrument host

```cmd
python server\grpc_server.py --host 0.0.0.0 --port 50051
```

> Pass `--host localhost` if the GUI runs on the same machine.

### 4 – Launch the GUI client

```cmd
python client\gui_client.py
```

---

## GUI Tabs

| Tab | Purpose |
|---|---|
| **Connection** | Enter `host:port`, connect/disconnect, quick action buttons |
| **SMU** | Resource name, channel, voltage, current limit, sense, output function |
| **Digital** | Resource name, pin-map, pattern, levels, and timing file pickers |
| **HRAM** | Trigger type (FIRST_FAILURE / CYCLE_NUMBER / PATTERN_LABEL), max samples, cycles to acquire |
| **Run & Log** | TDMS output path, timeout, Run / Abort buttons, live log |
| **Results** | Per-site pass/fail, HRAM failure table with pin-level detail, CSV export |

---

## Typical Workflow in the GUI

1. **Connection tab** → enter server address → **Connect**
2. **SMU tab** → fill in resource name, voltage, limit → **Send SMU Config**
3. **Digital tab** → browse to your `.pinmap`, `.digipat`, `.digilevels`,
   `.digitiming` files → set start label → **Send Digital Config**
4. **HRAM tab** → choose trigger type and sample limits → **Send HRAM Config**
5. **Run & Log tab** → choose a `.tdms` output path → **▶ Run Test**
6. **Results tab** → inspect per-site pass/fail and HRAM failure rows →
   optionally **Export CSV**

---

## gRPC Service Reference

```
ConfigureSMU(SMUConfig)         → StatusResponse
ConfigureDigital(DigitalConfig) → StatusResponse
ConfigureHRAM(HRAMConfig)       → StatusResponse
RunTest(RunTestRequest)         → RunTestResponse
AbortTest(Empty)                → StatusResponse
GetStatus(Empty)                → SystemStatus
Shutdown(Empty)                 → StatusResponse
```

Full message definitions are in [`proto/instrument_test.proto`](proto/instrument_test.proto).

---

## SMU Settings

| Field | Description |
|---|---|
| `resource_name` | VISA / NI-MAX resource, e.g. `PXI1Slot2` |
| `channel` | Channel string, e.g. `0` or `0,1` |
| `voltage_level` | Output voltage in Volts |
| `current_limit` | Current compliance limit in Amps |
| `voltage_level_range` | Voltage range (0 = autorange) |
| `current_limit_range` | Current range (0 = autorange) |
| `sense` | `LOCAL` or `REMOTE` (Kelvin) sensing |
| `source_delay` | Settling time in seconds before measurement |
| `output_function` | `DC_VOLTAGE` (default) or `DC_CURRENT` |
| `simulate` | `true` opens a simulated driver session for development |

---

## Digital Settings

| Field | Description |
|---|---|
| `resource_name` | VISA / NI-MAX resource, e.g. `PXI1Slot3` |
| `pin_map_file` | Full path to `.pinmap` file |
| `pattern_file` | Full path to `.digipat` file |
| `levels_file` | Full path to `.digilevels` file |
| `timing_file` | Full path to `.digitiming` file |
| `start_label` | Label in the pattern file where burst begins |
| `sites` | List of active site numbers (empty = all) |
| `simulate` | `true` opens a simulated driver session |

---

## HRAM Settings

| Field | Description |
|---|---|
| `trigger_type` | `FIRST_FAILURE`, `CYCLE_NUMBER`, or `PATTERN_LABEL` |
| `max_samples_per_site` | Maximum number of HRAM captures stored per site |
| `cycles_to_acquire` | `FAILED` (only failing cycles) or `ALL` |
| `pretrigger_samples` | Samples to store before the trigger event |
| `number_of_samples_finite` | Whether to stop after `max_samples_per_site` |

---

## TDMS File Structure

| TDMS Group | Channels |
|---|---|
| `Site Pass Fail` | `Site Number`, `Passed (1=Pass 0=Fail)` |
| `HRAM Failures` | `Site Number`, `Cycle Number`, `Vector Number`, `Scan Cycle Number`, `Pattern Name`, `Time Set Name` |

Root-level properties include the test timestamp, SMU voltage, current limit,
digital resource, pin-map path, levels file, and timing file.

---

## Simulation Mode

Both the SMU and Digital configuration dialogs have a **Simulate** checkbox.
When enabled the server opens the NI driver in simulation mode (no physical
hardware required) using generic model strings.  This lets you develop and test
the application without access to the instruments.

---

## Security Note

The gRPC server uses an **insecure channel** (no TLS) suitable for a private
lab network.  Do not expose port 50051 to untrusted networks.  For production
deployments add TLS credentials to `grpc.server()` in `grpc_server.py`.
