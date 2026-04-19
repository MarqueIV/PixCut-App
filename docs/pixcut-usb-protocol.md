# Liene / PixCut USB Device Control Protocol (Reverse-Engineered)

> **Status:** Work in progress  
> **Source:** USB capture, binary analysis, and runtime observation  
>
> - `responses.jsonl` (device responses extracted from USB capture)  
> - `cmd_data_stream.bin` reconstruction (JPG/PLT recovered from `cmd data` chunks)  
> - `Comm20260117.log` (official Windows app comm log; includes WinUSB open details + observed request/response JSON)
> **Scope:** Local USB protocol used by the Liene PixCut device and official desktop apps  
> **Not an HTTP or cloud API**

This document records **empirical findings** about the PixCut device’s USB protocol.  
Everything here reflects **observed behavior** unless explicitly marked as inferred or unknown.

---

## 1. High-Level Architecture

- Transport: **USB (vendor-specific interface)**
- Endpoints:
  - **Bulk OUT** — host → device (commands, data)
  - **Bulk IN** — device → host (responses, status)
- Logical layers:
  - **Control plane:** JSON messages (`cmd json`)
  - **Data plane:** raw binary streaming (`cmd data`)
- Job model:
  - A single logical job may bundle **multiple documents**
    - Print image (JPG)
    - Cut path (PLT)

---

## 2. USB Device Context (Observed)

- USB class: **Vendor-specific**
- Not a USB Printer class device
- No OS print spooler involvement
- Communication is entirely user-space via bulk transfers
- Interfaces/endpoints that work reliably (from captures and testing):
  - Control/JSON: **interface 2**, bulk OUT `0x06`, bulk IN `0x86`
  - Data: **interface 3**, bulk OUT `0x04`, bulk IN `0x84`

### 2.1 Windows (WinUSB) device open details (from `Comm20260117.log`)

The official Windows app uses **LibUsbDotNet.WinUsb** to enumerate and open the device.

Observed identifiers:

- **VID/PID:** `0x302C / 0x3101` (decimal `12332 / 12545`)
- Device path includes: `vid_302c&pid_3101` and `mi_03` (interface `3`)
- The app selects a channel type labeled `"USB_PRINT"` during enumeration

Observed endpoints (as labeled by LibUsbDotNet):

- **ReadEp:** `Ep04`
- **WriteEp:** `Ep04`

Notes:

- LibUsbDotNet’s `Ep04` is an internal endpoint label; confirm the underlying endpoint address/direction during MVP development (USB descriptors).

---

## 3. Command Framing Overview

All traffic is ASCII-framed and sent over USB bulk endpoints.

Two explicit command modes exist:

### 3.1 `cmd json` — Control Plane

Used for:

- Job creation
- State polling
- Device properties
- Counters / diagnostics

Format:
`cmd json\n
{ JSON object }`
Response shape (JSON-RPC–like):

- Responses are JSON objects containing at minimum:
  - `id` (number)
  - `result` (variant type)
- `result` can be:
  - **array** (including positional tuples)
  - **object**
  - **array of objects**
- Some numeric-looking fields are returned as **strings** (confirmed for printer state/sub-state)

---

### Request/response correlation (`id`)

Responses echo the request `id` and the host correlates on that value.

**Important:** the official app reuses small `id` values during polling (e.g., repeated `id=123` for `get-job-info` and `id=124` for `get-prop`), while using a different `id` for `combo-job` (e.g., `1234`). Do not assume `id` is globally unique—treat it as a short-lived correlation token.

### 3.2 `cmd data` — Data Plane

Used for:

- Image upload (JPG)
- Cut path upload (PLT)

Format:
`cmd data EXTLEN=<total_bytes>\n<4-byte-job-id><binary-payload>`

Observed behavior:

