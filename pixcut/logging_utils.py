import json
import logging
import os
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


def default_log_dir(prefix: str = "run-logs") -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path(prefix) / f"session-{stamp}"


class ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: "\033[36m",   # cyan
        logging.INFO: "\033[37m",    # light gray
        logging.WARNING: "\033[33m", # yellow
        logging.ERROR: "\033[31m",   # red
        logging.CRITICAL: "\033[41m" # red background
    }
    RESET = "\033[0m"

    def format(self, record):
        msg = super().format(record)
        color = self.COLORS.get(record.levelno, "")
        reset = self.RESET if color else ""
        return f"{color}{msg}{reset}"


def ensure_logging(level: int = logging.INFO, *, verbose: bool = True, logfile: Optional[Path] = None) -> None:
    if logging.getLogger().handlers:
        return
    handlers = []
    console = logging.StreamHandler(sys.stdout)
    if verbose:
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    else:
        console.setFormatter(ColorFormatter("%(asctime)s %(message)s"))
    handlers.append(console)
    if logfile:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(logfile, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        handlers.append(fh)
    logging.basicConfig(level=level, handlers=handlers)


def hexdump(data: bytes, width: int = 16) -> str:
    lines = []
    for i in range(0, len(data), width):
        chunk = data[i : i + width]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b <= 126 else "." for b in chunk)
        lines.append(f"{i:04x}  {hex_part:<{width*3}} {ascii_part}")
    return "\n".join(lines)


class SessionLogger:
    """
    Thin helper to keep JSONL logs and raw payload artifacts together.
    """

    def __init__(self, root: Optional[Path], *, keep_json: bool = True, plain_log_path: Optional[Path] = None):
        self.root = Path(root) if root is not None else None
        self.keep_json = keep_json and root is not None
        self.plain_log_path = plain_log_path
        if self.root is not None:
            self.root.mkdir(parents=True, exist_ok=True)
        if self.keep_json:
            self.out_jsonl = self.root / "requests_sent.jsonl"
            self.in_jsonl = self.root / "responses_seen.jsonl"
            self.combined_jsonl = self.root / "requests_and_responses.jsonl"
            self.raw_dir = self.root / "raw"
            self.raw_dir.mkdir(exist_ok=True)
        else:
            self.out_jsonl = None
            self.in_jsonl = None
            self.combined_jsonl = None
            self.raw_dir = None

    def _append_json(self, path: Path, obj: Any) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def log_json(self, direction: str, obj: Any) -> None:
        if not self.keep_json:
            return
        path = self.out_jsonl if direction == "out" else self.in_jsonl
        self._append_json(path, obj)
        if self.combined_jsonl:
            combined_obj = {"direction": direction, "payload": obj}
            self._append_json(self.combined_jsonl, combined_obj)

    def log_bytes(self, direction: str, label: str, data: bytes) -> None:
        if not self.keep_json or self.raw_dir is None:
            return
        fname = f"{direction}-{label}.bin"
        path = self.raw_dir / fname
        with open(path, "ab") as f:
            f.write(data)

    def log_text_block(
        self, title: str, payload: str, logger: Optional[logging.Logger] = None
    ) -> None:
        tgt = logger or logging.getLogger("pixcut")
        indent = "    "
        tgt.info("%s:\n%s", title, textwrap.indent(payload, indent))
        if self.plain_log_path:
            ts = datetime.now().isoformat(timespec="seconds")
            with open(self.plain_log_path, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {title}:\n{textwrap.indent(payload, '    ')}\n")

    def save_artifact(self, name: str, content: bytes) -> Path:
        path = self.root / name
        with open(path, "wb") as f:
            f.write(content)
        return path

    def describe(self) -> str:
        return os.path.abspath(self.root) if self.root else "(no log directory)"
