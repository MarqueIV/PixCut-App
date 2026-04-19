import sys
import re
from pathlib import Path

def convert_plt_to_hpgl(input_path: Path):

    text = input_path.read_text(errors="ignore")

    # Tokenize by whitespace
    tokens = text.replace("@", "").split()

    hpgl_lines = []
    hpgl_lines.append("IN;")
    hpgl_lines.append("SP1;")

    current_pd = []

    for token in tokens:
        # Skip PixCut-specific header tokens
        if token.startswith("VER") or token.startswith("KP"):
            continue
        if token == "IN":
            continue

        m = re.match(r"([UD])(-?\d+),(-?\d+)", token)
        if not m:
            continue

        cmd, x, y = m.groups()

        if cmd == "U":
            # Flush any active PD polyline
            if current_pd:
                hpgl_lines.append("PD" + ",".join(current_pd) + ";")
                current_pd = []

            # Pen up move
            hpgl_lines.append(f"PU{x},{y};")

        elif cmd == "D":
            current_pd.append(f"{x},{y}")

    # Flush final PD
    if current_pd:
        hpgl_lines.append("PD" + ",".join(current_pd) + ";")

    hpgl_lines.append("SP0;")

    out_path = input_path.with_suffix(".hpgl")

    out_path.write_text("\n".join(hpgl_lines))
    print(f"Wrote HPGL to {out_path}")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python plt_to_hpgl.py input.plt")
        sys.exit(1)

    convert_plt_to_hpgl(Path(sys.argv[1]))
    