- Data is **chunked**
- **Framing:** The command line (`cmd data...`) and the payload are sent in the **same USB bulk transfer**; no ZLPs required.
- **Internal Header = Job ID:** The 4-byte field after the newline must be the **little-endian job-id** returned by `combo-job`. If this is wrong, the device reports `transfer-size=0` and times out (`event.rpt_err`).
- Actual payload size per chunk = `EXTLEN - 4` (job-id counts toward EXTLEN).
- **Acknowledgments:**
  - Success: `cmd data EXTLEN=... OK`
  - Failure: `cmd data EXTLEN=... ER <message>` (e.g., `unsupported cmd`)

Typical chunk sizes:

- `EXTLEN=4075` (most common; yields a 4096-byte packet including header + ASCII)
- Final chunk shrinks to remaining bytes
- Native pacing is ~100–130 ms between chunks; matching this avoids timeouts.

Reconstruction:

- Concatenating chunk payloads into a single stream (`cmd_data_stream.bin`) allows recovery of full artifacts.
- JPEG reconstruction using SOI/EOI markers is reliable on captured runs (at least one run reconstructed perfectly).

---

## 4. Job Model

### 4.1 Combo Jobs

The device supports a **combo job** containing multiple documents.

Observed documents:

- Print document (JPG)
- Cut document (PLT)

The host:

1. Declares the job via JSON
2. Uploads each document via `cmd data`
3. Polls job state until completion

---

## 4.2 Transfer Accounting (Observed)

`get-job-info` includes both:

- `file-size` — declared logical document size
- `transfer-size` — bytes transferred according to device state reporting

In at least one observed job, `transfer-size` exceeded `file-size`.  
Working theory: `transfer-size` includes additional transferred artifacts (e.g., the PLT) and/or per-chunk framing/header overhead. Needs validation across additional captures where PLT/JPG sizes are known.

---

## 4.3 Media Sizes and Physical Dimensions (Observed)

- **Marketed media sizes differ from actual physical dimensions.**
- Explicitly:
  - Sticker (cut-capable) media marketed as “4x7” measures **100 mm x 196.5 mm**.
  - Extra length is present to allow roller grip and reliable transport.
  - Print-only “4x6” media does **not** support cutting.
- The `media-size` codes correspond to physical stock SKUs, **not** to the nominal inch dimensions.

|`media-size` Code|`media-type`|Physical Dimensions / Notes|
|---|---|---|
|5013|2030|100 mm x 196.5 mm cut-capable sticker stock (**confirmed**)|
|5012|2010|4×6" photo paper — print-only, no cutting (**confirmed**)|
|5009|—|Unknown stock. Reported by `get-prop(media-size)` when printer is in error state (5414 jam); may also be the code for clear sticker paper (unconfirmed). `big-data` shows `finished_5009: 0` — no jobs have completed with this stock.|

Measured cut dimensions (~42.5–43 mm for nominal 40 mm art with border) are consistent with a 1016 units/inch internal coordinate grid when accounting for cutter compensation and border logic.

---

## 5. JSON Methods and Response Models (Observed)

### 5.0 Device Identity / Capabilities (Observed)

At least one identity/capabilities query returns a **positional tuple** (array) combining identity strings and settings objects.

The official app fetches identity/config via `get-prop` for:
`firmware-revision`, `hardware-revision`, `model`, `sku`, `serial-number`, `mac-address`, `bt-phone-mac`, `sn-pcba`, `media-size`, `auto-off-interval` (where `0` means auto-off disabled), and `big-data`.
The same tuple is also returned by the convenience property `device-info` (observed via experimental probing).

Observed tuple order (confirmed/inferred from response content + user validation):

1. **Device model** (e.g., `DHP700`)
2. **MAC address** (e.g., `F0:13:C1:45:B3:2B`)
3. **Serial number** (string)
4. **Unknown identifier** (string; meaning TBD)
5. **Firmware version string** (e.g., `1.0.15_0054`)
6. **Subversion / platform code** (unknown; e.g., `x05`)
7. **Settings objects** (observed so far):
   - `media-size` (integer code; also used by jobs; mapping to physical media SKUs TBD)
   - `auto-off-interval` (integer; **0 means auto-off disabled**; this is a mutable setting)

