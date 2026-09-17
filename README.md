# CubeSat GCS — Python Ground Control Station

This application is a professional Python replacement for the supplied LabVIEW CTE ground station. It preserves the original cream/navy/red operator-console architecture while implementing the SSP and legacy fixed-length AX.25 protocols used by the OBC, COMM, Payload, and ADCS projects.

## Capabilities

- Direct SSP over USB/NI RS-485 plus selectable native SSP or legacy AX.25 over the HC-12 ground link
- Selectable legacy reflected CCITT, IBM/ARC, or ICD CRC-16/IBM-3740, plus receive-side auto detection
- Complete **Space Keys / Grad Project** spacecraft selector. It changes SSP
  subsystem addresses and telemetry schemas as one contract; AX.25 stays identical.
- Profile-aware subsystem tabs: Space Keys retains the LabVIEW telemetry views,
  while Grad Project switches OBC, EPS, ADCS, COMM, and Payload to the exact
  ICD v3.1 Table 11–15 fields. The Grad Space Environment view is explicitly
  marked unavailable because that record is not defined by the Grad ICD.
- Length-driven SSP parsing and fixed 254-byte AX.25 parsing for split, back-to-back, noisy, and locally echoed frames
- Complete command builder, custom frame transmission, live CRC preview, and command presets
- OBC STIM date/time selector with PC-time fill, mission-epoch seconds, and the
  exact 8-byte little-endian command preview used by the flight firmware
- Live SSP/AX.25 frame analysis with ACK, NACK, PEND, source, destination, command, data, and CRC status
- LabVIEW-compatible OBC, EPS, Space Environment, COMM, Payload camera, ADCS,
  COMM image, image-data, main-status, time-tagged-command, and FAQ workspaces
- Exact raw telemetry strips beside decoded mission fields so unknown legacy
  bytes are never silently relabelled
- COMM received/delivered command counters and OBC power/serial/time-tagged
  status panels matching the original operator grouping
- ADCS gyro/magnetometer/reaction-wheel decoding matching `PAYLOAD_ADCS/SSP.cpp`
- Payload mode, cached image size, segment counter, and image-error decoding
- ESP32-CAM Wi-Fi status and direct JPEG download from `http://192.168.4.1`
- Integrated CIMG controls for resolution, quality, flash, effect, and brightness. Space Keys uses CIMG → TIMG → Wi-Fi; Grad follows the ICD and uses one CIMG ACK followed by direct Wi-Fi download.
- LabVIEW-style session logs, robust historical replay (including SSP frames
  split across log lines and concatenated three-copy AX.25 downlinks), and
  long-form telemetry CSV recording
- Separate AX.25 physical, logical, repeated-copy, and rejected counters. All
  three physical satellite copies remain visible in the session log, while
  telemetry and sequence state are updated only from the first valid copy.
- Original 64-byte Payload telemetry decoding using the old-satellite ICD
  offsets, including image size, SPI ACK/NACK, capture parameters, and both
  radio MAC addresses.
- Time-tagged command queue
- Demonstration mode for training without connected hardware
- Settings persistence under `%LOCALAPPDATA%\CubeSatGCS`
- Laptop-safe responsive window with horizontal and vertical sliders for the
  complete console; large displays expand normally without changing panel order

## Run from source

```powershell
cd D:\PL_ADCS_v1.4\space_keys_cte
py app.py
```

## Build the Windows executable

```powershell
cd D:\PL_ADCS_v1.4\space_keys_cte
.\build_exe.ps1
```

The verified output is `dist\CubeSat_GCS.exe`.

## Operator start-up

1. Connect the USB/NI RS-485 interface or the PC-side HC-12 UART and choose its COM port.
2. Use **115200 baud** for direct STM32/ESP32 RS-485. Use **9600 baud** for the HC-12 RF path unless the radio pair has been configured to another rate.
3. Select **Subsystems / RS-485** for a direct bus connection. For HC-12,
   select **Ground Station / AX.25** or **Ground Station / SSP**. COMM detects
   `7E` AX.25 and `C0` SSP frame starts and replies using the same protocol.
   Ground Station / SSP sends the raw SSP packet immediately and does not
   require an AX.25 `INIT` session.
