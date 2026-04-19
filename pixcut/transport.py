import logging
import platform
import sys
from dataclasses import dataclass
from typing import Optional

import usb.core
import usb.util

log = logging.getLogger("pixcut.transport")

# Default timeout is generous to avoid spurious bulk timeouts on slow phases.
DEFAULT_TIMEOUT_MS = 5000


@dataclass
class USBConfig:
    vid: int
    pid: int
    interface: int = 0
    out_ep: int = 0x01
    in_ep: int = 0x81
    data_interface: Optional[int] = None
    data_out_ep: Optional[int] = None
    data_in_ep: Optional[int] = None
    timeout_ms: int = DEFAULT_TIMEOUT_MS


def _backend_error_help() -> str:
    if sys.platform == "win32":
        arch = platform.machine()
        is_64 = sys.maxsize > 2**32
        py_arch = "64-bit" if is_64 else "32-bit"
        return (
            f"USB backend not found.\n"
            f"  System: {arch}\n"
            f"  Python Process: {py_arch}\n"
            "Setup required:\n"
            "1. Download 'libusb-1.0.dll'.\n"
            "2. Place it in the same folder as this script (or C:\\Windows\\System32).\n"
            "3. IMPORTANT: The DLL architecture must match the Python process.\n"
            "   - If Python is ARM64, use an ARM64 build of libusb-1.0.dll.\n"
            "   - If Python is x64 (AMD64), use an x64 build.\n"
            "   - If Python is x86 (32-bit), use an x86 build.\n"
            "4. Ensure the device driver is 'WinUSB' (via Zadig)."
        )
    return "USB backend not found (check libusb installation)."