Notes:

- The presence of settings objects inside the identity response implies a mixed “capabilities + config” query, not a pure identity call.
- `device-info` returned an **object** with underscore-style keys:  
  `fw_ver`, `hw_ver`, `sn_pcba`, `sn_all`, `sn_alps`, `sku`, `model`, `mac`, `paper_size` (value seen: `"3x3"`).  
  These underscore variants mirror the dashed props; prefer dashed forms when possible, but record both for completeness.

---

### 5.1 `combo-job`

Creates a new logical job and declares documents.

The Windows app submits `combo-job` with two entries in `params`:

#### print-job

- `document-format`: `9` JPEG; `10` PNG; `11` BMP
- `print-quality`: `4`
- `copies`: `1`
- `media-size`: `5013` (sticker 4x7); `5012` (photo 4x6)
- `media-type`: `2030` (sticker 4x7); `2010` (photo 4x6)
- `job-type`: `0` (photo only); `600` (photo + cut). Cut-only (`job-type` without a photo) is **not supported** — the device silently discards the job and returns to idle with all-zero job fields.
- `channel`: observed `14864` on USB, `30784` / `30960` over BT
- `timeout`: `180`
- `document-name`: Windows path under `...\AppData\Local\LienePhoto\HtPrintDocuments\...jpg`
- `hash-method`: `1` (SHA1); `2` (MD5) also mentioned in BT doc
- `hash-value`: hash of photo (captures show constant `26b8...` placeholder)
- `user-account`: `"12345678"` observed default
- `link-type`: `1000` for photo-only jobs; `0` for photo+cut (from BT doc)
- `job-send-time`: Unix timestamp (seen in BT doc; not yet confirmed on USB)

#### cut-job

- `document-format`: `18` (PLT)
- `timeout`: `100`
- `document-name`: Windows path under `...\AppData\Local\LienePhoto\HtPrintDocuments\...plt`
- `hash-method`: `1` (SHA1) or `2` (MD5)
- `hash-value`: per-file hash (captures sometimes reused photo hash)
- `copies`: `1` (included in BT doc schema)
- `channel`, `media-size`, `media-type`, `job-type`: mirror print-job

Response example:

- `result` is an object containing `{ "job_id": <int> }` (e.g., `19`)

Observed characteristics:

- Returns or implicitly assigns a `job-id`
- Declares metadata **before** data upload
- Can represent both print and cut as a single logical “combo” job

Notes:

- Exact JSON parameter schema still being firmed up from request-side extraction.
- `channel` is present in job reports (often `-1`) but semantics are unknown.

#### Observed request schema (from `requests.jsonl`)

`combo-job` uses `params` as an **array** of method objects (observed: two entries):

1) `print-job` with `params` keys:

- `uuid`, `job-url`, `print-quality`, `copies`, `document-name`, `file-size`, `document-format`
- `hash-method`, `hash-value`
- `media-size`, `media-type`
- `user-account`, `job-type`, `channel`, `timeout`

1) `cut-job` with `params` keys:

- `file-size`, `document-format`, `document-name`, `job-url`
- `hash-method`, `hash-value`, `timeout`

`document-name` in the official app is a local filesystem path (Windows path observed). The device likely treats it as metadata/label rather than dereferencing it.

After `combo-job`, the host begins frequent polling immediately—interleaving `get-job-info` with `get-prop` state checks throughout the print/cut lifecycle

---

### 5.2 `get-job-info`

Polls the state of a specific job.

Parameters:

- `job-id`

Observed response shape:

- `result` is an array containing a single object: `[ { ... } ]` (USB)
- BT doc shows an object directly (no array); fields align.

Observed fields (partial):

