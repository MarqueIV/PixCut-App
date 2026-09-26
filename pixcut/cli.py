import argparse
import json
import logging
import tempfile
from pathlib import Path
from typing import Optional
from textwrap import dedent
import json as jsonlib

from .logging_utils import (
    SessionLogger, default_log_dir, ensure_logging,
    CLR_RESET, CLR_GREEN, CLR_YELLOW, CLR_CYAN, CLR_BOLD,
)
from .orchestrator import JobConfig, PixcutClient, run_job_session
from .transport import USBConfig, USBTransport, discover_pixcut, list_all_devices
from .svg_to_plt import convert_svg_to_plt, save_plt

log = logging.getLogger("pixcut.cli")


def _build_job_config(args: argparse.Namespace) -> JobConfig:
    # Default job-type depends on mode unless explicitly set.
    inferred_job_type = args.job_type
    photo_mode = args.mode == "print"
    if inferred_job_type is None:
        inferred_job_type = 0 if photo_mode else 600
    media_size = args.media_size if args.media_size is not None else (5012 if photo_mode else 5013)
    media_type = args.media_type if args.media_type is not None else (2010 if photo_mode else 2030)
    return JobConfig(
        media_size=media_size,
        media_type=media_type,
        copies=args.copies,
        quality=args.quality,
        job_type=inferred_job_type,
        channel=args.channel,
        user_account=args.user_account,
        jpg_timeout_s=args.jpg_timeout,
        plt_timeout_s=args.plt_timeout,
        poll_interval=args.poll_interval,
        max_poll_s=(args.max_poll_seconds if args.max_poll_seconds > 0 else None),
        uuid=args.uuid,
        hash_method=args.hash_method,
        hash_value=args.hash_value,
        cut_hash_value=args.cut_hash_value,
        chunk_delay_ms=args.chunk_delay_ms,
        extlen=args.extlen,
        ack_timeout_s=args.ack_timeout,
        heartbeat_interval_s=args.heartbeat_interval,
    )


def _resolve_transport(args: argparse.Namespace):
    vid = int(args.vid, 16) if isinstance(args.vid, str) and args.vid else None
    pid = int(args.pid, 16) if isinstance(args.pid, str) and args.pid else None
    data_interface = getattr(args, "data_interface", None)
    data_out_ep = getattr(args, "data_out_ep", None)
    data_in_ep = getattr(args, "data_in_ep", None)
    if args.auto_detect:
        cfg = discover_pixcut(vid_hint=vid, pid_hint=pid)
    else:
        if vid is None or pid is None:
            raise RuntimeError("manual USB mode requires --vid and --pid")
        cfg = USBConfig(
            vid=vid,
            pid=pid,
            interface=args.interface if args.interface is not None else 0,
            out_ep=args.out_ep if args.out_ep is not None else 0x01,
            in_ep=args.in_ep if args.in_ep is not None else 0x81,
            timeout_ms=args.timeout_ms if args.timeout_ms is not None else 2000,
            data_interface=data_interface,
            data_out_ep=data_out_ep,
            data_in_ep=data_in_ep,
        )
    # Preserve explicit overrides for interface/eps if user set them.
    cfg.interface = args.interface if args.interface is not None else cfg.interface
    cfg.out_ep = args.out_ep if args.out_ep is not None else cfg.out_ep
    cfg.in_ep = args.in_ep if args.in_ep is not None else cfg.in_ep
    cfg.data_interface = data_interface if data_interface is not None else cfg.data_interface
    cfg.data_out_ep = data_out_ep if data_out_ep is not None else cfg.data_out_ep
    cfg.data_in_ep = data_in_ep if data_in_ep is not None else cfg.data_in_ep
    cfg.timeout_ms = args.timeout_ms if args.timeout_ms is not None else cfg.timeout_ms
    return USBTransport(cfg)

DEFAULT_PROP_SWEEP = [
    # identity/config (known, documented)
    "model",
    "sku",
    "serial-number",
    "mac-address",
    "bt-phone-mac",
    "bt-fw-ver",         # observed empty on USB; likely BT-only
    "firmware-revision",
    "hardware-revision",
    "sn-pcba",
    "media-size",        # returns {"media-size": code}
    "media-type",        # observed empty; unknown semantics
    # power/idle (mostly documented; some empty)
    "auto-off-interval", # known; 0 disables auto-off
    "auto-sleep-interval", # observed empty
    "off_interval",      # observed empty
    "sleep_interval",    # observed empty
    # state (known)
    "printer-state",
    "printer-sub-state",
    "printer-state-alerts",
    # counters/status
    "big-data",          # documented counters
    "ota-progress",      # returns {"progress": int}, meaning TBD
]

