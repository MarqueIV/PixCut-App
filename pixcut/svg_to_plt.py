"""
Convert SVG vector paths into the PixCut's HPGL-ish PLT format.

Uses `svgelements` for robust parsing of SVG transforms, units, and shapes.
"""

import math
from pathlib import Path
from typing import List, Optional, Tuple, Union

from svgelements import SVG, Path as SvgPath, Matrix, Shape, Move, Close, Line, Color

Point = Tuple[float, float]

DEFAULT_UNITS_PER_INCH = 1016
DEFAULT_DPI = 96.0  # Standard CSS pixel ratio
MM_PER_INCH = 25.4
# Default knife pressure from official software for Liene sticker media
DEFAULT_KP = 42
# Internal tuning defaults (adjust here if needed)
DEFAULT_TOLERANCE = 5.0
DEFAULT_SIMPLIFY = 10.0
DEFAULT_ROTATE_DEG = -90.0
# Distance threshold (SVG units) for pruning points that lie on nearly straight segments.
STRAIGHT_DIST_EPS = 0.5
# Stroke color (lowercase 6-char hex, no #) that marks perf-cut paths in SVGs.
PERF_CUT_COLOR = "ff8800"


def _bbox_of_polys(polys: List[List[Point]]) -> Tuple[float, float, float, float]:
    if not polys:
        return 0, 0, 0, 0
    xs = [p[0] for poly in polys for p in poly]
    ys = [p[1] for poly in polys for p in poly]
    if not xs:
        return 0, 0, 0, 0
    return min(xs), min(ys), max(xs), max(ys)


def flatten_element(element: Shape, tolerance: float) -> List[List[Point]]:
    """
    Convert an svgelements Shape (Path, Rect, etc.) into a list of polylines.
    """
    # Convert shape to Path and apply its transform to the points
    path = SvgPath(element)
    path.reify() 

    polys = []
    current_poly = []

    for segment in path:
        if isinstance(segment, Move):
            if current_poly:
                polys.append(current_poly)
            current_poly = [(segment.end.x, segment.end.y)]
        elif isinstance(segment, Close):
            if current_poly:
                # Ensure the polygon is visually closed
                if current_poly[0] != current_poly[-1]:
                    current_poly.append(current_poly[0])
                polys.append(current_poly)
                current_poly = []
        elif isinstance(segment, Line):
            if not current_poly:
                current_poly.append((segment.start.x, segment.start.y))
            current_poly.append((segment.end.x, segment.end.y))
        else:
            # Curves (CubicBezier, QuadraticBezier, Arc)
            try:
                length = segment.length()
            except AttributeError:
                length = 0
            
            if length == 0:
                continue

            # Dynamic step count based on tolerance
            steps = max(2, int(length / tolerance))
            for i in range(1, steps + 1):
                t = i / steps
                p = segment.point(t)
                current_poly.append((p.x, p.y))
    
    if current_poly:
        polys.append(current_poly)
        
    return polys


def simplify_polyline(poly: List[Point], tolerance: float) -> List[Point]:
    """
    Simple distance-based simplification. Merges points closer than `tolerance`.
    """
    if len(poly) < 3:
        return poly
    out = [poly[0]]
    last = poly[0]
    tol_sq = tolerance * tolerance
    for p in poly[1:-1]:
        dist_sq = (p[0] - last[0]) ** 2 + (p[1] - last[1]) ** 2
        if dist_sq > tol_sq:
            out.append(p)
            last = p
    out.append(poly[-1])
    return out


def prune_straight_segments(poly: List[Point], dist_eps: float = STRAIGHT_DIST_EPS) -> List[Point]:
    """
    Drop interior points that lie within `dist_eps` of the line between their neighbors.
    This aggressively cleans dense straight runs while leaving curves (larger deviation) intact.
    """
    if len(poly) < 3:
        return poly
    kept = [poly[0]]
    for i in range(1, len(poly) - 1):
        a = kept[-1]
        b = poly[i]
        c = poly[i + 1]
        denom = math.hypot(c[0] - a[0], c[1] - a[1])
        if denom == 0:
            kept.append(b)
            continue
        dist = abs((c[0] - a[0]) * (a[1] - b[1]) - (a[0] - b[0]) * (c[1] - a[1])) / denom
        if dist > dist_eps:
            kept.append(b)
    kept.append(poly[-1])
    return kept


def _color_hex(stroke) -> Optional[str]:
    """Normalize a svgelements stroke value to lowercase 6-char hex, or None."""
    if stroke is None:
        return None
    try:
        s = stroke.hex if hasattr(stroke, "hex") else str(stroke)
    except Exception:
        return None
    s = s.strip().lstrip("#").lower()
    if len(s) == 6:
        return s
    if len(s) == 3:
        return s[0] * 2 + s[1] * 2 + s[2] * 2
    return None