| Field | Meaning |
| --- | --- |
| `job-id` | Job identifier |
| `job-state` | High-level job state (3=processing, 9=completed) |
| `job-sub-state` | Detailed phase (3005=printing, 9000=completed) |
| `job-state-reason` | Reason/status code |
| `copies` | Requested copies |
| `printing-page-number` | Printing progress indicator (observed 0 → 1) |
| `cutting-progress` | Cutting progress indicator (increases during cutting) |
| `cut-contours` | Contour count (increases during cutting) |
| `media-size` | Media code (matches job selection) |
| `media-type` | Media type code (meaning TBD) |
| `job-type` | Job type code (meaning TBD) |
| `document-format` | Document format code as reported in job-info (meaning TBD) |
| `file-size` | Declared file size |
| `transfer-status` | Transfer state (0=ok; 3=error/timeout observed) |
| `transfer-size` | Device-reported transferred bytes |

Observed lifecycle codes (confirmed):

- **Running:**
  - `job-state = 3`
  - `job-sub-state = 3005`
  - `job-state-reason = 30001`
- **Complete:**
  - `job-state = 9`
  - `job-sub-state = 9000`
  - `job-state-reason = 90001`

Operational observations:

- `printing-page-number` transitions from 0 → 1 during printing.
- `cutting-progress` and `cut-contours` increase during cutting phases.
- `user-account` is present (observed as `"12345678"`); semantics unknown (may be placeholder/default).
- `channel` observed as `-1`; semantics unknown.

---

### 5.3 `get-prop`

Generic property query.

Parameters:

- Array of property names

Observed printer state return model:

- Returns a positional triple:
  - `["<printer-state>", "<printer-sub-state>", "<printer-state-alerts>"]`

Notes:

- `printer-state` and `printer-sub-state` are observed returned as **strings** on USB, **numbers** in BT doc; treat as strings when parsing USB.
- `printer-state-alerts` is observed as a compact string such as `::0` (encoding unknown; likely bitfield/namespace).

Common properties observed:

- `printer-state`
- `printer-sub-state`
- `printer-state-alerts`
- `big-data` (see below)
- Naming: the device prefers **dash**-separated keys. Underscore variants (e.g., `big_data`, `bt_fw_ver`, `sn_pcba`) have returned empty objects on USB; use `big-data`, `bt-fw-ver`, `sn-pcba` instead.
- `ota-progress` — returns `{"progress": <int>}` (semantics TBD; value looked like a counter)
- `bt-fw-ver` — Bluetooth firmware version (returned `{}` in USB probe; likely BT-only)
- `auto-sleep-interval`, `off_interval`, `sleep_interval` — observed keys; returned `{}` (likely placeholders/BT-only)
- `media-type` — sometimes `{}`; whereas `media-size` returns an object wrapper (e.g., `{"media-size": 5009}`)
- `sn_pcba` — may return `{}` on USB though seen populated in other captures

#### Other observed properties queried

Beyond the state/job loop, the official app queries:

- `firmware-revision`, `hardware-revision`, `model`, `sku`, `serial-number`
- `mac-address`, `bt-phone-mac`, `sn-pcba`
- `media-size`, `auto-off-interval`

---

### 5.4 `big-data`

Returns counters and error flags.

Observed response shape:

- `result` is an array containing a single object: `[ { ... } ]`

Observed fields include:

- Error flags:
  - `paper_jam`
  - `paper_empty`
  - `no_ink_ribbon`
  - `hw_error`
  - (and others; mostly 0 in normal runs)
- Totals:
  - `printed`
  - `finished`
  - `cancelled`
- Media-specific stats:
  - `finished_<mediaCode>`
  - `cancelled_<mediaCode>`

Observed media-size codes (mapping to physical media TBD):

| Code | Where observed |
| --- | --- |
| 5009 | Setting/capabilities + big-data counters |
| 5012 | big-data counters |
| 5013 | Active job + big-data counters |

---

### 5.5 `upgrade-firmware`

Upgrades device firmware (untested)

- Parameters:
  - `job-type = 100`
  - `file-size = 3024852` (size of the rbn firmware file)

Assumption is that cmd data would send the rbn file, but this is untested.

---

