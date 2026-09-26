import json
import logging
import secrets
import sys
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple
import hashlib
import io

from .framing import chunk_payload, encode_json_command, parse_balanced_json
from .logging_utils import SessionLogger, hexdump
from .transport import USBConfig, USBTransport

log = logging.getLogger("pixcut.orchestrator")
MAX_JPG_BYTES = 1024 * 1024  # ~1 MiB device limit observed

# Known printer error codes from firmware/source analysis and observed sessions.
# Wire format for printer-state-alerts is commonly "::CODE" (e.g. "::8102").
PRINTER_ERROR_CODES: Dict[int, str] = {
    5001: "Paper is empty - load a new sheet and wait for the printer to resume.",
    5002: "Paper is installed but the printer cannot identify its size - check the tray and media.",
    5003: "Printer detected 4x6 media when this job expects different stock.",
    5004: "Printer detected 5x7 media when this job expects different stock.",
    5005: "Printer detected A4 media when this job expects different stock.",
    5199: "Ribbon or paper feed jam - power off the printer and clear the jammed ribbon or media before retrying.",
    5301: "Paper feeder alignment error - remove and reinsert the paper tray, then retry.",
    5302: "Paper feeder alignment error - remove and reinsert the paper tray, then retry.",
    5303: "Paper feeder alignment error - remove and reinsert the paper tray, then retry.",
    5304: "Paper mismatch - load the correct media for this job.",
    5305: "Paper tray position fault - reseat the tray and retry.",
    5306: "No paper/media cartridge installed - insert the paper cartridge and retry.",
    5401: "Paper cartridge is out of paper - refill or replace the paper cassette and retry.",
    5402: "Paper was not picked up - reload the sheet or tray and retry.",
    5403: "Paper jam in the feed path - power off the printer and clear the jam before retrying.",
    5404: "Paper is too short for this job - remove it and load the correct media.",
    5405: "Paper was not picked from the bypass path - reload the media and retry.",
    5406: "Paper jam at the registration sensor - power off the printer and clear the jam.",
    5407: "Paper jam before the exit sensor - power off the printer and clear the jam.",
    5408: "Paper jam at the exit sensor - power off the printer and clear the jam.",
    5409: "Paper jam in the fuser path - power off the printer and clear the jam.",
    5410: "Paper was not picked for duplex handling - reload the media and retry.",
    5411: "Paper jam at the relay sensor - power off the printer and clear the jam.",
    5412: "Paper jam before the registration sensor - power off the printer and clear the jam.",
    5413: "Bypass tray is out of paper - load media and retry.",
    5414: "Media size mismatch - wrong paper stock loaded for this job type. Open the bottom panel to remove the paper, then power cycle and retry with the correct stock.",
    5415: "Paper kick failed - remove and reload the media, then retry.",
    5416: "Paper mismatch - load the correct media for this job.",
    5417: "Printer is waiting for paper removal - remove the sheet and wait for it to resume.",
    5418: "Printer is waiting for paper removal - remove the sheet and wait for it to resume.",
    5419: "Paper was removed during the job - reload media and retry if the printer does not resume.",
    5420: "Paper pickup jam - clear the feed path and reload the media.",
    5506: "Printer cover is open - close it and wait for the printer to resume.",
    6002: "Printer system error - power-cycle the printer and retry.",
    7305: "Printer battery is very low - connect power before retrying.",
    7306: "Printer battery charge is very low - connect power before retrying.",
    8001: "Printer rejected an invalid print-job request.",
    8006: "Printer is still processing the previous job. Wait a few seconds for it to finish cancelling or resetting, then try again.",
    8008: "File size of job is too large as submitted.",
    8011: "Printer rejected job - likely in an error state. Power it off and back on, then retry.",
    8101: "Ink/ribbon cartridge empty - replace the cartridge.",
    8102: "No ink ribbon installed - insert a ribbon cartridge and retry.",
    8103: "Paper or ribbon jam - power off the printer and clear the jammed media before retrying.",
    8104: "Invalid ribbon type - install the correct ribbon cartridge.",
    8105: "Ribbon jam - power off the printer and clear the ribbon path before retrying.",
    8106: "Ribbon initialization error - reseat or replace the ribbon cartridge.",
    8199: "Ribbon error - reseat or replace the ribbon cartridge.",
    8301: "Cut path is outside the printer's cut range - reduce or move the cut path and retry.",
    8302: "Printer rejected the PLT cut data - simplify the cut path and retry.",
    8303: "Printer timed out while processing PLT cut data - simplify the cut path and retry.",
    8401: "Cutter media sensor jam - power off the printer and clear the cutter path.",
    8402: "Cutter initialization paper jam - power off the printer and clear the media path.",
    8403: "Cutter found media in the path during initialization - remove the media and retry.",
    8404: "Cutter failed to return home - power off the printer and check the cutter path.",
    8405: "Cutter paper handoff failed - power off the printer and clear the media path.",
    8406: "Cutter failed to pick paper - reload the media and retry.",
    8407: "Cutter failed to pick paper - reload the media and retry.",
    8408: "Cutter failed to find home - power off the printer and check the cutter path.",
    8409: "Cutter feed motor stalled - power off the printer and clear the media path.",
    8410: "Cutter carriage motor stalled - power off the printer and check the cutter path.",
    8411: "Cutter paper eject failed - power off the printer and remove the media.",
    8412: "Cutter sensor jam - power off the printer and clear the cutter path.",
    8413: "Cutter motor stalled - power off the printer and check the cutter path.",
    9002: "Paper tray battery is very low - connect power before retrying.",
    9003: "Paper tray battery charge is very low - connect power before retrying.",
    9004: "Paper tray battery temperature is high - let the printer cool before retrying.",
    9005: "Paper tray battery temperature is low - let the printer warm up before retrying.",
}