class USBTransport:
    """
    Thin pyusb wrapper. Caller owns framing.
    """

    def __init__(self, cfg: USBConfig):
        self.cfg = cfg
        self.dev: Optional[usb.core.Device] = None
        self._claimed_json = False
        self._claimed_data = False

    def open(self) -> None:
        try:
            dev = usb.core.find(idVendor=self.cfg.vid, idProduct=self.cfg.pid)
        except usb.core.NoBackendError:
            raise RuntimeError(_backend_error_help())
        if dev is None:
            raise RuntimeError(
                f"Device not found; specify --vid/--pid (current: 0x{self.cfg.vid:04x}/0x{self.cfg.pid:04x})"
            )
        self.dev = dev

        try:
            if dev.is_kernel_driver_active(self.cfg.interface):
                dev.detach_kernel_driver(self.cfg.interface)
        except NotImplementedError:
            pass
        except usb.core.USBError as e:
            log.warning("could not detach kernel driver from intf %d: %s", self.cfg.interface, e)

        # Only set configuration if not already active to avoid unnecessary Control Transfers.
        try:
            if dev.get_active_configuration() is None:
                dev.set_configuration()
        except usb.core.USBError:
            dev.set_configuration()

        # Claim control (JSON) interface up front.
        self._ensure_kernel_detached(self.cfg.interface)
        usb.util.claim_interface(dev, self.cfg.interface)
        for ep in (self.cfg.out_ep, self.cfg.in_ep):
            try:
                dev.clear_halt(ep)
            except Exception:
                pass
        self._claimed_json = True

        # Claim data interface up front if distinct.
        if self.cfg.data_interface is not None and self.cfg.data_interface != self.cfg.interface:
            self._ensure_kernel_detached(self.cfg.data_interface)
            usb.util.claim_interface(dev, self.cfg.data_interface)
            data_out = self.cfg.data_out_ep if self.cfg.data_out_ep is not None else 0
            data_in = self.cfg.data_in_ep if self.cfg.data_in_ep is not None else 0
            for ep in (data_out, data_in):
                if ep:
                    try:
                        dev.clear_halt(ep)
                    except Exception:
                        pass
            self._claimed_data = True

        data_out = self.cfg.data_out_ep if self.cfg.data_out_ep is not None else 0
        data_in = self.cfg.data_in_ep if self.cfg.data_in_ep is not None else 0

        log.info(
            "opened USB device vid=0x%04x pid=0x%04x interface=%d out_ep=0x%02x in_ep=0x%02x data_if=%s data_out=0x%02x data_in=0x%02x",
            self.cfg.vid,
            self.cfg.pid,
            self.cfg.interface,
            self.cfg.out_ep,
            self.cfg.in_ep,
            str(self.cfg.data_interface),
            data_out,
            data_in,
        )

    def close(self) -> None:
        if not self.dev:
            return
        try:
            if self._claimed_json:
                usb.util.release_interface(self.dev, self.cfg.interface)
        except Exception:
            pass
        try:
            if self._claimed_data and self.cfg.data_interface is not None and self.cfg.data_interface != self.cfg.interface:
                usb.util.release_interface(self.dev, self.cfg.data_interface)
        except Exception:
            pass
        usb.util.dispose_resources(self.dev)
        self.dev = None
        self._claimed_json = False
        self._claimed_data = False

    def _ensure_kernel_detached(self, iface: int):
        try:
            if self.dev.is_kernel_driver_active(iface):
                self.dev.detach_kernel_driver(iface)
        except NotImplementedError:
            pass
        except usb.core.USBError as e:
            if sys.platform.startswith("linux") and "Access denied" in str(e):
                raise RuntimeError(
                    f"could not detach kernel driver from interface {iface}: {e}\n"
                    "\n"
                    "Fix: grant non-root USB access via a udev rule:\n"
                    f"  sudo cp 99-pixcut.rules /etc/udev/rules.d/\n"
                    f"  sudo udevadm control --reload-rules && sudo udevadm trigger\n"
                    "Then unplug and reconnect the printer.\n"
                    "Alternatively, run once as root: sudo python pixcut_cli.py ..."
                ) from e
            log.warning("could not detach kernel driver from intf %d: %s", iface, e)

    def write(self, data: bytes, endpoint: Optional[int] = None) -> int:
        if not self.dev:
            raise RuntimeError("transport not open")
        
        ep = endpoint if endpoint is not None else self.cfg.out_ep

        # Loop to ensure all bytes are written (handle short writes)
        view = memoryview(data)
        total = 0
        while total < len(data):
            total += self.dev.write(ep, view[total:], timeout=self.cfg.timeout_ms)
        return total

    def read(self, size: int = 4096, timeout_ms: Optional[int] = None, endpoint: Optional[int] = None) -> bytes:
        if not self.dev:
            raise RuntimeError("transport not open")
        timeout = timeout_ms if timeout_ms is not None else self.cfg.timeout_ms
        ep = endpoint if endpoint is not None else self.cfg.in_ep

        return bytes(
            # Interface already selected; keep signature minimal to avoid positional conflicts.
            self.dev.read(ep, size, timeout=timeout)
        )