## 5.6 Additional constants from Bluetooth protocol (helpful on USB)

- **Document format codes:** JPG=9, PNG=10, BMP=11, PLT=18.
- **Media:** 5012 = photo 4x6; 5013 = sticker 4x7. Media-type 2010 = photo 4x6; 2030 = sticker 4x7.
- **Job-type:** 0 = photo-only; 600 = photo+cut.
- **Hash-method:** 1 = SHA1, 2 = MD5.
- **Channel:** observed values 14864 (USB), 30784/30960 (BT); semantics unknown.
- **Job send time:** BT schema includes `job-send-time` (Unix timestamp) and `link-type` (1000 photo-only, 0 photo+cut); not yet seen on USB captures.

---

### 5.5 Field Glossary (Protocol vs App Telemetry)

The official app binaries contain many strings that are **not** device protocol fields (e.g., analytics keys, platform metadata). Use this as a quick discriminator when mining strings.

#### Device protocol / job telemetry (high-confidence)

These fields are consistent with observed `get-job-info`, `get-prop`, and `big-data` payloads:

- Job fields: `job-id`, `document-name`, `printing-page-number`, `total-pages`, `file-transfer-progress`, `job-state`, `job-sub-state`, `job-state-reason`, `user-account`, `job-url`, `print-color-mode`, `media-size`, `media-type`, `job-type`, `transfer-status`, `transfer-size`, `channel`, `document-format`, `file-size`
- Identity fields: `firmware-revision`, `hardware-revision`, `serial-number`, `mac-address`, `bt-phone-mac`, `sn-pcba`
- Counters / events: `printed`, `finished`, `cancelled`, `finished_<mediaCode>`, `cancelled_<mediaCode>`, `poweroned`, `poweroffedUser`, `poweroffedSys`, `userreseted`, `btconned`, `btdisconned`, `decode_error`, `paper_empty`

#### App telemetry / environment (likely NOT device protocol)

These often appear in app logs/strings but are not expected in device JSON payloads:
`appPackage`, `appState`, `appVersion`, `deviceBrand`, `deviceId`, `deviceModel`, `deviceType`, `deviceVersion`, `eventName`, `eventSequenceId`, `language`, `latitude`, `longitude`, `networkState`, `platformVersion`, `screenHeight`, `screenWidth`, `sdkVersion`, `timestamp`, `timezoneOffset`, `userId`, `countryRegion`, `httpClientFactory`

#### Pipeline hints (may influence PLT generation; not yet observed in device JSON)

The presence of these strings suggests internal processing knobs in the app; treat as research leads:

- `outputPltPath`, `inputImagePath`, `outputImagePath`
- `dilation` (likely affects border/contour expansion)
- `knifePressure` (potential future die-cut control; may map to PLT `KP` or a separate setting/command)

---

### 5.7 Device-Initiated Events

In addition to responding to requests, the device can push event notifications. These have been observed over Bluetooth; USB behavior is not yet confirmed but is expected to be the same.

#### `event.print-job-finish`

Called upon completing a print job. Contains most of the same fields as `get-job-info` plus:

| Field | Type | Description |
| --- | --- | --- |
| `job-send-time` | Number | Unknown; observed as 0 |
| `job-recv-time` | Number | Time to receive job data |
| `file-download-time` | Number | Time to download file |
| `job-execute-time` | Number | Time to execute job |
| `page-summary` | String | Unknown; example: `"7:5:1,5012:3:1"` |
| `alerts-count` | String | Unknown |

#### `event.combo-job-finish`

Called upon completing a combo (print+cut) job. Same fields as `event.print-job-finish` but does **not** include `cutting-progress` or `cut-contours`.

---

## 6. Printer State Model (Observed)

### 6.1 `printer-state` (USB strings vs BT numbers)