def _dash_polyline(
    pts: List[Point],
    dash_u: float,
    gap_u: float,
) -> List[Tuple[bool, List[Point]]]:
    """
    Segment a polyline into alternating draw/lift pieces by arc length.

    Returns [(is_draw, points), ...].  Emit U+D commands for is_draw=True
    and a bare U (pen-up move to last point) for False.
    """
    if not pts or dash_u <= 0 or gap_u <= 0:
        return [(True, pts)]

    segments: List[Tuple[bool, List[Point]]] = []
    is_draw = True
    budget = dash_u
    current: List[Point] = [pts[0]]

    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        seg_len = math.hypot(x1 - x0, y1 - y0)
        if seg_len < 1e-6:
            continue
        remaining = seg_len
        t = 0.0

        while remaining > 1e-6:
            step = min(remaining, budget)
            t_new = t + step / seg_len
            ix = x0 + (x1 - x0) * t_new
            iy = y0 + (y1 - y0) * t_new
            current.append((ix, iy))
            remaining -= step
            budget -= step
            t = t_new

            if budget <= 1e-6:
                segments.append((is_draw, current))
                is_draw = not is_draw
                budget = dash_u if is_draw else gap_u
                current = [(ix, iy)]

    if len(current) > 1:
        segments.append((is_draw, current))

    return segments


def _plt_path_commands(
    polylines: List[List[Point]],
    dash_u: float = 0.0,
    gap_u: float = 0.0,
    dash_kp: Optional[int] = None,
    gap_kp: Optional[int] = None,
    nudge_u: float = 0.0,
) -> List[str]:
    """
    Return PLT command tokens for a list of polylines.

    dash_u/gap_u > 0: segment into alternating draw/lift pieces (perf-cut dash mode).

    dash_kp/gap_kp both set: pressure mode — single pass along the perf path with
    alternating knife pressure. Each segment is its own sub-path (KP then U then D).
    nudge_u: small X offset added to the U coordinate so the firmware sees real travel
    and doesn't optimise the lift away. The first D of the next segment returns to the
    true shared endpoint, leaving an imperceptible mark on the cutter.
    """
    parts: List[str] = []
    for pts in polylines:
        if not pts:
            continue
        if dash_u > 0 and gap_u > 0:
            for is_draw, seg in _dash_polyline(pts, dash_u, gap_u):
                # Each segment is its own sub-path. KP before U so the new
                # pressure is applied when the knife re-seats on the first D.
                # nudge_u shifts the U slightly so the firmware registers real travel.
                parts.append(f"KP{dash_kp if is_draw else gap_kp}")
                parts.append(f"U{round(seg[0][0] + nudge_u)},{round(seg[0][1])}")
                for x, y in seg[1:]:
                    parts.append(f"D{round(x)},{round(y)}")
        else:
            parts.append(f"U{round(pts[0][0])},{round(pts[0][1])}")
            for x, y in pts:
                parts.append(f"D{round(x)},{round(y)}")
    return parts


def points_to_plt(polylines: List[List[Point]], kp: int = DEFAULT_KP) -> str:
    parts = ["IN", "VER0.1.0", f"KP{kp}"]
    for poly in polylines:
        if not poly:
            continue
        # Move to start
        parts.append(f"U{round(poly[0][0])},{round(poly[0][1])}")
        # Draw to rest
        for (x, y) in poly:
            parts.append(f"D{round(x)},{round(y)}")
    parts.append(" U6476,0 @ ")
    return " ".join(parts)