4. Select **Space Keys** or **Grad Project**. The CTE selects legacy reflected CCITT for Space Keys and **CRC-16/IBM-3740** for Grad Project.
5. Send PING and confirm a green CRC indicator and ACK. Ground-station AX.25 uplinks use the mission callsigns `ESKQUB` (spacecraft) and `EGYGCS` (ground station), with A7 values 2 and 3.

## Firmware profile selection

Each active firmware target has the same two build macros:

```c
#define SATELLITE_PROFILE SPACE_KEYS      /* or GRAD_PROJECT */
#define SSP_CRC16_ALGORITHM SSP_CRC16_CCITT /* or SSP_CRC16_IBM / SSP_CRC16_IBM_3740 */
```

The selectors are in `COMM/ported/comm_config.h`,
`OBC/ported/STM32F401_OBC/src/satellite_profile.h`,
`PAYLOAD_ADCS/Config.h`, and `ESP32CAM/SatelliteConfig.h`. OBC owns its local
EPS and ADCS implementations, so those telemetry builders inherit the OBC
selection. In the Grad profile, addresses are OBC `A1`, EPS `A2`, ADCS `A3`,
Payload `A4`, S-band `A5`, UHF/HC-12 `A6`, GCS `B0`, and broadcast `FF`.
The Grad command catalog follows ICD Table 10: HI `01`, ACK `02`, NACK `03`,
PING `04`, STIME `05`, SMODE `06`, GOTLM `07`, GSTLM `08`, SON `09`,
SOFF `0A`, CIMG `0C`, DIMG `0D`, and GIMG `0E`. GOTLM and GSTLM replies use
`47` and `48` respectively.

Grad telemetry follows ICD v3.1 Tables 11–15 in index order and little-endian
wire order. OBC longitude and latitude are signed 32-bit fields as required by
this project. The two consecutive EPS SRecords fields from Table 12 are both
transmitted. The source trees currently default to Grad Project for the ICD
hardware test; selecting `SPACE_KEYS` retains the original layouts and IDs.

For Wi-Fi images, connect Windows to SSID `AAST` using password `12345678`; ESP32-CAM is normally `http://192.168.4.1`. In Grad mode, `CIMG 0C` captures and publishes the image, returns exactly one ACK whose data is `0C`, and the CTE downloads `/image`; no TIMG exists in the ICD flow. Space Keys retains its CIMG/TIMG workflow. The standalone receiver auto-downloads each new published revision. JPEG bytes never travel on Serial0, RS-485, or HC-12. Direct RS-485 requests may use either Core-OBC or Ground Station as the SSP source; the CTE applies the selected profile's address automatically.

## Hardware notes

The current Payload/ADCS RS-485 transceiver control is `/RE = GPIO 4` and `DE = GPIO 0`. The PC-side USB adapter should normally provide automatic direction control. Use one common ground and terminate the two physical ends of the RS-485 bus only. The new COMM RF interface is an HC-12 transparent byte stream. AX.25 mode sends the established fixed 254-byte frames and physical repetitions; SSP mode sends one native variable-length SSP frame.

## Protocol reference

SSP frame layout:

```text
C0 DEST SOURCE COMMAND LENGTH DATA... CRC_LO CRC_HI C0
```

The CRC covers `DEST` through the final data byte. The Grad ICD PING from GCS to OBC is:

```text
C0 A1 B0 04 00 39 2A C0
```

The RF envelope is a fixed 254-byte mission frame, not shifted amateur-radio AX.25:

```text
7E | destination ASCII[6] | destination A7 | source ASCII[6] | source A7
   | control | SSP data/padding | CRC_LO | CRC_HI | 7E
```

The outer CRC is the legacy reflected CCITT calculation over bytes 1 through
250. The parser is length-driven because a CRC byte may itself equal `7E`.
The COMM firmware repeats every prepared downlink frame three times. The CTE
counts and logs all physical copies, but processes the first valid copy as one
logical frame. A `GOSTM` reply is therefore `RNR x3`, telemetry I-frame `x3`,
then `RR x3`. I-frame sequence numbers use the flight software's six-frame
window (`0` through `5`), and the CTE wraps N(S)/N(R) with the same modulo-6
rule.