| Value | Meaning | Notes |
| --- | --- | --- |
| `"10"` | Initializing | |
| `"20"` | Idle / ready | Returned as string on USB (`get-prop`) |
| `"30"` | Sleep | |
| `"40"` | Busy / job active | Returned as string on USB (`get-prop`) |
| `"50"` | Off | |
| `"60"` | Error | Device-side fault; check `printer-state-alerts` for error code |
| `3` | Processing | Appears as numeric `job-state` in BT `get-job-info` |
| `9` | Completed | Appears as numeric `job-state` in BT `get-job-info` |

### 6.2 `printer-sub-state` (string; partial mapping)

Idle baseline:

- `"2000"` (observed while `printer-state="20"`)

Busy phases observed (while `printer-state="40"`):

- `"3015"`, `"3016"`, `"3018"`, `"3019"`, `"3020"`, `"3021"`, `"3025"`, `"3028"`, `"3029"`, `"3030"`, `"3031"`

The known phase progression for a combo job (from liene-pixcut-s1-api):

`decoding → init → printing_Y → printing_M → printing_C → printing_OC → cutting_pick → cutting_home → cutting_cutting → cutting_eject → idle`

The numeric sub-state codes above map onto these phases, but the exact code-to-phase mapping is not yet confirmed on USB.

### 6.3 `printer-state-alerts` — Error Codes (Observed)

Returned as the third element of the `get-prop(printer-state, printer-sub-state, printer-state-alerts)` response array.
Also appears in the `result` of a rejected `combo-job` response as `{"error-code": <N>}` when the device refuses to start a job.

| Code | Trigger condition | Observed behavior |
| --- | --- | --- |
| `5306` | No paper/media cartridge installed | Appears in `printer-state-alerts` as `::5306` during print attempt. Printer enters state 60 (Error), sub-state 3015. Insert paper cartridge; device auto-resumes. |
| `5401` | Paper cassette out of paper | Appears in `printer-state-alerts` as `::5401` during print attempt. Printer enters state 60 (Error), sub-state 3015. Refill or replace the paper cassette; device auto-resumes. |
| `5414` | Media size mismatch → physical paper jam | Observed when 4×6 photo paper was loaded but job requested `media-size` 5013 (4×7 sticker stock). The printer feeds the paper expecting 4×7 length; the shorter 4×6 sheet jams and must be removed manually by opening the bottom panel. `big-data` paper jam counters do **not** increment for this fault — those counters appear to track only consumable/user-serviceable events, not mid-job mechanical failures. Printer enters state 60 (Error), sub-state 5000. Power cycle required to recover. |
| `8011` | Printer not ready / in error state | Returned in `combo-job` response as `{"error-code": 8011}` when the printer rejects a new job submission (e.g. already in error state from a prior fault). Power cycle the printer and retry. |
| `8101` | Ink/ribbon cartridge empty/depleted | Appears in `printer-state-alerts` as `::8101`; printer pauses mid-job. Device auto-resumes after cartridge replacement. |
| `8102` | No ink ribbon/cartridge installed | Appears in `printer-state-alerts` as `::8102` during print attempt. Printer enters state 60 (Error). Insert ribbon and retry. |

**Notes:**

- `printer-state-alerts` encoding during normal operation is `::0` (observed). The exact format (bitfield vs namespace-colon encoding) is not fully understood.
- When `printer-state="60"` (Error), the alerts field is expected to contain the relevant error code. Always log the raw alerts value alongside any mapped description.
- Further error codes likely exist for paper jams, head errors, etc. — add observations here as discovered.

---

## 7. `jxspi` Marker

The string `jxspi` appears repeatedly between requests and responses in logs.

Observations:

- Does **not** appear inside JSON payloads
- Appears duplicated (`jxspi` twice)
- Likely a **logging artifact or transport boundary marker**
- Not believed to be part of the device protocol itself

Currently treated as **out-of-band**.

---

## 8. End-to-End Job Flow (Observed)