ERROR_SUB_STATE_CODES: Dict[int, str] = {
    5000: "Printer mechanism jam or internal fault - power off the printer and clear any jammed paper or ribbon before retrying.",
    6000: "Printer entered a hardware error state - power off the printer and clear any jammed paper or ribbon before retrying.",
}

# Conditions where the printer can normally resume after the user fixes the issue.
RECOVERABLE_ERROR_CODES = {
    5001, 5306, 5401, 5402, 5417, 5418, 5419, 5420,
    5506, 8101, 8102, 8104, 8106,
}


import re as _re


def _extract_alert_codes(alerts) -> set:
    """
    Parse printer-state-alerts into a set of integer error codes.
    Handles wire format "::8102", plain integers, lists, and bare strings.
    """
    if alerts is None:
        return set()
    candidates = alerts if isinstance(alerts, list) else [alerts]
    codes = set()
    for val in candidates:
        # Try direct integer conversion first.
        try:
            codes.add(int(val))
            continue
        except (TypeError, ValueError):
            pass
        # Extract all digit runs (handles "::8102", "::0", etc.).
        for m in _re.finditer(r"\d+", str(val)):
            n = int(m.group())
            if n > 0:  # skip 0 — "::0" is the normal no-alert sentinel
                codes.add(n)
    return codes


def _describe_alerts(alerts) -> str:
    """
    Return a human-readable description for printer-state-alerts value(s).
    Returns empty string if nothing recognizable.
    """
    codes = _extract_alert_codes(alerts)
    parts = []
    for code in sorted(codes):
        desc = PRINTER_ERROR_CODES.get(code)
        if desc:
            parts.append(f"Error {code}: {desc}")
        else:
            parts.append(f"Error {code}: (unknown code — please document)")
    return "  ".join(parts)


@dataclass
class JobConfig:
    media_size: int = 5013
    media_type: int = 2030
    copies: int = 1
    quality: int = 4
    job_type: int = 600
    channel: int = 14864
    user_account: str = "12345678"
    jpg_timeout_s: int = 180
    plt_timeout_s: int = 100
    poll_interval: float = 2.0  # seconds between status polls after upload
    max_poll_s: Optional[int] = None  # overall poll timeout; None = unlimited
    max_idle_polls: int = 0  # optional stop if printer returns to idle repeatedly (0 disables)
    uuid: Optional[str] = None
    hash_method: int = 1  # 1 observed in captures (SHA1-length hashes)
    hash_value: Optional[str] = None  # override print hash
    cut_hash_value: Optional[str] = None  # override cut hash
    chunk_delay_ms: int = 120  # optional delay between chunk sends (ms); native captures ~120ms
    extlen: int = 4075  # chunk EXTLEN (full 4096 transfer with header; tail chunk may be smaller)
    ack_timeout_s: float = 10.0  # timeout waiting for chunk OK
    heartbeat_interval_s: float = 5.0  # keep-alive ping interval


def _first_result(result_obj):
    if isinstance(result_obj, list) and result_obj:
        return result_obj[0]
    return result_obj


def _scalar_string(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)
    return str(value).strip()