EXPERIMENTAL_PROP_SWEEP = [
    # Guesses / observed strings (mostly unknown; treat as research)
    "printer-status",          # unknown; maybe consolidated state
    "device-status",           # unknown
    "job-status",              # unknown
    "mixed-status",            # unknown
    "paper-size",              # unknown; paper descriptor?
    "total-jobs",              # guess: queue count
    "jobs-in-queue",           # guess: queue depth
    "job-queue",               # guess: queue object
    "job-mutex",               # guess: internal lock info
    "category",                # unknown (logging string)
    "sub-category",            # unknown (logging string)
    "battery",                 # maybe BT battery state
    "time-left",               # unknown; could be power timer
    "hw_error",                # error flag (also in big-data)
    "paper_error",             # error flag
    "paper_jam",               # error flag
    "paper_empty",             # error flag
    "paper_eject",             # error flag
    "carrier-offset-delta",    # unknown calibration value
    "carrier offset delta:%.3f", # format string; likely invalid as prop
    "carrier-resolution-ratio",  # unknown calibration value
    "carrier resolution ratio:%.3f", # format string; likely invalid as prop
    "paper-dynamic-offset-enable", # unknown flag
    "paper dynamic offset enable:%d", # format string; likely invalid as prop
    "paper-knife-down-pwm",    # unknown motor setting
    "paper knife down pwm:%.3f", # format string; likely invalid as prop
    "spec-type",               # unknown
    "ota-progress",            # duplicate of default; kept for completeness
    "miIO.get_ota_state",      # method-style; likely not a prop
    "get-ota-info",            # method-style; likely not a prop
    "device-info",             # known: returns identity object with underscores
    "device_info",             # underscore alias; same as above
    "job_info",                # method-style; likely not a prop
    "job_info_list",           # method-style; likely not a prop
]


def cmd_send(args: argparse.Namespace) -> None:
    ensure_logging(verbose=args.verbose)
    log_dir = Path(args.log_dir) if args.log_dir else (default_log_dir() if args.verbose else None)
    logger = SessionLogger(log_dir, keep_json=args.verbose)

    job_cfg = _build_job_config(args)

    transport = _resolve_transport(args)
    plt_path: Optional[Path] = Path(args.plt) if args.plt else None
    if args.svg:
        svg_path = Path(args.svg)
        plt_text = convert_svg_to_plt(
            svg_path,
            dpi=args.dpi,
            units_per_inch=args.units_per_inch,
            knife_pressure=args.kp,
            translate_x=args.tx,
            translate_y=args.ty,
            perf_cut_color=args.perf_color,
            perf_knife_pressure=args.perf_kp,
            perf_dash_mm=args.perf_dash,
            perf_gap_mm=args.perf_gap,
        )
        if log_dir is not None:
            plt_path = log_dir / "converted.plt"
        else:
            with tempfile.NamedTemporaryFile(suffix=".plt", delete=False) as tmp:
                plt_path = Path(tmp.name)
        save_plt(plt_text, plt_path)
        log.info("converted SVG %s -> PLT %s", svg_path, plt_path)

    if not args.jpg:
        raise ValueError("--jpg is required")
    if args.mode == "combo" and not plt_path:
        raise ValueError("combo mode requires --plt or --svg")

    try:
        result = run_job_session(
            transport=transport,
            logger=logger,
            jpg_path=Path(args.jpg) if args.jpg else None,
            plt_path=plt_path,
            job_cfg=job_cfg,
            request_ids={"combo": args.combo_id} if args.combo_id else None if args.id_strategy == "fixed" else None,
            start_id=(args.combo_id if args.sequential_ids else None),
            sequential_ids=args.sequential_ids,
            id_strategy=args.id_strategy,
            mode=args.mode,
        )
    except RuntimeError as e:
        log.error("\033[31m%s\033[0m", e)
        return
    if args.verbose:
        logger.log_text_block("final result", json.dumps(result, indent=2), log)
        print(f"{CLR_GREEN}done.{CLR_RESET} logs at {logger.describe()}")
    else:
        if result.get("error"):
            log.error("\033[31mJob error: %s\033[0m", result.get("error"))
            info = result.get("last_info")
            props = result.get("last_props")
            big = result.get("big_data")
            alerts = result.get("alerts")
            if alerts:
                log.error("printer-state-alerts: %s", alerts)
            if info:
                log.error("job-info: %s", info)
            if props:
                log.error("props: %s", props)
            if big:
                log.error("big-data: %s", big)
        else:
            log.info("Job complete! Exiting...")


