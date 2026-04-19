import json
import re
import struct
from typing import Generator, Iterable, Optional, Tuple

CMD_JSON_PREFIX = b"cmd json\n"
CMD_DATA_PREFIX = b"cmd data EXTLEN="


def encode_json_command(obj: dict) -> bytes:
    """Build a framed JSON command using compact JSON (matches captures)."""
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return CMD_JSON_PREFIX + payload


def parse_balanced_json(buf: bytes, start: int = 0) -> Optional[Tuple[dict, int]]:
    """
    Best-effort parser that walks braces and tolerates nulls/CR chars found in captures.
    Returns (obj, end_offset) or None if no full JSON object is found.
    """
    i = buf.find(b"{", start)
    if i < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for j in range(i, len(buf)):
        c = buf[j]
        if in_str:
            if esc:
                esc = False
            elif c == 0x5C:
                esc = True
            elif c == 0x22:
                in_str = False
            continue
        if c == 0x22:
            in_str = True
            continue
        if c == 0x7B:
            depth += 1
        elif c == 0x7D:
            depth -= 1
            if depth == 0:
                raw = buf[i : j + 1]
                for candidate in (
                    raw,
                    raw.replace(b"\x00", b""),
                    raw.replace(b"\x00", b"").replace(b"\r", b""),
                ):
                    try:
                        return json.loads(candidate.decode("utf-8")), j + 1
                    except Exception:
                        pass
                try:
                    cleaned = bytes(
                        b for b in raw if b in (9, 10, 13) or 32 <= b <= 126
                    )
                    return json.loads(cleaned.decode("utf-8")), j + 1
                except Exception:
                    return None
    return None


def chunk_payload(
    payload: bytes,
    chunk_extlen: int = 4075,
    job_id: int = 0,
) -> Iterable[bytes]:
    """
    Yield framed cmd data packets.
    """
    max_body = chunk_extlen - 4
    header = struct.pack("<I", job_id)
    offset = 0
    while offset < len(payload):
        body_size = max_body 
        body = payload[offset : offset + body_size]
        extlen = len(body) + 4
        yield (
            CMD_DATA_PREFIX
            + str(extlen).encode("ascii")
            + b"\n"
            + header
            + body
        )
        offset += len(body)