1. Host sends `combo-job` (declares job + documents)
2. Host uploads PLT and JPG via `cmd data` chunks — **PLT must be sent first, then JPEG** (confirmed by sapodilla; reversed order is untested)
   - Note: PLT and JPG appear to be authored/handled in different coordinate frames; analysis overlays required rotation/mirroring to align. Because PLT was captured from host output, this likely represents a print→cut mapping decision made by the host/app (or by a fixed device convention the host adheres to).
3. Host enters poll loop:
   - `get-job-info(job-id)`
   - `get-prop(printer-state, printer-sub-state, printer-state-alerts)`
4. Job completes (`job-state=9`, `job-sub-state=9000`, `job-state-reason=90001`)
5. Host queries `big-data`
6. Printer returns to idle (`printer-state="20"`, `printer-sub-state="2000"`)

**Keep-Alive / Heartbeat:**

During data transfer and processing, the host **must** periodically send status queries (e.g., `get-prop` for `printer-state`) to prevent the device from timing out the connection. The official app sends these approximately every 5 seconds, even while uploading data chunks.

---

## 9. Known Gaps / Further Research

Open questions:

- Full request-side JSON schemas (exact params for identity query, combo-job, get-prop usage)
- Cancel / pause / resume commands
- Error state mappings (`printer-state-alerts` decoding)
- **Cut path complexity caveat:** Very dense PLT paths (thousands of `D` coordinates streamed as one line) have caused the device to halt mid-cut and drive the head off-page. Mitigations that worked in testing:
  - Pre-simplify curves/paths to reduce point count.
  - Thin straight-line runs (remove redundant collinear points) or split long outlines into smaller subpaths.
  - Keep overall PLT size and per-path point counts moderate; the cutter likely has a parser/buffer limit for long HPGL-like streams.
- Meaning of:
  - `channel`
  - `user-account`
  - `media-type`
  - `job-type`
  - `document-format` (as reported in job-info)
  - identity tuple fields #4 and #6
- Calibration, maintenance, firmware update commands
- Full mapping of `printer-sub-state` values to phases
- Whether WinUSB endpoint mapping is fixed to Ep04 in the app, and the underlying endpoint addresses/directions from descriptors
- Whether `dilation` / `knifePressure` exist as actual device settings/commands (or are app-only pipeline controls); search for corresponding JSON methods or `set-prop` style calls
- Confirm effect and valid range of `KP` values in PLT header
- Confirm PLT coordinate scaling (working assumption: **1016 units per inch**) across multiple media sizes and layouts

Planned research:

- Capture cancellation mid-job
- Inject paper / ribbon errors
- Compare Windows vs macOS behavior
- Analyze PLT dialect in detail (command set, scaling, coordinate units)

### Observed but currently unresolved (USB)

These keys/methods were queried over USB and returned empty objects or unclear values. They likely require BT, a different mode, or are unimplemented. Keep them for future validation; treat them as **research/possibly invalid**:

- `bt-fw-ver`
- `auto-sleep-interval`, `off_interval`, `sleep_interval`
- `media-type` (returned `{}` while `media-size` was populated)
- `big_data` (underscore alias; returned `{}`)
- `ota-progress` (returned a large counter; meaning unknown)

---

## 11. PLT Cut Path Format (Observed)

### Overview

- The `.plt` file is a **proprietary, HPGL-inspired plotter language** used to drive the cutting mechanism.
- It is *not* G-code and does **not** expose motion control primitives beyond absolute moves.

### File Structure

- **Header line format:**
  - `IN VER<version> KP<value>`
    - `IN` = initialize
    - `VER` = PLT format version
    - `KP` = knife pressure / knife profile (meaning inferred; see below)
- **Body:**
  - One command per token
  - ASCII text
- **Terminator:**
  - `@` marks end of job (vendor-specific)

### Commands

| Command      | Meaning                                               |
|--------------|-------------------------------------------------------|
| `U x,y`      | Move to absolute coordinate with blade up (no cutting)|
| `D x,y`      | Move to absolute coordinate with blade down (cutting) |
| `IN`         | Initialize plot                                       |
| `@`          | End-of-job marker                                     |

### Coordinate System