def convert_svg_to_plt(
    svg_path: Path,
    *,
    dpi: float = DEFAULT_DPI,
    units_per_inch: float = DEFAULT_UNITS_PER_INCH,
    knife_pressure: int = DEFAULT_KP,
    translate_x: float = 0.0,
    translate_y: float = 0.0,
    perf_cut_color: str = PERF_CUT_COLOR,
    perf_knife_pressure: int = 60,
    perf_dash_mm: float = 8.0,
    perf_gap_mm: float = 0.05,
) -> str:
    """Convert an SVG to PLT.

    Paths whose stroke color matches *perf_cut_color* (default ``#ff8800``,
    orange) are treated as perf-cut lines and emitted as a dashed section at
    *perf_knife_pressure* after the main kiss-cut section.  All other stroked
    paths are treated as kiss-cut lines at *knife_pressure*.

    Set ``perf_cut_color=""`` to disable colour-based separation and emit
    everything as kiss-cut (original behaviour).
    """
    tolerance = DEFAULT_TOLERANCE
    simplify = DEFAULT_SIMPLIFY
    rotate_deg = DEFAULT_ROTATE_DEG
    perf_color_norm = perf_cut_color.strip().lstrip("#").lower() if perf_cut_color else ""

    svg = SVG.parse(svg_path, ppi=dpi)

    def get_pixels(v, dpi):
        if v is None: return 0.0
        if isinstance(v, (int, float)): return float(v)
        if hasattr(v, "value"): return v.value(ppi=dpi)
        return 0.0

    # 1. Determine Document Dimensions
    if svg.viewbox:
        doc_x, doc_y, doc_w, doc_h = svg.viewbox.x, svg.viewbox.y, svg.viewbox.width, svg.viewbox.height
    else:
        doc_x, doc_y = get_pixels(svg.x, dpi), get_pixels(svg.y, dpi)
        doc_w, doc_h = get_pixels(svg.width, dpi), get_pixels(svg.height, dpi)

    tgt_w_in = 4.0
    tgt_h_in = 7.0

    # Stage 0: Parse and Flatten — track stroke colour alongside each polyline.
    polylines_raw: List[List[Point]] = []
    colors_raw: List[Optional[str]] = []   # parallel to polylines_raw
    for element in svg.elements():
        if not isinstance(element, Shape):
            continue
        if getattr(element, "visibility", "visible") == "hidden":
            continue
        if getattr(element, "stroke", None) is None:
            continue
        if getattr(element, "transform", None):
            element = element * element.transform
        color = _color_hex(getattr(element, "stroke", None))
        new_polys = flatten_element(element, tolerance)
        polylines_raw.extend(new_polys)
        colors_raw.extend([color] * len(new_polys))

    # Fallback dimensions from geometry
    if doc_w <= 0 or doc_h <= 0:
        gx0, gy0, gx1, gy1 = _bbox_of_polys(polylines_raw)
        doc_x, doc_y = gx0, gy0
        doc_w, doc_h = gx1 - gx0, gy1 - gy0
        if doc_w <= 0: doc_w = 100
        if doc_h <= 0: doc_h = 100

    # Phantom document-bounds rectangle (keeps transform pipeline honest).
    polylines_raw.append([
        (doc_x, doc_y), (doc_x + doc_w, doc_y),
        (doc_x + doc_w, doc_y + doc_h), (doc_x, doc_y + doc_h),
        (doc_x, doc_y),
    ])
    colors_raw.append(None)   # phantom has no colour

    # Stage 1: Normalise to document origin
    polylines_norm = [[(x - doc_x, y - doc_y) for x, y in poly] for poly in polylines_raw]

    # Stage 2: Scale
    scale_x = (tgt_w_in / doc_w) * units_per_inch
    scale_y = (tgt_h_in / doc_h) * units_per_inch
    scale = min(scale_x, scale_y)
    polylines_scaled = [[(x * scale, y * scale) for x, y in poly] for poly in polylines_norm]
    cur_w = doc_w * scale
    cur_h = doc_h * scale

    # Stage 3: Rotate
    rot = rotate_deg % 360
    def _rot(pt):
        x, y = pt
        if rot == 90:  return (y, cur_w - x)
        if rot == 270: return (cur_h - y, x)
        if rot == 180: return (cur_w - x, cur_h - y)
        return (x, y)
    polylines_rot = [[_rot(p) for p in poly] for poly in polylines_scaled]
    if rot in (90, 270):
        cur_w, cur_h = cur_h, cur_w

    # Stage 4: Translate
    polylines_final = [
        [(x + translate_x, y + translate_y) for x, y in poly]
        for poly in polylines_rot
    ]

    # Remove phantom rectangle (last element) — colours list tracks in sync.
    if polylines_final:
        polylines_final.pop()
        colors_raw.pop()

    # Clean and simplify
    polylines_final = [prune_straight_segments(p) for p in polylines_final]
    if simplify > 0:
        polylines_final = [simplify_polyline(p, simplify) for p in polylines_final]

    # Separate kiss-cut and perf-cut by stroke colour.
    kiss_polys: List[List[Point]] = []
    perf_polys: List[List[Point]] = []
    for color, poly in zip(colors_raw, polylines_final):
        if perf_color_norm and color == perf_color_norm:
            perf_polys.append(poly)
        else:
            kiss_polys.append(poly)

    # Build PLT — kiss-cut first, then optional dashed perf-cut section.
    plt_parts = ["IN", "VER0.1.0", f"KP{knife_pressure}"]
    plt_parts += _plt_path_commands(kiss_polys)
    if perf_polys:
        units_per_mm = units_per_inch / MM_PER_INCH
        dash_u = perf_dash_mm * units_per_mm
        gap_u = perf_gap_mm * units_per_mm
        plt_parts.append(f"KP{perf_knife_pressure}")
        plt_parts += _plt_path_commands(perf_polys, dash_u, gap_u)
    plt_parts.append(" U6476,0 @ ")
    return " ".join(plt_parts)


def save_plt(plt_text: str, out_path: Path) -> None:
    out_path.write_text(plt_text, encoding="ascii")