class DummyTransport:
    """
    Offline transport used by --dry-run mode.
    """

    def __init__(self):
        self.buffer = bytearray()
        self.closed = False

    def open(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def write(self, data: bytes, endpoint: Optional[int] = None) -> int:
        if self.closed:
            raise RuntimeError("dummy transport closed")
        self.buffer.extend(data)
        return len(data)

    def read(self, size: int = 4096, timeout_ms: Optional[int] = None, endpoint: Optional[int] = None) -> bytes:
        return b""


# --- discovery helpers ------------------------------------------------------
KNOWN_IDS = [
    (0x302C, 0x3101),  # Observed PixCut S1 on macOS capture
]


def discover_pixcut(
    vid_hint: Optional[int] = None, pid_hint: Optional[int] = None
) -> USBConfig:
    """
    Scan USB bus and return a USBConfig for the first device matching known IDs
    or optional hints. Falls back to known defaults if hints are missing.
    """
    matches = []
    try:
        devices = list(usb.core.find(find_all=True))
    except usb.core.NoBackendError:
        raise RuntimeError(_backend_error_help())

    for dev in devices:
        vid = dev.idVendor
        pid = dev.idProduct
        if vid_hint and pid_hint:
            if vid == vid_hint and pid == pid_hint:
                matches.append((vid, pid, dev))
        else:
            if (vid, pid) in KNOWN_IDS:
                matches.append((vid, pid, dev))
            else:
                # Heuristic on product/manufacturer strings for future variants.
                try:
                    mfr = usb.util.get_string(dev, dev.iManufacturer) or ""
                    prod = usb.util.get_string(dev, dev.iProduct) or ""
                except Exception:
                    mfr = prod = ""
                if "Liene" in mfr or "PixCut" in prod:
                    matches.append((vid, pid, dev))
    if not matches:
        raise RuntimeError("No PixCut-like USB device found. Specify --vid/--pid manually.")
    vid, pid, dev = matches[0]
    serial = None
    try:
        serial = usb.util.get_string(dev, dev.iSerialNumber)
    except Exception:
        pass

    # Explicitly look for the known PixCut interfaces
    # Interface 2: Control (0x06 OUT / 0x86 IN)
    # Interface 3: Data (0x04 OUT / 0x84 IN)
    
    cfg = USBConfig(vid=vid, pid=pid, timeout_ms=DEFAULT_TIMEOUT_MS)
    # Pre-fill with known PixCut defaults so we attempt to claim both even if scan fails/is hidden.
    cfg.interface = 2
    cfg.out_ep = 0x06
    cfg.in_ep = 0x86
    cfg.data_interface = 3
    cfg.data_out_ep = 0x04
    cfg.data_in_ep = 0x84

    found_ctrl = False
    found_data = False

    try:
        for config in dev:
            for intf in config:
                if intf.bInterfaceNumber == 2:
                    cfg.interface = 2
                    cfg.out_ep = 0x06
                    cfg.in_ep = 0x86
                    found_ctrl = True
                elif intf.bInterfaceNumber == 3:
                    cfg.data_interface = 3
                    cfg.data_out_ep = 0x04
                    cfg.data_in_ep = 0x84
                    found_data = True
    except Exception:
        pass

    log.info(
        "auto-detected device vid=0x%04x pid=0x%04x serial=%s ctrl_if=%d(0x%02x) data_if=%s(0x%s)",
        vid,
        pid,
        serial or "?",
        cfg.interface,
        cfg.out_ep,
        str(cfg.data_interface),
        f"{cfg.data_out_ep:02x}" if cfg.data_out_ep else "None",
    )
    return cfg


def list_all_devices():
    """
    Yields (vid, pid, description, is_pixcut) for all detected USB devices.
    is_pixcut is True when the VID/PID matches a known PixCut device.
    """
    try:
        devices = list(usb.core.find(find_all=True))
    except usb.core.NoBackendError:
        yield (0, 0, _backend_error_help(), False)
        return
    except Exception as e:
        yield (0, 0, f"Error scanning bus: {e}", False)
        return

    for dev in devices:
        try:
            vid = dev.idVendor
            pid = dev.idProduct
        except Exception:
            continue
        is_pixcut = (vid, pid) in KNOWN_IDS
        parts = []
        try:
            mfr = usb.util.get_string(dev, dev.iManufacturer) if dev.iManufacturer else ""
            if mfr:
                parts.append(mfr)
        except Exception:
            pass
        try:
            prod = usb.util.get_string(dev, dev.iProduct) if dev.iProduct else ""
            if prod:
                parts.append(prod)
        except Exception:
            pass
        try:
            serial = usb.util.get_string(dev, dev.iSerialNumber) if dev.iSerialNumber else ""
            if serial:
                parts.append(f"serial={serial}")
        except Exception:
            pass
        # Heuristic detection for unknown VID/PID variants
        if not is_pixcut:
            combined = " ".join(parts).lower()
            if "liene" in combined or "pixcut" in combined or "hannto" in combined:
                is_pixcut = True
        description = "  ".join(parts) if parts else "(no device strings)"
        yield (vid, pid, description, is_pixcut)