def cmd_query(args: argparse.Namespace) -> None:
    ensure_logging(verbose=args.verbose)
    log_dir = Path(args.log_dir) if args.log_dir else (default_log_dir() if args.verbose else None)
    logger = SessionLogger(log_dir, keep_json=args.verbose)

    transport = _resolve_transport(args)
    client = PixcutClient(transport, logger)
    client.open()
    try:
        if args.identity:
            identity_props = [
                "model",
                "mac-address",
                "serial-number",
                "sn-pcba",
                "firmware-revision",
                "hardware-revision",
                "media-size",
                "auto-off-interval",
                "bt-phone-mac",
                "sku",
            ]
            req = {"id": args.req_id, "method": "get-prop", "params": identity_props}
        elif args.job_id is not None:
            req = {"id": args.req_id, "method": "get-job-info", "params": {"job-id": args.job_id}}
        elif args.props:
            req = {"id": args.req_id, "method": "get-prop", "params": args.props}
        else:
            req = {"id": args.req_id, "method": args.method, "params": json.loads(args.params)}
        for idx in range(args.repeat):
            resp = client.send_command(req)
            logger.log_text_block(
                f"query response #{idx+1}",
                json.dumps(resp, indent=2),
                log,
            )
            if idx + 1 < args.repeat:
                log.info("sleeping %.2fs before next query", args.interval)
                import time

                time.sleep(args.interval)
    finally:
        client.close()
    print(f"done. logs at {logger.describe()}")


def cmd_scan(args: argparse.Namespace) -> None:
    ensure_logging(verbose=False)
    print("Scanning USB bus for PixCut devices...")
    pixcut_found = False
    other_count = 0
    for vid, pid, description, is_pixcut in list_all_devices():
        if is_pixcut:
            pixcut_found = True
            print(f"  {CLR_BOLD}{CLR_CYAN}[PIXCUT]{CLR_RESET}  vid=0x{vid:04x} pid=0x{pid:04x}  {description}")
        else:
            other_count += 1
    if pixcut_found:
        print(f"  ({other_count} other USB device(s) not shown)")
    else:
        print(f"  {CLR_YELLOW}No PixCut device found{CLR_RESET} ({other_count} other USB device(s) present).")
        print("  Check that the printer is powered on and connected, and that libusb can see it.")


def cmd_printer(args: argparse.Namespace) -> None:
    ensure_logging(verbose=args.verbose)
    log_dir = Path(args.log_dir) if args.log_dir else (default_log_dir() if args.verbose else None)
    logger = SessionLogger(log_dir, keep_json=args.verbose)

    transport = _resolve_transport(args)
    client = PixcutClient(transport, logger)
    client.open()
    try:
        method = "pause-printer" if args.pause else "resume-printer"
        resp = client.send_command({"method": method, "params": []})
        logger.log_text_block(
            f"{method} response",
            json.dumps(resp, indent=2),
            log,
        )
    finally:
        client.close()
    print(f"done. logs at {logger.describe()}")


def cmd_job(args: argparse.Namespace) -> None:
    """List active printer jobs or cancel one by job id."""
    ensure_logging(verbose=args.verbose)
    log_dir = Path(args.log_dir) if args.log_dir else (default_log_dir() if args.verbose else None)
    logger = SessionLogger(log_dir, keep_json=args.verbose)

    transport = _resolve_transport(args)
    client = PixcutClient(transport, logger)
    client.open()
    try:
        if args.list_jobs:
            job_ids = client.get_job_ids()
            if job_ids:
                print("Active job ID(s): " + ", ".join(str(job_id) for job_id in job_ids))
            else:
                print("No active jobs reported by printer.")
        else:
            resp = client.cancel_job(args.cancel)
            logger.log_text_block(
                f"cancel-job {args.cancel} response",
                json.dumps(resp, indent=2),
                log,
            )
            print(json.dumps(resp, indent=2))
    finally:
        client.close()

    if args.verbose:
        print(f"done. logs at {logger.describe()}")


def cmd_convert(args: argparse.Namespace) -> None:
    """
    Offline SVG -> PLT converter; no USB access.
    """
    ensure_logging(verbose=args.verbose)
    svg_path = Path(args.svg)
    out_path = Path(args.out) if args.out else svg_path.with_suffix(".plt")
    plt_text = convert_svg_to_plt(
        svg_path,
        dpi=args.dpi,
        units_per_inch=args.units_per_inch,
        knife_pressure=args.kp,
        translate_x=args.tx,
        translate_y=args.ty,
        perf_cut_color=args.perf_color,
        perf_knife_pressure=args.perf_kp,
        perf_dash_mm=args.perf_dash,
        perf_gap_mm=args.perf_gap,
    )
    save_plt(plt_text, out_path)
    print(f"{CLR_GREEN}wrote PLT to{CLR_RESET} {out_path}")


