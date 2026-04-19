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

from .framing import chunk_payload, encode_json_command, parse_balanced_json
from .logging_utils import SessionLogger, hexdump
from .transport import USBConfig, USBTransport

log = logging.getLogger("pixcut.orchestrator")
MAX_JPG_BYTES = 1024 * 1024  # ~1 MiB device limit observed

# Known printer error codes observed via reverse engineering.
# Wire format for printer-state-alerts is "::CODE" (e.g. "::8102").
PRINTER_ERROR_CODES: Dict[int, str] = {
    5306: "No paper/media cartridge installed — insert the paper cartridge and retry.",
    5401: "Paper cartridge is out of paper — refill or replace the paper cassette and retry.",
    5414: "Media size mismatch — wrong paper stock loaded for this job type. The printer will physically jam; open the bottom panel to remove the paper, then power cycle and retry with the correct stock.",
    8011: "Printer rejected job — likely in an error state. Power it off and back on, then retry.",
    8101: "Ink/ribbon cartridge empty — replace the cartridge.",
    8102: "No ink ribbon installed — insert a ribbon cartridge and retry.",
}

# Error codes that represent recoverable consumable issues.
# The device will auto-resume once the user addresses the problem, so we keep polling.
RECOVERABLE_ERROR_CODES = {5306, 5401, 8101, 8102}


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
    poll_interval: float = 10.0  # seconds between status polls after upload
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
        jpg_bytes = Path(jpg_path).read_bytes()
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
        jpg_bytes = Path(jpg_path).read_bytes()
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

    def poll_job(self, job_id: int, job_cfg: JobConfig, status_callback=None) -> dict:
        poll_interval = job_cfg.poll_interval
        max_poll_s = job_cfg.max_poll_s
        start = time.time()
        last_state = None
        idle_polls = 0
        seen_busy = False
        last_transfer = None
        seen_cartridge_empty = False
        last_cut = (None, None)
        last_line_len = 0
        while True:
            info_req = {
                "method": "get-job-info",
                "params": {"job-id": job_id},
            }
            props_req = {
                "method": "get-prop",
                "params": ["printer-state", "printer-sub-state", "printer-state-alerts"],
            }
            if self.ids.get("job-info") is not None:
                info_req["id"] = self.ids["job-info"]
            if self.ids.get("props") is not None:
                props_req["id"] = self.ids["props"]
            info_resp = self._send_json(info_req, expect_response=True)
            props_resp = self._send_json(props_req, expect_response=True)
            info = _first_result(info_resp.get("result")) if info_resp and info_resp.get("result") else None
            props = props_resp.get("result") if props_resp and props_resp.get("result") is not None else None
            if info is None:
                log.error("no job info returned; breaking poll loop")
                break
            printer_state = props[0] if isinstance(props, list) and props else None
            printer_sub_state = props[1] if isinstance(props, list) and len(props) > 1 else None
            alerts = props[2] if isinstance(props, list) and len(props) > 2 else None

            state = (
                info.get("job-state"),
                info.get("job-sub-state"),
                printer_state,
                printer_sub_state,
            )
            # Always log if state changes OR transfer-size/status changes OR cut progress changes
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
                    # Friendly status line
                    job_state = info.get("job-state")
                    job_sub = info.get("job-sub-state")
                    cut_prog = info.get("cutting-progress")
                    cut_total = info.get("cut-contours")
                    cut_pct = 0
                    try:
                        cut_pct = int((cut_prog / cut_total) * 100) if cut_prog is not None and cut_total else 0
                    except Exception:
                        cut_pct = 0
                    readable = {
                        "job-state": self._human_job_state(job_state),
                        "job-sub": self._human_job_sub_state(job_sub),
                        "printer": self._human_printer_state(printer_state),
                        "printer-sub": self._human_printer_sub_state(printer_sub_state) if printer_sub_state is not None else "",
                        "transfer": f"{transfer[1]} bytes" if transfer[1] else "",
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

            job_state = info.get("job-state")

            # Alert on any known error code in printer-state-alerts.
            if alerts and not seen_cartridge_empty:
                desc = _describe_alerts(alerts)
                if desc:
                    seen_cartridge_empty = True
                    log.warning(
                        "Printer alert (alerts=%s, job-state=%s): %s",
                        alerts,
                        job_state,
                        desc,
                    )
                    self.logger.log_text_block(
                        "printer alert",
                        f"Printer-state-alerts={alerts}, job-state={job_state}. {desc}",
                        log,
                    )

            if printer_state == "40":
                seen_busy = True
                idle_polls = 0
            elif printer_state == "20" and seen_busy and job_state != 9:
                idle_polls += 1
            if job_cfg.max_idle_polls and idle_polls >= job_cfg.max_idle_polls:
                raise TimeoutError(
                    f"printer returned to idle {idle_polls} times without job completion; last_info={info}"
                )
                # If idle guard disabled, allow loop to continue; we want to observe final state.

            # Break conditions: observed completion code, or stable idle after seeing busy.
            if info.get("job-state") == 9:
                if not self.verbose:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                break
            # Printer entered error state — check if it's a recoverable consumable issue.
            if printer_state == "60":
                alert_codes = _extract_alert_codes(alerts)
                desc = _describe_alerts(alerts)
                if alert_codes and alert_codes.issubset(RECOVERABLE_ERROR_CODES):
                    # Consumable issue — prompt user and keep polling; device will auto-resume.
                    if not self.verbose:
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                    log.warning(
                        "ACTION REQUIRED — printer paused (alerts=%s): %s  Waiting for device to resume...",
                        alerts,
                        desc,
                    )
                    if status_callback:
                        status_callback(desc)
                    # Don't break; fall through to sleep and re-poll.
                else:
                    # Unknown or fatal error — stop polling.
                    if not self.verbose:
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                    log.error(
                        "Printer entered error state (printer-state=60, alerts=%s)%s",
                        alerts,
                        f": {desc}" if desc else " — no known error code mapping; check raw alerts value above.",
                    )
                    return {"last_info": info, "last_props": props, "error": "printer_error", "alerts": alerts}
            # Treat job-state 7 (cancelled/failed) or transfer-status 3 as terminal error states.
            if info.get("job-state") == 7 or info.get("transfer-status") == 3:
                if not self.verbose:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                desc = _describe_alerts(alerts)
                if desc:
                    log.error("Job failed — printer alerts: %s", desc)
                return {"last_info": info, "last_props": props, "error": "job_error"}
            if printer_state == "20" and seen_busy and idle_polls >= 3:
                log.info("printer idle after busy; breaking poll loop. last_info=%s", info)
                break
            if max_poll_s and (time.time() - start) > max_poll_s:
                return {"last_info": info, "last_props": props, "error": "poll_timeout"}
            time.sleep(poll_interval)
        # big-data is typically queried after completion
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
        Send the initial identity/state queries seen in captures (ids ~101-116) to warm up.
        Raises RuntimeError if the printer is already in an error state.
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

        # Check printer state before attempting a job.
        try:
            state_resp = self.ping_printer_state()
            props = state_resp.get("result") if state_resp else None
            printer_state = props[0] if isinstance(props, list) and props else None
            alerts = props[2] if isinstance(props, list) and len(props) > 2 else None
            if printer_state == "60":
                desc = _describe_alerts(alerts)
                detail = f": {desc}" if desc else f" (alerts={alerts})"
                raise RuntimeError(
                    f"Printer is in an error state and cannot accept jobs{detail}. "
                    "Power the printer off and back on, then retry."
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
        try:
            result = client.poll_job(job_id, job_cfg, status_callback=status_callback)
        except RuntimeError as e:
            log.error("job polling aborted: %s", e)
            result = {"error": str(e)}
        log.info("job completed: %s", result.get("last_info"))
        return {"job_id": job_id, **result}
    finally:
        client.stop_heartbeat()
        client.close()