- **Absolute coordinates only**
- **Integer units**
- No relative moves, arcs, curves, feeds, or Z-axis control observed
- Coordinate scale appears to align with **classic HPGL plotter units (1016 units per inch)**.
- This corresponds to approximately **0.025 mm per unit**.
- Based on reconstructed PLT bounding boxes (~1710 units) and measured cut size (~42.5–43 mm), this scale fits within expected mechanical tolerance.

**Important:** The reconstructed `.plt` was recovered from the **host → device** USB stream, so any rotation/mirroring seen when overlaying PLT paths on the JPG reflects a **host-side choice of coordinate frame**, not something “baked into” the device capture process. The device or host may still apply a deterministic transform between print-space (raster) and cut-space (plotter). We will validate the exact transform during MVP development by sending known test patterns and comparing physical cuts.

- This choice is consistent with the PLT language being HPGL-inspired; minor discrepancies may result from blade offset, material stretch, or cutter compensation.

An explicit pixel-to-PLT coordinate conversion formula (from liene-pixcut-s1-api; not yet independently validated on USB):

```text
plt_x = (pixel_y * 1.01333 - 14.0) * 3.3866668
plt_y = (pixel_x * 1.01333 -  8.0) * 3.3866668
```

Note the axis swap (pixel_y → plt_x, pixel_x → plt_y), consistent with the −90° rotation applied during SVG→PLT conversion. The scale factor 3.3866668 ≈ 1016 / (300 / 1) confirms the 1016 units/inch assumption at 300 DPI.

### Cutting Semantics

- Each contour begins with `U x,y` followed by one or more `D x,y`
- Multiple contours per file are common
- Observed horizontal offsets indicate multiple stickers laid out across a single sheet

### Knife Pressure / Profile (`KP`)

- The official software does **not** expose pressure controls in its UI, but PLT accepts `KP<value>` anywhere in the stream (header or mid-path) to change pressure.
- Empirical findings:
  - `KP42` is the default used by the official app for Liene sticker media.
  - Above `KP50` performs a full perforation through backing; risks damaging the cutter’s strip. Strip is not officially user-serviable but can likely be replaced with an 8mm wide Graphtec/Roland-compatible cutting strip.
  - Thinner media (e.g., Oracal 651 vinyl) likely needs lower pressure (≈`KP35` observed as a good starting point).
- `KP` may be sent multiple times per job to vary pressure by path/segment; device accepts it inline with `U/D` commands.
- Safe practice: adjust in small steps and test; keep to a single `KP` per job unless deliberately experimenting.

## 10. Disclaimer

This document is **reverse-engineered** from observed behavior.  
It is not affiliated with or endorsed by the device manufacturer.

---

## Appendix: Bluetooth Binary Framing

The Bluetooth transport uses a binary-framed packet format (not the ASCII `cmd json` / `cmd data` framing used over USB). Each BT packet is wrapped in a `0x7E ... 0x7E` envelope with version, type, interaction, encoding, terminal ID, message number, flags, and a wrapping-add checksum. Packets are limited to 896 bytes of payload; larger transfers are split into subpackage messages. The JSON request/response schema is the same as USB once the BT envelope is stripped.

For the full BT packet layout and an ImHex pattern for decoding captures, see the sapodilla reference below.

---

## References

- **sapodilla** by Syfaro — independent reverse-engineering of the same protocol, focused on Bluetooth: [github.com/Syfaro/sapodilla](https://github.com/Syfaro/sapodilla/blob/main/protocol.md)
- **liene-pixcut-s1-api** by sincethestudy — Bluetooth client implementation for the PixCut S1; useful reference if adding BT support: [github.com/sincethestudy/liene-pixcut-s1-api](https://github.com/sincethestudy/liene-pixcut-s1-api)

---

## Appendix: Terminology

- **Control plane**: JSON commands sent via `cmd json`
- **Data plane**: Binary payloads sent via `cmd data`
- **Combo job**: A single logical job containing both print and cut documents