def cmd_layout(args: argparse.Namespace) -> None:
    """
    Auto-trace cut paths from PNG/JPG sticker images and export a print-ready
    composite JPEG, a PLT cut file, and an SVG cut-path preview — without
    touching the printer.  Pass the outputs directly to `send --jpg ... --plt ...`.
    """
    ensure_logging(verbose=getattr(args, "verbose", False))
    from .image_to_cut import add_printer_margins, process_images

    img_paths = [Path(p) for p in args.images] * max(1, args.repeat)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        results = process_images(
            img_paths,
            dpi=args.dpi,
            margin_mm=args.margin,
            padding_mm=args.padding,
            left_margin_mm=args.left_margin,
            kp=args.kp,
            paginate=args.paginate,
            bg_white=args.bg_white,
            perf_cut=args.perf_cut,
            perf_kp=args.perf_kp,
            perf_dash_mm=args.perf_dash,
            perf_gap_mm=args.perf_gap,
        )
    except ImportError as e:
        log.error("%s", e)
        return
    except ValueError as e:
        log.error("%s", e)
        return

    multi = len(results) > 1
    for result in results:
        suffix = f"_{result.batch_index + 1}" if multi else ""
        jpg_path = out_dir / f"layout{suffix}.jpg"
        plt_path = out_dir / f"layout{suffix}.plt"
        svg_path = out_dir / f"layout{suffix}_cut.svg"

        add_printer_margins(result.composite).save(str(jpg_path), "JPEG", quality=95)
        plt_path.write_text(result.cut_plt, encoding="ascii")
        svg_path.write_text(result.cut_svg, encoding="utf-8")

        log.info(
            "Batch %d: %d sticker(s) placed → %s | %s | %s",
            result.batch_index + 1,
            len(result.placed),
            jpg_path,
            plt_path,
            svg_path,
        )
        for p in result.placed:
            log.info(
                "  %-30s  placed at (%d, %d)  %dx%d px",
                p.source_path.name, p.x, p.y, p.w, p.h,
            )
        if result.overflow_paths and not args.paginate:
            log.warning(
                "%d image(s) did not fit and were skipped: %s",
                len(result.overflow_paths),
                ", ".join(p.name for p in result.overflow_paths),
            )
            log.warning("Re-run with --paginate to create additional sheets.")

    if multi:
        log.info("Created %d sheet(s) total.", len(results))
    log.info(
        "To print+cut: pixcut_cli.py send --jpg %s --plt %s",
        out_dir / "layout.jpg",
        out_dir / "layout.plt",
    )


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def cmd_probe(args: argparse.Namespace) -> None:
    """
    Lightweight debugger to sweep get-prop and ad-hoc methods safely.
    """
    ensure_logging(verbose=args.verbose)
    log_dir = Path(args.log_dir) if args.log_dir else (default_log_dir() if args.verbose else None)
    logger = SessionLogger(log_dir, keep_json=args.verbose)

    transport = _resolve_transport(args)
    client = PixcutClient(transport, logger, id_strategy=args.id_strategy)
    client.open()
    try:
        # Property sweep
        props = []
        if args.properties:
            props.extend(args.properties)
        if args.all_properties:
            props.extend(p for p in DEFAULT_PROP_SWEEP if p not in props)
            if args.experimental:
                props.extend(p for p in EXPERIMENTAL_PROP_SWEEP if p not in props)
        props = [p for p in dict.fromkeys(props)]  # de-dupe while preserving order

        if props:
            log.info("probing %d properties", len(props))
            for batch in _chunks(props, args.prop_batch):
                req = {"method": "get-prop", "params": batch}
                resp = client.send_command(req)
                logger.log_text_block(
                    f"get-prop {batch}",
                    json.dumps(resp, indent=2),
                    log,
                )

        # Ad-hoc methods
        for method in args.methods or []:
            if method == "get-job-info" and args.job_id is None:
                log.warning("skipping get-job-info: --job-id not provided")
                continue
            payload = {"method": method, "params": {}}
            if method == "get-job-info":
                payload["params"] = {"job-id": args.job_id}
            if args.req_id is not None:
                payload["id"] = args.req_id
            resp = client.send_command(payload)
            logger.log_text_block(
                f"{method} response",
                json.dumps(resp, indent=2),
                log,
            )

        # Dangerous: set-prop (explicit opt-in)
        if args.set_prop:
            if not args.dangerous:
                raise RuntimeError("set-prop requested without --dangerous")
            payload_obj = {}
            for kv in args.set_prop:
                if "=" not in kv:
                    raise ValueError(f"--set-prop expects key=value, got {kv}")
                k, v = kv.split("=", 1)
                try:
                    v_parsed = jsonlib.loads(v)
                except Exception:
                    # Attempt int fallback, else keep as string
                    try:
                        v_parsed = int(v)
                    except ValueError:
                        v_parsed = v
                payload_obj[k] = v_parsed

            req = {"method": "set-prop", "params": payload_obj}
            if args.req_id is not None:
                req["id"] = args.req_id
            resp = client.send_command(req)
            logger.log_text_block(
                "set-prop response",
                json.dumps(resp, indent=2),
                log,
            )
    finally:
        client.close()
    if args.verbose:
        print(f"{CLR_GREEN}done.{CLR_RESET} logs at {logger.describe()}")

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--verbose", action="store_true", help="detailed logging with per-session JSON/RAW files")

    ap = argparse.ArgumentParser(description="PixCut CLI", parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)

    ap_send = sub.add_parser("send", help="run a combo job end-to-end", parents=[common])
    ap_send.add_argument("--mode", choices=["combo", "print"], default="combo", help="job mode: combo (print+cut, default) or print (photo only)")
    ap_send.add_argument("--jpg", help="path to JPG to print")
    ap_send.add_argument("--plt", help="path to PLT to cut")
    ap_send.add_argument("--svg", help="path to SVG to convert to PLT before sending")
    ap_send.add_argument("--dpi", type=float, default=96.0, help="assumed SVG DPI (default 96)")
    ap_send.add_argument("--units-per-inch", type=float, default=1016.0, help="PLT units/inch (default 1016)")
    # SVG->PLT tuning now uses internal defaults; no CLI overrides needed
    ap_send.add_argument("--kp", type=int, default=42, help="knife pressure KP value applied to kiss-cut paths (default 42 for Liene sticker media)")
    ap_send.add_argument("--tx", type=float, default=0.0, help="translate X")
    ap_send.add_argument("--ty", type=float, default=0.0, help="translate Y")
    ap_send.add_argument(
        "--perf-color", default="ff8800", metavar="HEX",
        help="stroke color (6-char hex, no #) marking perf-cut paths in --svg input (default: ff8800)",
    )
    ap_send.add_argument("--perf-kp", type=int, default=53, help="knife pressure for perf-cut paths in --svg (default: 53)")
    ap_send.add_argument("--perf-dash", type=float, default=8.0, metavar="MM", help="perf-cut dash length in mm (default: 8.0)")
    ap_send.add_argument("--perf-gap", type=float, default=0.05, metavar="MM", help="perf-cut gap between dashes in mm (default: 0.05)")
    ap_send.add_argument("--vid", help="USB vendor id hex (e.g. 0x302c)")
    ap_send.add_argument("--pid", help="USB product id hex (e.g. 0x3101)")
    ap_send.add_argument("--auto-detect", action="store_true", default=True, help="auto-detect PixCut device (default)")
    ap_send.add_argument("--no-auto-detect", dest="auto_detect", action="store_false", help="disable auto-detect")
    ap_send.add_argument("--interface", type=int, default=None, help="USB interface index (default: 0)")
    ap_send.add_argument(
        "--out-ep",
        type=lambda x: int(x, 0),
        default=None,
        help="bulk OUT endpoint address (accepts decimal or 0x-prefixed hex; default: 0x01)",
    )
    ap_send.add_argument(
        "--in-ep",
        type=lambda x: int(x, 0),
        default=None,
        help="bulk IN endpoint address (accepts decimal or 0x-prefixed hex; default: 0x81)",
    )
    ap_send.add_argument(
        "--data-interface",
        type=int,
        default=None,
        help="USB interface index for data plane (default: auto-detect or unset)",
    )
    ap_send.add_argument(
        "--data-out-ep",
        type=lambda x: int(x, 0),
        default=None,
        help="bulk OUT endpoint for data (accepts decimal or 0x-prefixed hex; default: auto-detect or unset)",
    )
    ap_send.add_argument(
        "--data-in-ep",
        type=lambda x: int(x, 0),
        default=None,
        help="bulk IN endpoint for data (accepts decimal or 0x-prefixed hex; default: auto-detect or unset)",
    )
    ap_send.add_argument(
        "--timeout-ms", type=int, default=None, help="USB read/write timeout"
    )
    ap_send.add_argument("--copies", type=int, default=1)
    ap_send.add_argument("--quality", type=int, default=4)
    ap_send.add_argument("--media-size", type=int, default=None, help="media-size code (default: 5012 for --mode print, 5013 for combo/cut)")
    ap_send.add_argument("--media-type", type=int, default=None, help="media-type code (default: 2010 for --mode print, 2030 for combo/cut)")
    ap_send.add_argument("--job-type", type=int, default=None, help="job-type override (default: auto based on mode)")
    ap_send.add_argument("--channel", type=int, default=14864)
    ap_send.add_argument("--user-account", default="12345678")
    ap_send.add_argument("--uuid", help="explicit UUID string for combo job (default: SHA1 of JPG)")
    ap_send.add_argument("--combo-id", type=int, default=1234, help="request id for combo-job (default 1234 to match captures)")
    ap_send.add_argument(
        "--id-strategy",
        choices=["monotonic", "fixed"],
        default="monotonic",
        help="message id strategy: monotonic (default) or fixed capture-style ids",
    )
    ap_send.add_argument(
        "--sequential-ids",
        action="store_true",
        help="after combo-job, auto-increment ids for later requests instead of fixed ids (matches capture cadence)",
    )
    ap_send.add_argument("--hash-method", type=int, default=1, help="hash-method value to send (default 1)")
    ap_send.add_argument("--hash-value", help="override hash-value for print-job (default: SHA1 of JPG)")
    ap_send.add_argument("--cut-hash-value", help="override hash-value for cut-job (default: SHA1 of PLT)")
    ap_send.add_argument("--jpg-timeout", type=int, default=180, help="jpg timeout seconds")
    ap_send.add_argument("--plt-timeout", type=int, default=100, help="plt timeout seconds")
    ap_send.add_argument(
        "--chunk-delay-ms",
        type=int,
        default=120,
        help="delay in milliseconds between data chunks (default 120ms, similar to native app pacing)",
    )
    ap_send.add_argument(
        "--extlen",
        type=int,
        default=4075,
        help="EXTLEN for chunks (default 4075; tail chunk will shrink as needed)",
    )
    ap_send.add_argument(
        "--ack-timeout",
        type=float,
        default=10.0,
        help="timeout in seconds waiting for chunk ACK (default 10.0)",
    )
    ap_send.add_argument(
        "--heartbeat-interval",
        type=float,
        default=5.0,
        help="seconds between background status pings (default 5.0)",
    )
    # preflight remains on by default; flag removed
    ap_send.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="seconds between status polls during job monitoring (default 2s)",
    )
    ap_send.add_argument(
        "--max-poll-seconds",
        type=int,
        default=0,
        help="overall poll timeout in seconds (0 means unlimited)",
    )
    ap_send.add_argument("--log-dir", type=Path, help="explicit log directory")
    ap_send.set_defaults(func=cmd_send)

    ap_query = sub.add_parser("query", help="send a single JSON command and print the response", parents=[common])
    ap_query.add_argument("--vid", help="USB vendor id hex (e.g. 0x302c)")
    ap_query.add_argument("--pid", help="USB product id hex (e.g. 0x3101)")
    ap_query.add_argument("--auto-detect", action="store_true", default=True, help="auto-detect PixCut device (default)")
    ap_query.add_argument("--no-auto-detect", dest="auto_detect", action="store_false", help="disable auto-detect")
    ap_query.add_argument("--interface", type=int, default=None, help="USB interface index (default: 0)")
    ap_query.add_argument(
        "--out-ep", type=int, default=None, help="bulk OUT endpoint address (default: 0x01)"
    )
    ap_query.add_argument(
        "--in-ep", type=int, default=None, help="bulk IN endpoint address (default: 0x81)"
    )
    ap_query.add_argument("--timeout-ms", type=int, default=None, help="USB read/write timeout")
    ap_query.add_argument(
        "--req-id",
        type=int,
        help="request id to send (default: auto-increment per request to avoid stale responses)",
    )
    ap_query.add_argument(
        "--props",
        nargs="+",
        help="send get-prop with these property names",
    )
    ap_query.add_argument(
        "--identity",
        action="store_true",
        help="fetch identity/config bundle (model, mac, serial, firmware, media-size, auto-off-interval, etc.)",
    )
    ap_query.add_argument("--job-id", type=int, help="send get-job-info for this job-id")
    ap_query.add_argument(
        "--method",
        default="get-prop",
        help="method name when not using --props/--job-id",
    )
    ap_query.add_argument(
        "--params",
        default="[]",
        help="JSON string for params when not using --props/--job-id",
    )
    ap_query.add_argument("--repeat", type=int, default=1, help="repeat query N times")
    ap_query.add_argument("--interval", type=float, default=1.0, help="seconds between repeats")
    ap_query.add_argument("--log-dir", type=Path, help="explicit log directory")
    ap_query.set_defaults(func=cmd_query)

    ap_scan = sub.add_parser("scan", help="list all visible USB devices", parents=[common])
    ap_scan.set_defaults(func=cmd_scan)

    ap_printer = sub.add_parser("printer", help="send pause or resume to printer", parents=[common])
    ap_printer.add_argument("--vid", help="USB vendor id hex (e.g. 0x302c)")
    ap_printer.add_argument("--pid", help="USB product id hex (e.g. 0x3101)")
    ap_printer.add_argument("--auto-detect", action="store_true", default=True, help="auto-detect PixCut device (default)")
    ap_printer.add_argument("--no-auto-detect", dest="auto_detect", action="store_false", help="disable auto-detect")
    ap_printer.add_argument("--interface", type=int, default=None, help="USB interface index (default: 0)")
    ap_printer.add_argument("--out-ep", type=int, default=None, help="bulk OUT endpoint address (default: 0x01)")
    ap_printer.add_argument("--in-ep", type=int, default=None, help="bulk IN endpoint address (default: 0x81)")
    ap_printer.add_argument("--timeout-ms", type=int, default=None, help="USB read/write timeout")
    ap_printer.add_argument(
        "--resume",
        action="store_true",
        help="send resume-printer (default action if neither flag set)",
    )
    ap_printer.add_argument(
        "--pause",
        action="store_true",
        help="send pause-printer",
    )
    ap_printer.add_argument("--log-dir", type=Path, help="explicit log directory")
    ap_printer.set_defaults(func=cmd_printer)

    ap_job = sub.add_parser("job", help="list or cancel active printer jobs", parents=[common])
    ap_job.add_argument("--vid", help="USB vendor id hex (e.g. 0x302c)")
    ap_job.add_argument("--pid", help="USB product id hex (e.g. 0x3101)")
    ap_job.add_argument("--auto-detect", action="store_true", default=True, help="auto-detect PixCut device (default)")
    ap_job.add_argument("--no-auto-detect", dest="auto_detect", action="store_false", help="disable auto-detect")
    ap_job.add_argument("--interface", type=int, default=None, help="USB interface index")
    ap_job.add_argument("--out-ep", type=int, default=None, help="bulk OUT endpoint address")
    ap_job.add_argument("--in-ep", type=int, default=None, help="bulk IN endpoint address")
    ap_job.add_argument("--timeout-ms", type=int, default=None, help="USB read/write timeout")
    ap_job.add_argument("--log-dir", type=Path, help="explicit log directory")
    job_action = ap_job.add_mutually_exclusive_group(required=True)
    job_action.add_argument("--list", dest="list_jobs", action="store_true", help="list active printer job IDs")
    job_action.add_argument("--cancel", type=int, metavar="JOB_ID", help="cancel the specified printer job")
    ap_job.set_defaults(func=cmd_job)

    ap_convert = sub.add_parser("convert", help="convert SVG to PLT without connecting to device", parents=[common])
    ap_convert.add_argument("--svg", required=True, help="input SVG file")
    ap_convert.add_argument("--out", help="output PLT path (default: replace .svg with .plt)")
    ap_convert.add_argument("--dpi", type=float, default=96.0, help="assumed SVG DPI for px units (default 96)")
    ap_convert.add_argument(
        "--units-per-inch",
        type=float,
        default=1016.0,
        help="PLT target units per inch (default 1016, matches observed PixCut scale)",
    )
    ap_convert.add_argument("--kp", type=int, default=42, help="knife pressure KP value applied to all paths (default 42)")
    ap_convert.add_argument("--tx", type=float, default=0.0, help="translate X in HPGL units after transform")
    ap_convert.add_argument("--ty", type=float, default=0.0, help="translate Y in HPGL units after transform")
    ap_convert.add_argument(
        "--perf-color", default="ff8800", metavar="HEX",
        help="stroke color (6-char hex, no #) that marks perf-cut paths in the SVG (default: ff8800). "
             "Set to empty string to disable color-based separation.",
    )
    ap_convert.add_argument("--perf-kp", type=int, default=53, help="knife pressure for perf-cut paths (default: 53)")
    ap_convert.add_argument("--perf-dash", type=float, default=8.0, metavar="MM", help="perf-cut dash length in mm (default: 8.0)")
    ap_convert.add_argument("--perf-gap", type=float, default=0.05, metavar="MM", help="perf-cut gap between dashes in mm (default: 0.05)")
    ap_convert.set_defaults(func=cmd_convert)

    ap_layout = sub.add_parser(
        "layout",
        help="auto-trace cut paths from PNG/JPG images and export a print-ready sheet",
        parents=[common],
        description=(
            "Load one or more PNG/JPG sticker images, pack them onto a 4×7\" canvas, "
            "trace cut paths from alpha transparency (or white background for JPEGs), "
            "and export a composite JPEG + PLT cut file + SVG preview. "
            "Pass the outputs directly to:  pixcut_cli.py send --jpg layout.jpg --plt layout.plt\n\n"
            "Requires: pip install Pillow scikit-image\n"
            "Optional (accurate margin offsets): pip install shapely"
        ),
    )
    ap_layout.add_argument("images", nargs="+", help="input PNG or JPEG image(s)")
    ap_layout.add_argument(
        "--out-dir", default=".", metavar="DIR",
        help="output directory for generated files (default: current directory)",
    )
    ap_layout.add_argument(
        "--dpi", type=int, default=300,
        help="output resolution in DPI — also controls how large images appear "
             "on the sheet (e.g. a 300×300 px image at 300 DPI = 1×1 inch sticker, default: 300)",
    )
    ap_layout.add_argument(
        "--margin", type=float, default=2.0, metavar="MM",
        help="cut margin in mm outside the sticker edge (default: 2.0)",
    )
    ap_layout.add_argument(
        "--padding", type=float, default=3.0, metavar="MM",
        help="padding between stickers in mm (default: 3.0)",
    )
    ap_layout.add_argument(
        "--kp", type=int, default=42,
        help="knife pressure KP value for PLT output (default: 42)",
    )
    ap_layout.add_argument(
        "--paginate", action="store_true",
        help="if images overflow the canvas, create additional sheets "
             "(layout_1.*, layout_2.*, ...) instead of skipping them",
    )
    ap_layout.add_argument(
        "--bg-white", action="store_true",
        help="treat near-white pixels as transparent background "
             "(use for JPEGs or PNGs without an alpha channel)",
    )
    ap_layout.add_argument(
        "--repeat", type=int, default=1, metavar="N",
        help="place each image N times on the sheet(s) — e.g. --repeat 6 fills a sheet "
             "with 6 copies of the same sticker. Use with --paginate if N copies overflow "
             "one canvas.",
    )
    ap_layout.add_argument(
        "--left-margin", type=float, default=0.0, metavar="MM",
        help="extra left paper margin in mm — shifts all cuts away from the left edge (default: 0.0)",
    )

    # Perf-cut options
    ap_layout.add_argument(
        "--perf-cut", action="store_true",
        help="add a second set of dashed cut lines outside the kiss-cut at higher knife pressure, "
             "allowing stickers to pop out cleanly without jams",
    )
    ap_layout.add_argument(
        "--perf-kp", type=int, default=53,
        help="knife pressure for perf-cut lines (default: 50)",
    )
    ap_layout.add_argument(
        "--perf-dash", type=float, default=8.0, metavar="MM",
        help="perf-cut dash length in mm (default: 8.0)",
    )
    ap_layout.add_argument(
        "--perf-gap", type=float, default=0.05, metavar="MM",
        help="perf-cut gap length between dashes in mm (default: 0.05)",
    )
    ap_layout.set_defaults(func=cmd_layout)

    ap_probe = sub.add_parser("probe", help="probe device properties and methods (read-only)", parents=[common])
    ap_probe.add_argument("--vid", help="USB vendor id hex (e.g. 0x302c)")
    ap_probe.add_argument("--pid", help="USB product id hex (e.g. 0x3101)")
    ap_probe.add_argument("--auto-detect", action="store_true", default=True, help="auto-detect PixCut device (default)")
    ap_probe.add_argument("--no-auto-detect", dest="auto_detect", action="store_false", help="disable auto-detect")
    ap_probe.add_argument("--interface", type=int, default=None, help="USB interface index (default: 0)")
    ap_probe.add_argument("--out-ep", type=int, default=None, help="bulk OUT endpoint address (default: 0x01)")
    ap_probe.add_argument("--in-ep", type=int, default=None, help="bulk IN endpoint address (default: 0x81)")
    ap_probe.add_argument(
        "--data-interface",
        type=int,
        default=None,
        help="USB interface index for data plane (default: auto-detect or unset)",
    )
    ap_probe.add_argument(
        "--data-out-ep",
        type=lambda x: int(x, 0),
        default=None,
        help="bulk OUT endpoint for data (accepts decimal or 0x-prefixed hex; default: auto-detect or unset)",
    )
    ap_probe.add_argument(
        "--data-in-ep",
        type=lambda x: int(x, 0),
        default=None,
        help="bulk IN endpoint for data (accepts decimal or 0x-prefixed hex; default: auto-detect or unset)",
    )
    ap_probe.add_argument("--timeout-ms", type=int, default=None, help="USB read/write timeout")
    ap_probe.add_argument("--id-strategy", choices=["monotonic", "fixed"], default="monotonic", help="message id strategy")
    ap_probe.add_argument("--req-id", type=int, help="explicit id for manual method calls")
    ap_probe.add_argument("--properties", nargs="+", help="extra property names to include in get-prop sweep")
    ap_probe.add_argument("--no-all-properties", dest="all_properties", action="store_false", help="disable default property sweep")
    ap_probe.add_argument("--experimental", action="store_true", help="include broader experimental property names (read-only)")
    ap_probe.add_argument("--prop-batch", type=int, default=8, help="batch size for get-prop requests")
    ap_probe.add_argument("--methods", nargs="+", default=[], help="additional methods to call (no params unless job-id given)")
    ap_probe.add_argument("--job-id", type=int, help="job id used when calling get-job-info")
    ap_probe.add_argument("--dangerous", action="store_true", help="allow set-prop mutations")
    ap_probe.add_argument("--set-prop", nargs="+", help="key=value pairs to send via set-prop (requires --dangerous)")
    ap_probe.add_argument("--log-dir", type=Path, help="explicit log directory")
    ap_probe.set_defaults(func=cmd_probe, all_properties=True)

    return ap


def main(argv: Optional[list] = None) -> None:
    import sys
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:
        ensure_logging(verbose=False)
        log.error("%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