def _int_value(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _describe_error_sub_state(value) -> str:
    return ERROR_SUB_STATE_CODES.get(_int_value(value), "")


def _extract_job_ids(value) -> list[int]:
    """Extract positive job IDs from the response shapes seen from get-job-id-list."""
    ids: list[int] = []

    def add(v) -> None:
        if v is None:
            return
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            n = int(v)
            if n > 0:
                ids.append(n)
            return
        if isinstance(v, str):
            for part in v.split(";"):
                part = part.strip()
                if part.isdigit() and int(part) > 0:
                    ids.append(int(part))
            return
        if isinstance(v, (list, tuple)):
            for item in v:
                add(item)
            return
        if isinstance(v, dict):
            for key in ("job-id-list", "job_id_list", "job-id", "job_id", "result"):
                if key in v:
                    add(v[key])
            return

    add(value)
    # Preserve device order while removing duplicates.
    return list(dict.fromkeys(ids))


PRINTER_RASTER_SIZES = {
    # media-size: (logical content raster, firmware raster)
    5013: ((1200, 2100), (1216, 2128)),  # 4x7 cut-capable sticker stock
    5012: ((1200, 1800), (1216, 1828)),  # 4x6 print-only photo stock
}


def _prepare_jpg_for_printer(jpg_bytes: bytes, media_size: int) -> bytes:
    """
    Pad a nominal 300-DPI sheet raster into the printer's real raster space.

    4x7: 1200x2100 -> 1216x2128 (8 px L/R, 14 px T/B)
    4x6: 1200x1800 -> 1216x1828

    Already-padded JPEGs are returned byte-for-byte. Unknown dimensions are
    left untouched so custom/research workflows are not silently rescaled.
    """
    spec = PRINTER_RASTER_SIZES.get(media_size)
    if not spec:
        return jpg_bytes

    from PIL import Image

    logical_size, padded_size = spec
    with Image.open(io.BytesIO(jpg_bytes)) as src:
        if src.size == padded_size:
            return jpg_bytes
        if src.size != logical_size:
            log.warning(
                "JPEG dimensions %sx%s do not match expected logical %sx%s or padded %sx%s raster for media-size %s; sending unchanged.",
                src.width, src.height,
                logical_size[0], logical_size[1],
                padded_size[0], padded_size[1],
                media_size,
            )
            return jpg_bytes

        rgb = src.convert("RGB")
        padded = Image.new("RGB", padded_size, (255, 255, 255))
        padded.paste(rgb, (8, 14))

        smallest = None
        for quality in (95, 92, 90, 85, 80, 75, 70, 65, 60, 55, 50, 45):
            out = io.BytesIO()
            padded.save(out, format="JPEG", quality=quality)
            data = out.getvalue()
            smallest = data
            if len(data) <= MAX_JPG_BYTES:
                log.info(
                    "Padded JPEG from %sx%s to %sx%s for printer registration (quality=%s, %s bytes).",
                    logical_size[0], logical_size[1], padded_size[0], padded_size[1], quality, len(data),
                )
                return data
        return smallest if smallest is not None else jpg_bytes


def _completion_confirmed(
    *, print_only: bool, cut_started: bool, completion_reported: bool, idle_polls: int
) -> bool:
    return completion_reported and idle_polls >= 3 and (print_only or cut_started)


class PixcutClient:
    """
    Client orchestrates combo-job submission and polling.
    """

    def __init__(
        self,
        transport: USBTransport,
        logger: SessionLogger,
        request_ids: Optional[Dict[str, int]] = None,
        job_cfg: Optional[JobConfig] = None,
        start_id: Optional[int] = None,
        id_strategy: str = "monotonic",
    ):
        self.transport = transport
        self.logger = logger
        self.verbose = getattr(logger, "keep_json", True)
        # Default to captured ids from USB sniff.
        self.ids = request_ids or {
            "combo": 1234,
            "job-info": 123,
            "props": 124,
            "big-data": 125,
        }
        self._req_counter = start_id if start_id is not None else (int(time.time()) & 0xFFFF)
        self.job_cfg = job_cfg or JobConfig()
        self.id_strategy = id_strategy
        self._lock = threading.RLock()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = None

    def _human_job_state(self, code):
        mapping = {
            1: "Waiting",
            2: "Start",
            3: "Processing",
            4: "Processing (Held)",
            5: "Pending",
            6: "Terminating",
            7: "Aborted",
            8: "Cancelled",
            9: "Completed",
        }
        try:
            c = int(code)
        except Exception:
            return str(code)
        return mapping.get(c, str(code))

    def _human_job_sub_state(self, code):
        mapping = {
            1000: "Waiting",
            2000: "Start",
            3000: "Processing",
            3001: "Downloading",
            3002: "Uploading",
            3003: "Cloud Rendering",
            3004: "Local Rendering",
            3005: "Printing",
            4000: "Held",
            5000: "Pending",
            6000: "Terminating",
            7000: "Aborted",
            8000: "Cancelled",
            9000: "Completed",
        }
        try:
            c = int(code)
        except Exception:
            return str(code)
        return mapping.get(c, str(code))

    def _human_printer_state(self, code):
        mapping = {
            10: "Initializing",
            20: "Idle",
            30: "Sleep",
            40: "Processing",
            50: "Off",
            60: "Error",
        }
        try:
            c = int(code)
        except Exception:
            return str(code)
        return mapping.get(c, str(code))

    def _human_printer_sub_state(self, code):
        mapping = {
            1000: "Init",
            2000: "Idle",
            3001: "Printing",
            3002: "File Transferring",
            3006: "Cancelling",
            3007: "Upgrading",
            3008: "Calibrating",
            3009: "Semi-auto Printing",
            3010: "Semi-auto Scan Required",
            3011: "Semi-auto Scanning",
            3012: "Scan Waiting",
            3013: "Copy Waiting",
            3014: "Rendering",
            3015: "Initializing",
            3016: "Decoding",
            3017: "Loading Paper",
            3018: "Printing Yellow",
            3019: "Printing Magenta",
            3020: "Printing Cyan",
            3021: "Printing OC",
            3022: "Preheating",
            3023: "Cooldown",
            3024: "Cleaning",
            3025: "Home Feed",
            3026: "Ejecting Paper",
            3027: "SmartSheet",
            3028: "Cut Pick",
            3029: "Cut Home",
            3030: "Cutting",
            3031: "Cut Eject",
            4002: "Normal",
            5002: "Not Real Off",
            6000: "Error",
        }
        try:
            c = int(code)
        except Exception:
            return str(code)
        return mapping.get(c, str(code))

    def open(self):
        self.transport.open()

    def close(self):
        self.transport.close()

    # --- low-level helpers -------------------------------------------------
    def _send_json(self, obj: dict, expect_response: bool = True, quiet: bool = False) -> Optional[dict]:
        with self._lock:
            if obj.get("id") is None:
                self._req_counter += 1
                obj = {**obj, "id": self._req_counter}
            self.ids["last_req_id"] = obj.get("id")
            raw = encode_json_command(obj)
            self.logger.log_json("out", obj)
            self.logger.log_bytes("out", f"id{obj.get('id','noid')}-json", raw)
            ep_out = self.transport.cfg.out_ep
            if self.verbose and not quiet:
                log.info("JSON OUT ep=0x%02x len=%d", ep_out, len(raw))
            self.transport.write(raw)

            if not expect_response:
                return None

            buf = bytearray()
            start = time.time()
            while True:
                chunk = self.transport.read(4096)
                if chunk:
                    if self.verbose and not quiet:
                        log.info("JSON IN  ep=0x%02x len=%d", self.transport.cfg.in_ep, len(chunk))
                    buf.extend(chunk)
                    # Parse as many JSON objects as possible from buffer (consuming each)
                    while True:
                        parsed = parse_balanced_json(buf, 0)
                        if not parsed:
                            break
                        resp_obj, end = parsed
                        self.logger.log_json("in", resp_obj)
                        self.logger.log_bytes("in", f"id{resp_obj.get('id','?') or resp_obj.get('method','?')}-json", buf[:end])
                        buf = buf[end:]
                        if resp_obj.get("method") == "event.rpt_err":
                            log.error("device error event: %s", resp_obj)
                            return resp_obj
                        if not self.verbose and quiet:
                            continue
                        if resp_obj.get("id") == self.ids.get("last_req_id"):
                            return resp_obj
                if time.time() - start > 10:
                    raise TimeoutError("timed out waiting for JSON response")

    def _send_data_chunks(
        self,
        payload: bytes,
        chunk_extlen: int,
        job_id: int,
        job_cfg: Optional[JobConfig] = None,
        base_idx: int = 0,
        label_prefix: str = "data",
    ) -> int:
        delay_s = (job_cfg.chunk_delay_ms / 1000.0) if job_cfg and job_cfg.chunk_delay_ms else 0
        ack_timeout = job_cfg.ack_timeout_s if job_cfg else 10.0
        
        ep_out = self.transport.cfg.data_out_ep or self.transport.cfg.out_ep
        ep_in = self.transport.cfg.data_in_ep or self.transport.cfg.in_ep
        
        # Materialize chunks to a list so we can identify the final one.
        chunks = list(chunk_payload(
            payload,
            chunk_extlen=chunk_extlen,
            job_id=job_id,
        ))
        total = len(chunks)

        for i, packet in enumerate(chunks):
            idx = base_idx + i
            with self._lock:
                # Single write per chunk
                self.logger.log_bytes("out", f"{label_prefix}-{idx:04d}", packet)
                try:
                    self.transport.write(packet, endpoint=ep_out)
                    if self.verbose:
                        log.info("%s chunk %d: sent full packet ep=0x%02x len=%d", label_prefix, idx, ep_out, len(packet))
                except Exception as e:
                    log.error("write failed on chunk %d len=%d: %s", idx, len(packet), e)
                    raise
                # Read ack (device sometimes responds without newline; scan for OK token)
                ack_buf = bytearray()
                deadline = time.time() + ack_timeout
                while time.time() < deadline:
                    # Use remaining time for timeout to avoid generating cancellation errors in USB capture
                    time_left = deadline - time.time()
                    if time_left < 0.1:
                        time_left = 0.1
                    try:
                        chunk_in = self.transport.read(
                            4096,
                            timeout_ms=int(time_left * 1000),
                            endpoint=ep_in,
                        )
                    except Exception:
                        chunk_in = b""
                    if chunk_in:
                        ack_buf.extend(chunk_in)
                        
                        # Scan for interleaved JSON events (e.g. errors, heartbeats)
                        while True:
                            parsed = parse_balanced_json(ack_buf, 0)
                            if not parsed:
                                break
                            obj, end = parsed
                            self.logger.log_json("in", obj)
                            # If we caught an error event, raise immediately
                            if obj.get("method") == "event.rpt_err":
                                log.error("device error during data: %s", obj)
                                return base_idx + len(chunks)
                            # Remove the consumed JSON bytes from the buffer so we can find the ACK
                            del ack_buf[:end]

                        if b"OK" in ack_buf or b"ER" in ack_buf:
                            break

                    # Guard against buffer bloat if we can't parse (e.g. huge garbage)
                    if len(ack_buf) > 4096:
                        raise RuntimeError(f"buffer full waiting for chunk {idx} ACK: {ack_buf[:100]!r}...")

                ack_bytes = bytes(ack_buf)
                self.logger.log_bytes("in", f"{label_prefix}-ack-{idx:04d}", ack_bytes)
                if not ack_bytes:
                    raise RuntimeError(f"no ack for chunk {idx}")
                elif b"ER" in ack_bytes:
                    err_msg = ack_bytes.decode("utf-8", errors="replace").strip()
                    raise RuntimeError(f"device returned error for chunk {idx}: {err_msg}")
                elif b"OK" not in ack_bytes:
                    raise RuntimeError(f"unexpected ack for chunk {idx}: {ack_bytes!r}")
                else:
                    if self.verbose:
                        log.info("%s chunk %d: received ACK ep=0x%02x len=%d: %r", label_prefix, idx, ep_in, len(ack_bytes), ack_bytes)
                    else:
                        pct = int(((i + 1) / total) * 100) if total else 100
                        bar_len = 30
                        filled = int(bar_len * pct / 100)
                        bar = "#" * filled + " " * (bar_len - filled)
                        sys.stdout.write(f"\r{label_prefix.upper():<4} [{bar}] {pct:3d}% ({i+1}/{total})")
                        sys.stdout.flush()

            if delay_s:
                time.sleep(delay_s)

        if not self.verbose:
            sys.stdout.write("\n")
            sys.stdout.flush()

        return base_idx + len(chunks)

    def start_heartbeat(self, interval: float = 5.0):
        if self._heartbeat_thread:
            return
        self._heartbeat_stop.clear()

        def loop():
            log.debug("heartbeat thread started")
            while not self._heartbeat_stop.wait(interval):
                try:
                    self.ping_printer_state(quiet=True)
                except Exception as e:
                    log.debug("heartbeat ping failed: %s", e)
            log.debug("heartbeat thread stopped")

        self._heartbeat_thread = threading.Thread(target=loop, daemon=True)
        self._heartbeat_thread.start()

    def stop_heartbeat(self):
        if self._heartbeat_thread:
            self._heartbeat_stop.set()
            self._heartbeat_thread.join(timeout=2.0)
            self._heartbeat_thread = None

    # --- user-visible operations ------------------------------------------
    def _enforce_jpg_size(self, jpg_bytes: bytes):
        if len(jpg_bytes) > MAX_JPG_BYTES:
            raise ValueError(f"JPEG is {len(jpg_bytes)} bytes; device limit is ~1 MiB.")

    def create_print_job(self, jpg_path: Path, job_cfg: JobConfig) -> Tuple[int, dict]:
        jpg_bytes = _prepare_jpg_for_printer(Path(jpg_path).read_bytes(), job_cfg.media_size)
        self._enforce_jpg_size(jpg_bytes)
        default_hash = "26b8714aea78792854637621ad5cf1c4ed31ad1f"
        jpg_hash = job_cfg.hash_value or default_hash or hashlib.sha1(jpg_bytes).hexdigest()
        filename_base = secrets.token_hex(16)
        payload = {
            "method": "print-job",
            "params": {
                "media-size": job_cfg.media_size,
                "media-type": job_cfg.media_type,
                "job-type": job_cfg.job_type,
                "channel": job_cfg.channel,
                "file-size": len(jpg_bytes),
                "document-format": 9,
                "document-name": f"{filename_base}.jpg",
                "hash-method": job_cfg.hash_method,
                "hash-value": jpg_hash,
                "user-account": job_cfg.user_account,
                "job-send-time": int(time.time()),
                "copies": job_cfg.copies,
            },
        }
        if self.ids.get("combo") is not None:
            payload["id"] = self.ids["combo"]
        job_id = self.send_combo_payload(payload)
        info_resp = self.send_command({"method": "get-job-info", "params": {"job-id": job_id}})
        log.info("print job %d created, initial state: %s", job_id, _first_result(info_resp.get("result")))
        return job_id, {"jpg": jpg_bytes}

    def create_combo_job(
        self,
        jpg_path: Path,
        plt_path: Path,
        job_cfg: JobConfig,
    ) -> Tuple[int, dict]:
        jpg_bytes = _prepare_jpg_for_printer(Path(jpg_path).read_bytes(), job_cfg.media_size)
        plt_bytes = Path(plt_path).read_bytes()
        self._enforce_jpg_size(jpg_bytes)
        default_uuid = "f45d69a1da22727c26aa863ea657de8b32a501f2"
        default_hash = "26b8714aea78792854637621ad5cf1c4ed31ad1f"

        jpg_hash = job_cfg.hash_value or default_hash or hashlib.sha1(jpg_bytes).hexdigest()
        plt_hash = job_cfg.cut_hash_value or default_hash or hashlib.sha1(plt_bytes).hexdigest()
        combo_uuid = job_cfg.uuid or default_uuid
        filename_base = secrets.token_hex(16)

        combo_payload = {
            "method": "combo-job",
            "params": [
                {
                    "method": "print-job",
                    "params": {
                        "uuid": combo_uuid,
                        "job-url": "",
                        "print-quality": job_cfg.quality,
                        "copies": job_cfg.copies,
                        "document-name": f"{filename_base}.jpg",
                        "file-size": len(jpg_bytes),
                        "document-format": 9,
                        "hash-method": job_cfg.hash_method,
                        "hash-value": jpg_hash,
                        "media-size": job_cfg.media_size,
                        "media-type": job_cfg.media_type,
                        "user-account": job_cfg.user_account,
                        "job-type": job_cfg.job_type,
                        "channel": job_cfg.channel,
                        "timeout": job_cfg.jpg_timeout_s,
                    },
                },
                {
                    "method": "cut-job",
                    "params": {
                        "file-size": len(plt_bytes),
                        "document-format": 18,
                        "document-name": f"{filename_base}.plt",
                        "job-url": "",
                        "hash-method": job_cfg.hash_method,
                        "hash-value": plt_hash,
                        "timeout": job_cfg.plt_timeout_s,
                    },
                },
            ],
        }
        if self.ids.get("combo") is not None:
            combo_payload["id"] = self.ids["combo"]
        total_size = len(jpg_bytes) + len(plt_bytes)
        if self.verbose:
            log.info("Submitting combo-job (%d bytes total)…", total_size)
        else:
            log.info("Combo Print & Cut job submitted to printer, awaiting response…")
        job_id = self.send_combo_payload(combo_payload)
        info_resp = self.send_command({"method": "get-job-info", "params": {"job-id": job_id}})
        if self.verbose:
            log.info("job %d created, initial state: %s", job_id, _first_result(info_resp.get("result")))
        else:
            log.info("Job assigned ID %s with file size %d. Beginning to print…", job_id, len(jpg_bytes))

        return job_id, {"jpg": jpg_bytes, "plt": plt_bytes}

    def send_combo_payload(self, payload: dict) -> int:
        """
        Send an already-formed combo-job payload (e.g., captured JSON) and return job_id.
        """
        resp = self._send_json(payload, expect_response=True)
        if not resp or "result" not in resp:
            raise RuntimeError(f"combo-job failed: {resp}")
        result = resp["result"]
        # Device may reject the job immediately with an error-code instead of a job_id.
        if isinstance(result, dict) and "error-code" in result and "job_id" not in result:
            ec = result["error-code"]
            desc = PRINTER_ERROR_CODES.get(int(ec), "") if ec is not None else ""
            msg = f"printer rejected job (error-code={ec})"
            if desc:
                msg += f": {desc}"
            raise RuntimeError(msg)
        job_id = result.get("job_id") if isinstance(result, dict) else result[0].get("job_id")
        if job_id is None:
            raise RuntimeError(f"no job_id in response: {resp}")
        return job_id

    def upload_documents(self, payloads: Dict[str, bytes], job_id: Optional[int] = None) -> None:
        # Upload PLT first (matches observed order), then JPG.
        idx = 0
        if "plt" in payloads:
            idx = self._send_data_chunks(
                payloads["plt"],
                chunk_extlen=self.job_cfg.extlen,
                job_cfg=self.job_cfg,
                base_idx=idx,
                label_prefix="plt",
                job_id=job_id,
            )
        if "jpg" in payloads:
            idx = self._send_data_chunks(
                payloads["jpg"],
                chunk_extlen=self.job_cfg.extlen,
                job_cfg=self.job_cfg,
                base_idx=idx,
                label_prefix="jpg",
                job_id=job_id,
            )

    def get_job_ids(self) -> list[int]:
        resp = self._send_json(
            {"method": "get-job-id-list", "params": {}},
            expect_response=True,
        ) or {}
        return _extract_job_ids(resp.get("result"))

    def discover_active_job_id(self) -> Optional[int]:
        ids = self.get_job_ids()
        return ids[0] if ids else None

    def cancel_job(self, job_id: int) -> dict:
        return self._send_json(
            {"method": "cancel-job", "params": {"job-id": int(job_id)}},
            expect_response=True,
        ) or {}

    def confirm_job(self, job_id: int) -> dict:
        # The working USB print-only flow uses the underscore spelling.
        return self._send_json(
            {"method": "confirm_job", "params": {"job-id": int(job_id)}},
            expect_response=True,
        ) or {}

    def poll_job(
        self,
        job_id: int,
        job_cfg: JobConfig,
        status_callback=None,
        *,
        print_only: bool = False,
    ) -> dict:
        poll_interval = job_cfg.poll_interval
        max_poll_s = job_cfg.max_poll_s
        start = time.time()
        last_state = None
        last_transfer = None
        last_cut = (None, None)
        last_line_len = 0
        seen_busy = False
        cut_started = False
        completion_reported = False
        completion_idle_polls = 0
        unconfirmed_idle_polls = 0
        in_alert_state = False
        announced_codes = set()
        missed_job_polls = 0
        missed_prop_polls = 0
        info = None
        props = None

        while True:
            props_req = {
                "method": "get-prop",
                "params": ["printer-state", "printer-sub-state", "printer-state-alerts"],
            }
            if self.ids.get("props") is not None:
                props_req["id"] = self.ids["props"]

            try:
                props_resp = self._send_json(props_req, expect_response=True)
                missed_prop_polls = 0
            except TimeoutError:
                missed_prop_polls += 1
                if missed_prop_polls < (10 if in_alert_state else 3):
                    time.sleep(poll_interval)
                    continue
                raise

            props = props_resp.get("result") if props_resp and props_resp.get("result") is not None else None
            printer_state = _scalar_string(props[0]) if isinstance(props, list) and props else None
            printer_sub_state = _int_value(props[1]) if isinstance(props, list) and len(props) > 1 else 0
            alerts = props[2] if isinstance(props, list) and len(props) > 2 else None
            alert_codes = _extract_alert_codes(alerts)
            recoverable = bool(alert_codes) and alert_codes.issubset(RECOVERABLE_ERROR_CODES)

            for code in sorted(alert_codes):
                if code not in announced_codes:
                    announced_codes.add(code)
                    prefix = "ACTION REQUIRED" if code in RECOVERABLE_ERROR_CODES else "PRINTER ALERT"
                    log.warning("%s - Error %s: %s", prefix, code, PRINTER_ERROR_CODES.get(code, "Unknown printer alert."))

            if printer_state == "60":
                desc = _describe_alerts(alerts) or _describe_error_sub_state(printer_sub_state)
                if recoverable:
                    if not in_alert_state:
                        if not self.verbose:
                            sys.stdout.write("\n")
                            sys.stdout.flush()
                        log.warning(
                            "ACTION REQUIRED - printer paused (state=60 sub=%s alerts=%s): %s Waiting for device to resume...",
                            printer_sub_state,
                            alerts,
                            desc or "User action required.",
                        )
                        if status_callback:
                            status_callback(desc or f"Printer paused (alerts={alerts})")
                    in_alert_state = True
                    time.sleep(poll_interval)
                    continue

                if not self.verbose:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                log.error(
                    "Printer entered non-recoverable error state (state=60 sub=%s alerts=%s)%s",
                    printer_sub_state,
                    alerts,
                    f": {desc}" if desc else "",
                )
                return {
                    "last_info": info,
                    "last_props": props,
                    "error": "printer_error",
                    "alerts": alerts,
                }

            if in_alert_state:
                in_alert_state = False
                announced_codes.clear()
                log.info("Printer alert cleared; resuming job polling.")

            if printer_state == "40":
                seen_busy = True
                unconfirmed_idle_polls = 0
                completion_idle_polls = 0
            elif printer_state == "20":
                if completion_reported:
                    completion_idle_polls += 1
                elif seen_busy:
                    unconfirmed_idle_polls += 1

            if 3028 <= printer_sub_state <= 3031:
                cut_started = True

            info_req = {
                "method": "get-job-info",
                "params": {"job-id": job_id},
            }
            if self.ids.get("job-info") is not None:
                info_req["id"] = self.ids["job-info"]

            try:
                info_resp = self._send_json(info_req, expect_response=True)
                missed_job_polls = 0
            except TimeoutError:
                missed_job_polls += 1
                if missed_job_polls >= 3:
                    try:
                        recovered_id = self.discover_active_job_id()
                    except Exception as exc:
                        log.warning("get-job-id-list recovery failed: %s", exc)
                        recovered_id = None
                    if recovered_id and recovered_id != job_id:
                        log.warning("Recovered active job-id %s; replacing stale job-id %s.", recovered_id, job_id)
                        job_id = recovered_id
                        missed_job_polls = 0
                        continue
                if _completion_confirmed(
                    print_only=print_only,
                    cut_started=cut_started,
                    completion_reported=completion_reported,
                    idle_polls=completion_idle_polls,
                ):
                    break
                if missed_job_polls < 3 or printer_state == "40":
                    time.sleep(poll_interval)
                    continue
                raise

            raw_info = _first_result(info_resp.get("result")) if info_resp and info_resp.get("result") else None
            if isinstance(raw_info, dict) and isinstance(raw_info.get("info"), dict):
                info = raw_info["info"]
            else:
                info = raw_info if isinstance(raw_info, dict) else None

            if info is None:
                if completion_reported and printer_state == "20" and completion_idle_polls >= 3:
                    if print_only or cut_started:
                        break
                    return {
                        "last_info": None,
                        "last_props": props,
                        "error": "unconfirmed_completion",
                    }
                time.sleep(poll_interval)
                continue

            job_state = _int_value(info.get("job-state"))
            job_sub_state = _int_value(info.get("job-sub-state"))
            cut_prog = _int_value(info.get("cutting-progress"))
            cut_total = _int_value(info.get("cut-contours"))
            transfer_status = _int_value(info.get("transfer-status"), default=-1)

            if 3028 <= job_sub_state <= 3031 or cut_total > 0:
                cut_started = True

            state = (job_state, job_sub_state, printer_state, printer_sub_state)
            transfer = (info.get("transfer-status"), info.get("transfer-size"))
            cut_tuple = (info.get("cutting-progress"), info.get("cut-contours"))

            if state != last_state or transfer != last_transfer or cut_tuple != last_cut:
                if self.verbose:
                    self.logger.log_text_block(
                        "job status",
                        json.dumps({"info": info, "props": props}, indent=2),
                        log,
                    )
                else:
                    cut_pct = int((cut_prog / cut_total) * 100) if cut_total else 0
                    readable = {
                        "job-state": self._human_job_state(job_state),
                        "job-sub": self._human_job_sub_state(job_sub_state),
                        "printer": self._human_printer_state(printer_state),
                        "printer-sub": self._human_printer_sub_state(printer_sub_state),
                        "cut": f"{cut_prog}/{cut_total} ({cut_pct}%)" if cut_total else "",
                    }
                    line = "Status: {job-state} / {job-sub} | Printer: {printer} / {printer-sub} | cut={cut}".format(**readable)
                    pad = " " * max(0, last_line_len - len(line))
                    sys.stdout.write(f"\r{line}{pad}")
                    sys.stdout.flush()
                    last_line_len = len(line)
                last_state = state
                last_transfer = transfer
                last_cut = cut_tuple

            if job_state == 8 or (job_state == 7 and job_sub_state == 7000):
                if not self.verbose:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                return {"last_info": info, "last_props": props, "error": "cancelled"}

            if transfer_status == 3 and job_state != 1:
                if not self.verbose:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                log.error("Job transfer failed (transfer-status=3).")
                return {"last_info": info, "last_props": props, "error": "transfer_failed", "alerts": alerts}

            if job_state == 7:
                if not self.verbose:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                desc = _describe_alerts(alerts) or _describe_error_sub_state(job_sub_state)
                if desc:
                    log.error("Job failed: %s", desc)
                return {"last_info": info, "last_props": props, "error": "job_error", "alerts": alerts}

            if job_state == 9:
                completion_reported = True
                if printer_state == "20":
                    completion_idle_polls = max(1, completion_idle_polls)

            if _completion_confirmed(
                print_only=print_only,
                cut_started=cut_started,
                completion_reported=completion_reported,
                idle_polls=completion_idle_polls,
            ):
                if not self.verbose:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                break

            if printer_state == "20" and seen_busy and not completion_reported and unconfirmed_idle_polls >= 3:
                if not self.verbose:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                log.error("Printer returned to idle without confirming job completion.")
                return {"last_info": info, "last_props": props, "error": "unconfirmed_completion"}

            if completion_reported and printer_state == "20" and completion_idle_polls >= 3 and not print_only and not cut_started:
                if not self.verbose:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                log.error("Printer reported completion but no physical cut phase was observed.")
                return {"last_info": info, "last_props": props, "error": "unconfirmed_cut"}

            if job_cfg.max_idle_polls and unconfirmed_idle_polls >= job_cfg.max_idle_polls:
                raise TimeoutError(
                    f"printer returned to idle {unconfirmed_idle_polls} times without job completion; last_info={info}"
                )

            if max_poll_s and (time.time() - start) > max_poll_s:
                return {"last_info": info, "last_props": props, "error": "poll_timeout"}

            time.sleep(poll_interval)

        big_req = {"method": "get-prop", "params": ["big-data"]}
        if self.ids.get("big-data") is not None:
            big_req["id"] = self.ids["big-data"]
        big_resp = self._send_json(big_req, expect_response=True)
        return {"last_info": info, "last_props": props, "big_data": big_resp}

    def send_command(self, obj: dict) -> dict:
        """
        Public wrapper to send an arbitrary JSON command and return its response.
        """
        resp = self._send_json(obj, expect_response=True)
        if resp is None:
            raise RuntimeError("no response received")
        return resp

    def ping_printer_state(self, quiet: bool = False) -> dict:
        req = {
            "method": "get-prop",
            "params": ["printer-state", "printer-sub-state", "printer-state-alerts"],
        }
        return self._send_json(req, expect_response=True, quiet=quiet) or {}

    def preflight(self) -> None:
        """
        Warm up identity/state and refuse a new job when the printer is busy
        or already in an error state.
        """
        seq = [
            {"id": 101, "method": "get-prop", "params": ["firmware-revision", "hardware-revision", "model", "sku"]},
            {"id": 102, "method": "get-prop", "params": ["serial-number", "mac-address", "bt-phone-mac", "sn-pcba", "media-size", "auto-off-interval"]},
            {"id": 103, "method": "get-prop", "params": ["big-data"]},
        ]
        for req in seq:
            try:
                self._send_json(req, expect_response=True)
            except Exception as e:
                log.warning("preflight request %s failed: %s", req.get("id"), e)

        try:
            state_resp = self.ping_printer_state()
            props = state_resp.get("result") if state_resp else None
            printer_state = _scalar_string(props[0]) if isinstance(props, list) and props else None
            printer_sub_state = _int_value(props[1]) if isinstance(props, list) and len(props) > 1 else 0
            alerts = props[2] if isinstance(props, list) and len(props) > 2 else None

            if printer_state == "40":
                try:
                    active_job = self.discover_active_job_id()
                except Exception:
                    active_job = None
                suffix = f" (active job-id={active_job})" if active_job else ""
                raise RuntimeError(f"Printer is busy{suffix}. Wait for the current job to finish or cancel it before starting another.")

            if printer_state != "60":
                return

            codes = _extract_alert_codes(alerts)
            desc = _describe_alerts(alerts) or _describe_error_sub_state(printer_sub_state)
            if codes and codes.issubset(RECOVERABLE_ERROR_CODES):
                raise RuntimeError(
                    f"Printer is paused and needs user action before a new job can start"
                    f"{': ' + desc if desc else ''}."
                )
            raise RuntimeError(
                f"Printer is in an error state and cannot accept jobs"
                f"{': ' + desc if desc else f' (sub={printer_sub_state} alerts={alerts})'}."
            )
        except RuntimeError:
            raise
        except Exception as e:
            log.warning("preflight state check failed: %s", e)



def run_job_session(
    transport: USBTransport,
    logger: SessionLogger,
    jpg_path: Optional[Path] = None,
    plt_path: Optional[Path] = None,
    job_cfg: Optional[JobConfig] = None,
    request_ids: Optional[Dict[str, int]] = None,
    start_id: Optional[int] = None,
    sequential_ids: bool = False,
    id_strategy: str = "monotonic",
    mode: str = "combo",
    status_callback=None,
) -> dict:
    job_cfg = job_cfg or JobConfig()
    # If sequential ids requested with fixed strategy, make non-combo ids None so they auto-increment from start_id (combo id).
    if sequential_ids and id_strategy == "fixed":
        base_ids = {"combo": request_ids.get("combo") if request_ids else None}
        request_ids = base_ids
    client = PixcutClient(
        transport,
        logger,
        request_ids=request_ids,
        job_cfg=job_cfg,
        start_id=start_id,
        id_strategy=id_strategy,
    )
    client.open()
    try:
        if job_cfg.heartbeat_interval_s > 0:
            client.start_heartbeat(job_cfg.heartbeat_interval_s)

        client.preflight()
        if mode == "print":
            if jpg_path is None:
                raise ValueError("print mode requires --jpg")
            job_id, payloads = client.create_print_job(jpg_path, job_cfg)
        else:
            if jpg_path is None or plt_path is None:
                raise ValueError("combo mode requires --jpg and --plt/--svg")
            job_id, payloads = client.create_combo_job(jpg_path, plt_path, job_cfg)
        log.info("job-id assigned: %s", job_id)
        client.upload_documents(payloads, job_id=job_id)
        if mode == "print":
            client.confirm_job(job_id)
        try:
            result = client.poll_job(
                job_id,
                job_cfg,
                status_callback=status_callback,
                print_only=(mode == "print"),
            )
        except KeyboardInterrupt:
            try:
                client.cancel_job(job_id)
                log.warning("Cancellation requested for job-id %s.", job_id)
            finally:
                raise
        except RuntimeError as e:
            log.error("job polling aborted: %s", e)
            result = {"error": str(e)}
        log.info("job completed: %s", result.get("last_info"))
        return {"job_id": job_id, **result}
    finally:
        client.stop_heartbeat()
        client.close()
