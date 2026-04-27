#!/usr/bin/env python3
"""
gpx_simplify.py — Simplify large GPX files for sailing track archives.

Merges all tracks/segments from multiple sources into a single chronologically
sorted track, filters speed anomalies, elevation spikes, and geometric
cross-track outliers, then decimates to a target point spacing.

Usage:
    python gpx_simplify.py -i voyage.gpx -o simplified.gpx
    python gpx_simplify.py -i voyage.gpx -d 200 -s 40 -vv
    python gpx_simplify.py -i voyage.gpx --passes 5 --dry-run -vvv
    python gpx_simplify.py -i voyage.gpx --max-ele-change 100

Requirements:
    pip install gpxpy rich
"""

import argparse
import math
import sys
import urllib.request
import urllib.parse
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# ── dependency check ──────────────────────────────────────────────────────────
try:
    import gpxpy
    import gpxpy.gpx
except ImportError:
    print("ERROR: gpxpy is not installed.  Run: pip install gpxpy", file=sys.stderr)
    sys.exit(1)

try:
    from rich.console import Console
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TaskProgressColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )
    from rich.table import Table
    from rich import print as rprint
    from rich.panel import Panel
    from rich.text import Text
    from rich.theme import Theme
except ImportError:
    print("ERROR: rich is not installed.  Run: pip install rich", file=sys.stderr)
    sys.exit(1)

# ── theme / console ───────────────────────────────────────────────────────────
THEME = Theme(
    {
        "info":    "cyan",
        "debug":   "bright_black",
        "trace":   "blue",
        "warn":    "yellow bold",
        "error":   "red bold",
        "good":    "green bold",
        "stat":    "magenta",
        "heading": "bold white",
    }
)
console = Console(theme=THEME, highlight=False)

VERBOSITY_QUIET   = 0   # only errors + final summary
VERBOSITY_INFO    = 1   # -v   : phase headers + key numbers
VERBOSITY_DEBUG   = 2   # -vv  : per-segment details, filter decisions
VERBOSITY_TRACE   = 3   # -vvv : every point examined


def log(level: int, verbosity: int, style: str, msg: str) -> None:
    if verbosity >= level:
        console.print(f"[{style}]{msg}[/{style}]")


# ── data structures ───────────────────────────────────────────────────────────
@dataclass
class Point:
    lat:       float
    lon:       float
    ele:       Optional[float]
    time:      Optional[datetime]
    source:    str = ""           # track/segment label for debug output

    # filled in during speed-filter pass
    speed_to_next: float = 0.0   # knots


# ── geometry helpers ──────────────────────────────────────────────────────────
EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return great-circle distance in metres between two lat/lon points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi  = math.radians(lat2 - lat1)
    dlam  = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def cross_track_distance_m(
    lat_a: float, lon_a: float,   # start of baseline segment
    lat_b: float, lon_b: float,   # end of baseline segment
    lat_p: float, lon_p: float,   # candidate point
) -> float:
    """
    Return the cross-track (perpendicular) distance in metres from point P to
    the great-circle path A→B.

    Uses the spherical cross-track formula:
        d_xt = asin(sin(d_AP/R) * sin(θ_AP − θ_AB)) * R

    where d_AP is the angular distance A→P and θ are bearings.
    Returns the absolute cross-track deviation (always ≥ 0).
    """
    lat_a_r = math.radians(lat_a)
    lon_a_r = math.radians(lon_a)
    lat_b_r = math.radians(lat_b)
    lon_b_r = math.radians(lon_b)
    lat_p_r = math.radians(lat_p)
    lon_p_r = math.radians(lon_p)

    # Angular distance A→P
    d_ap_r = haversine_m(lat_a, lon_a, lat_p, lon_p) / EARTH_RADIUS_M

    # Bearing A→P
    def bearing(la, lo, lb, lb2):
        dlo = lb2 - lo
        x = math.cos(lb) * math.sin(dlo)
        y = math.cos(la) * math.sin(lb) - math.sin(la) * math.cos(lb) * math.cos(dlo)
        return math.atan2(x, y)

    theta_ap = bearing(lat_a_r, lon_a_r, lat_p_r, lon_p_r)
    theta_ab = bearing(lat_a_r, lon_a_r, lat_b_r, lon_b_r)

    # If A and B are the same point, cross-track = distance A→P
    if haversine_m(lat_a, lon_a, lat_b, lon_b) < 1.0:
        return haversine_m(lat_a, lon_a, lat_p, lon_p)

    xt = math.asin(math.sin(d_ap_r) * math.sin(theta_ap - theta_ab)) * EARTH_RADIUS_M
    return abs(xt)


def destination_point(
    lat: float, lon: float, bearing_rad: float, distance_m: float
) -> tuple[float, float]:
    """
    Return the lat/lon of a point reached by travelling `distance_m` metres
    from (lat, lon) along `bearing_rad` (radians, clockwise from north) on a
    spherical Earth.  Uses the spherical law of cosines (destination formula).
    """
    d = distance_m / EARTH_RADIUS_M   # angular distance (radians)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    lat2 = math.asin(
        math.sin(lat1) * math.cos(d)
        + math.cos(lat1) * math.sin(d) * math.cos(bearing_rad)
    )
    lon2 = lon1 + math.atan2(
        math.sin(bearing_rad) * math.sin(d) * math.cos(lat1),
        math.cos(d) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


def initial_bearing_rad(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Return the initial great-circle bearing (radians) from point 1 to point 2."""
    la1, lo1, la2, lo2 = (
        math.radians(lat1), math.radians(lon1),
        math.radians(lat2), math.radians(lon2),
    )
    dlo = lo2 - lo1
    x = math.sin(dlo) * math.cos(la2)
    y = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dlo)
    return math.atan2(x, y)


def speed_knots(p1: Point, p2: Point) -> Optional[float]:
    """Return speed in knots between two timed points, or None if no timestamps."""
    if p1.time is None or p2.time is None:
        return None
    dt = abs((p2.time - p1.time).total_seconds())
    if dt < 1e-6:
        return None
    dist_m = haversine_m(p1.lat, p1.lon, p2.lat, p2.lon)
    mps = dist_m / dt
    return mps * 1.94384  # m/s → knots


def time_gap_hours(p1: Point, p2: Point) -> Optional[float]:
    """Return absolute time difference in hours between two points."""
    if p1.time is None or p2.time is None:
        return None
    return abs((p2.time - p1.time).total_seconds()) / 3600.0


# ── stats accumulator ────────────────────────────────────────────────────────
@dataclass
class Stats:
    tracks_in:              int = 0
    segments_in:            int = 0
    points_in:              int = 0
    points_no_time:         int = 0
    points_speed_drop:      int = 0
    points_lonjump_drop:    int = 0
    points_ele_drop:        int = 0
    points_crosstrack_drop: int = 0
    points_merge_drop:      int = 0
    points_duptime_drop:    int = 0
    points_duppos_drop:     int = 0
    points_zerospd_drop:    int = 0
    points_bridge_fill:     int = 0
    points_gap_fill:        int = 0
    gaps_found:             int = 0
    gaps_filled:            int = 0
    points_out:             int = 0
    segments_out:           int = 0
    waypoints_in:           int = 0
    waypoints_out:          int = 0
    total_dist_km:          float = 0.0
    crosstrack_passes:      int = 0
    bbox:                   list = field(default_factory=lambda: [90.0, 180.0, -90.0, -180.0])
    # bbox = [min_lat, min_lon, max_lat, max_lon]

    def update_bbox(self, lat: float, lon: float) -> None:
        self.bbox[0] = min(self.bbox[0], lat)
        self.bbox[1] = min(self.bbox[1], lon)
        self.bbox[2] = max(self.bbox[2], lat)
        self.bbox[3] = max(self.bbox[3], lon)


# ── phase 1: parse ────────────────────────────────────────────────────────────
def parse_gpx(path: Path, verbosity: int, stats: Stats) -> tuple[list[Point], list]:
    """
    Parse a GPX file, returning (all_track_points, waypoints).
    Track points are *not* yet sorted — that happens after collection.
    """
    log(VERBOSITY_INFO, verbosity, "info", f"📂  Parsing {path} …")

    file_size = path.stat().st_size
    log(VERBOSITY_INFO, verbosity, "debug", f"    File size: {file_size / 1_048_576:.1f} MB")

    all_points: list[Point] = []
    waypoints = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[info]Parsing GPX …"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        with open(path, "rb") as fh:
            gpx = gpxpy.parse(fh)

        stats.tracks_in    = len(gpx.tracks)
        stats.waypoints_in = len(gpx.waypoints)
        waypoints = list(gpx.waypoints)

        total_segs = sum(len(t.segments) for t in gpx.tracks)
        task = progress.add_task("segments", total=total_segs)

        for ti, track in enumerate(gpx.tracks):
            track_name = track.name or f"track_{ti}"
            for si, seg in enumerate(track.segments):
                stats.segments_in += 1
                seg_label = f"{track_name}/seg{si}"

                log(
                    VERBOSITY_DEBUG, verbosity, "debug",
                    f"    Segment [{seg_label}]: {len(seg.points)} raw points",
                )

                for pt in seg.points:
                    stats.points_in += 1
                    if pt.time is None:
                        stats.points_no_time += 1
                        log(
                            VERBOSITY_DEBUG, verbosity, "debug",
                            f"    DROP (no timestamp)  [{seg_label}] "
                            f"{pt.latitude:.5f},{pt.longitude:.5f}",
                        )
                        # Points without timestamps cannot be speed-checked,
                        # cross-track-checked, or placed correctly in the
                        # chronological timeline.  Drop them at parse time
                        # rather than silently appending them at the end of
                        # the sorted list where they would produce a spurious
                        # 'segment' of untimed fixes potentially far from the
                        # rest of the track.
                        continue

                    # Normalise to UTC-aware datetime
                    t = pt.time
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=timezone.utc)

                    p = Point(
                        lat=pt.latitude,
                        lon=pt.longitude,
                        ele=pt.elevation,
                        time=t,
                        source=seg_label,
                    )
                    all_points.append(p)

                progress.advance(task)

    log(
        VERBOSITY_INFO, verbosity, "info",
        f"    Loaded {stats.points_in:,} points from "
        f"{stats.tracks_in} track(s) / {stats.segments_in} segment(s).",
    )
    if stats.points_no_time:
        log(
            VERBOSITY_INFO, verbosity, "warn",
            f"    ⚠  {stats.points_no_time:,} points had no timestamp and were dropped.",
        )

    return all_points, waypoints


# ── phase 2: sort ─────────────────────────────────────────────────────────────
def sort_points(points: list[Point], verbosity: int) -> list[Point]:
    """Sort all points chronologically. Points without timestamps go to the end."""
    log(VERBOSITY_INFO, verbosity, "info", "🔀  Sorting points chronologically …")

    def sort_key(p: Point):
        if p.time is None:
            return datetime.max.replace(tzinfo=timezone.utc)
        return p.time

    points.sort(key=sort_key)
    log(VERBOSITY_DEBUG, verbosity, "debug",
        f"    Sort complete. First point: {points[0].time}  "
        f"Last point: {points[-1].time if points[-1].time else '(no time)'}")
    return points


# ── phase 3: speed-anomaly filter ─────────────────────────────────────────────
def filter_speed_anomalies(
    points: list[Point],
    max_speed_knots: float,
    verbosity: int,
    stats: Stats,
) -> list[Point]:
    """
    Remove points that imply an impossible speed or position.

    Normal case (dt > 0): a point is dropped if BOTH of the following are true:
      • the speed from the previous *kept* point to this point exceeds max_speed_knots
      • the speed from this point to the next point also exceeds max_speed_knots
    (This avoids dropping a valid point when two consecutive GPS fixes are very
    close in time but the boat just happened to be moving fast.)
    The additional one-sided "impossible distance" check drops a point immediately
    when no amount of look-ahead can make it geometrically reachable.

    Zero-dt case (two points share the same timestamp): speed_knots() returns None
    so the normal speed maths cannot be applied.  Instead we use a pure spatial
    check against the *raw* previous point in the sorted array (not kept[-1], which
    may itself be an outlier from a different GPS source).  If the raw predecessor
    shares the same timestamp and is more than ~1 second's worth of travel away
    (i.e. > max_speed × 1 s ≈ 26 m), the point is a ghost fix from a second GPS
    recording the same instant but placing the vessel thousands of km away.
    These clusters of simultaneous phantom positions cannot be caught by the
    impossible-distance check because dt = 0 makes max_plausible_m = 0, which
    would incorrectly flag every duplicate-position fix too.  The 1-second budget
    is a deliberate conservative floor: a genuinely stationary duplicate should be
    within a few metres of its twin; anything beyond 26 m at the same timestamp
    is not a duplicate, it is a ghost.
    """
    log(VERBOSITY_INFO, verbosity, "info",
        f"🚀  Filtering speed anomalies > {max_speed_knots:.0f} kn …")

    if not points:
        return points

    # One second's worth of travel at the speed cap — used as the zero-dt distance floor.
    _one_sec_max_m = max_speed_knots / 1.94384   # metres

    kept: list[Point] = [points[0]]
    # Track which raw indices were kept so zero-dt duplicates of dropped points
    # are themselves dropped (propagate-drop rule — see zero-dt branch below).
    _kept_indices: set[int] = {0}

    with Progress(
        SpinnerColumn(),
        TextColumn("[info]Speed filter …"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("points", total=len(points) - 1)

        for i in range(1, len(points)):
            cur      = points[i]
            prev     = kept[-1]
            prev_raw = points[i - 1]   # immediate predecessor in sorted array
            nxt      = points[i + 1] if i + 1 < len(points) else None

            # ── zero-dt branch ────────────────────────────────────────────────
            # Two points share the same timestamp.  speed_knots() would return None
            # (dt ≈ 0), causing the normal branch to keep the point unconditionally.
            #
            # We handle this in two steps:
            #
            # (a) Propagate-drop: if the raw predecessor was itself dropped (it was a
            #     ghost fix), drop this point too.  A ghost fix often appears in the
            #     sorted list as a pair — the original ghost and then a duplicate of it
            #     from a second logger — both at the same impossible position.  Without
            #     this rule the duplicate would be kept and corrupt kept[-1].
            #
            # (b) Spatial check: if the raw predecessor was kept, compare positions.
            #     If they share a timestamp but are more than one second's worth of
            #     travel apart (~26 m at 50 kn), this is a ghost fix from a second GPS
            #     recording the same instant but placing the vessel somewhere else.
            if (cur.time is not None
                    and prev_raw.time is not None
                    and abs((cur.time - prev_raw.time).total_seconds()) < 1e-6):
                if (i - 1) not in _kept_indices:
                    # (a) raw predecessor was dropped → propagate drop
                    stats.points_speed_drop += 1
                    log(
                        VERBOSITY_DEBUG, verbosity, "warn",
                        f"    DROP (zero-dt propagate)  [{cur.source}] "
                        f"{cur.lat:.5f},{cur.lon:.5f}  raw_prev was dropped",
                    )
                else:
                    # (b) raw predecessor was kept → spatial check
                    d_raw_prev = haversine_m(prev_raw.lat, prev_raw.lon, cur.lat, cur.lon)
                    if d_raw_prev > _one_sec_max_m:
                        stats.points_speed_drop += 1
                        log(
                            VERBOSITY_DEBUG, verbosity, "warn",
                            f"    DROP (zero-dt ghost)  [{cur.source}] "
                            f"{cur.lat:.5f},{cur.lon:.5f}  "
                            f"d_raw_prev={d_raw_prev:.0f} m at dt=0",
                        )
                    else:
                        log(VERBOSITY_TRACE, verbosity, "trace",
                            f"    [{cur.source}] {cur.lat:.5f},{cur.lon:.5f} "
                            f"— zero-dt duplicate, kept")
                        kept.append(cur)
                        _kept_indices.add(i)
                progress.advance(task)
                continue

            # ── normal branch (dt > 0) ────────────────────────────────────────
            s_from_prev = speed_knots(prev, cur)
            s_to_next   = speed_knots(cur, nxt) if nxt else None

            # If we can't compute speed (missing timestamps on THIS point), keep it
            if s_from_prev is None:
                log(VERBOSITY_TRACE, verbosity, "trace",
                    f"    [{cur.source}] {cur.lat:.5f},{cur.lon:.5f} — no timestamp, kept")
                kept.append(cur)
                _kept_indices.add(i)
                progress.advance(task)
                continue

            over_limit_from = s_from_prev > max_speed_knots

            # One-sided drop: if the distance from the last *kept* point is
            # physically impossible regardless of what comes next, drop immediately.
            # This catches cases where two interleaved GPS sources alternate such
            # that the OUTGOING leg always looks slow (next point is back on the
            # same source), allowing the bad point to sneak through the two-sided
            # check.  A sailboat genuinely cannot be more than
            # max_speed × elapsed_time away from the previous fix.
            if prev.time and cur.time:
                dt_s = abs((cur.time - prev.time).total_seconds())
                max_plausible_m = (max_speed_knots / 1.94384) * dt_s
                actual_m = haversine_m(prev.lat, prev.lon, cur.lat, cur.lon)
                impossible = actual_m > max_plausible_m
            else:
                impossible = False

            # s_to_next is None when:
            #   (a) nxt has no timestamp → genuinely unknown → be conservative, keep
            #   (b) cur and nxt share the same timestamp (dt=0) → degenerate step,
            #       treat the same as being at the end of the list (drop if over limit)
            nxt_has_no_time = (nxt is not None and nxt.time is None)
            nxt_simultaneous = (nxt is not None and s_to_next is None and not nxt_has_no_time)

            if s_to_next is not None:
                over_limit_to = s_to_next > max_speed_knots
            elif nxt is None or nxt_simultaneous:
                # Last point, or next point is simultaneous (dt=0): outgoing leg
                # is degenerate — count as over limit if incoming is already over
                over_limit_to = over_limit_from
            else:
                # Next point has no timestamp — can't assess, be conservative
                over_limit_to = False

            if impossible or (over_limit_from and over_limit_to):
                stats.points_speed_drop += 1
                log(
                    VERBOSITY_DEBUG, verbosity, "warn",
                    f"    DROP  [{cur.source}] {cur.lat:.5f},{cur.lon:.5f} "
                    f"speed {s_from_prev:.0f} kn → "
                    f"{f'{s_to_next:.0f}' if s_to_next is not None else '?'} kn",
                )
            else:
                log(VERBOSITY_TRACE, verbosity, "trace",
                    f"    keep  [{cur.source}] {cur.lat:.5f},{cur.lon:.5f} "
                    f"speed {s_from_prev:.1f} kn")
                kept.append(cur)
                _kept_indices.add(i)

            progress.advance(task)

    log(VERBOSITY_INFO, verbosity, "info",
        f"    Dropped {stats.points_speed_drop:,} anomalous points.  "
        f"{len(kept):,} remain.")
    return kept


# ── phase 3b: longitude-jump filter ──────────────────────────────────────────
def filter_longitude_jumps(
    points: list[Point],
    max_lon_jump_deg: float,
    verbosity: int,
    stats: Stats,
) -> list[Point]:
    """
    Remove points whose longitude differs from BOTH neighbours by more than
    max_lon_jump_deg degrees.

    This catches GPS glitches near the antimeridian (±180°) that the speed
    filter misses.  When a boat is crossing the date line near lon=±179°, a
    bad fix can land at e.g. lon=-60° (South Atlantic).  Haversine wraps the
    longitude correctly and returns the geodesic distance, but if the bad
    point is between two points that are themselves on opposite sides of ±180°
    the geodesic distance to the nearest neighbour can appear deceptively small
    (the sphere's shortest path crosses the antimeridian), fooling the speed
    filter.

    The longitude-jump check is purely angular and does NOT wrap at ±180: we
    compare raw degree differences.  A point at lon=-60 surrounded by points
    at lon=+179 and lon=-179 has |Δlon| of 239° and 119° respectively — both
    far above any plausible single-step longitude change.

    For filtering, we require BOTH neighbours to show a large jump: this
    prevents the filter from dropping valid points at the start or end of a
    long eastward or westward passage.

    Default threshold: 90°.  A sailboat cannot legitimately move 90° of
    longitude (~10,000 km at the equator) in a single GPS fix interval.
    """
    log(VERBOSITY_INFO, verbosity, "info",
        f"🌐  Longitude-jump filter: dropping points with |Δlon| > "
        f"{max_lon_jump_deg:.0f}° from BOTH neighbours …")

    if len(points) < 3:
        return points

    kept: list[Point] = [points[0]]
    dropped = 0

    for i in range(1, len(points) - 1):
        prev = kept[-1]
        cur  = points[i]
        nxt  = points[i + 1]

        d_prev = abs(cur.lon - prev.lon)
        d_next = abs(cur.lon - nxt.lon)

        if d_prev > max_lon_jump_deg and d_next > max_lon_jump_deg:
            dropped += 1
            stats.points_lonjump_drop += 1
            log(
                VERBOSITY_DEBUG, verbosity, "warn",
                f"    lonjump-DROP  [{cur.source}] {cur.lat:.5f},{cur.lon:.5f}  "
                f"|Δlon_prev|={d_prev:.1f}°  |Δlon_next|={d_next:.1f}°",
            )
        else:
            kept.append(cur)

    kept.append(points[-1])

    log(VERBOSITY_INFO, verbosity, "info",
        f"    Dropped {dropped:,} longitude-jump points.  "
        f"{len(kept):,} remain.")
    return kept


# ── phase 4: cross-track sanity filter (iterative) ───────────────────────────
def _crosstrack_pass(
    points: list[Point],
    max_crosstrack_m: float,
    max_crosstrack_rate_m_per_h: float,
    verbosity: int,
) -> tuple[list[Point], int]:
    """
    Single pass: examine every interior point and drop it if it is a geometric
    outlier relative to its neighbours, *unless* the time gap to its neighbours
    is large enough that a big positional deviation is plausible (i.e. the track
    legitimately crossed over itself on a different passage).

    The test is:
        cross_track_distance > max_crosstrack_m
        AND
        cross_track_distance / time_gap_hours > max_crosstrack_rate_m_per_h

    The second condition is the self-crossing guard: if the two neighbours are
    days apart, a deviation of many km is fine. The rate threshold converts the
    raw distance limit into a distance-per-hour budget — at the default of
    50 kn ≈ 93 km/h, a 1,000 m deviation is only suspicious if the neighbours
    are less than ~10 minutes apart.

    Returns (kept_points, n_dropped).
    """
    if len(points) < 3:
        return points, 0

    kept: list[Point] = [points[0]]
    dropped = 0

    for i in range(1, len(points) - 1):
        prev = kept[-1]          # last kept point (not necessarily points[i-1])
        cur  = points[i]
        nxt  = points[i + 1]

        # Cannot do geometry without two valid neighbours
        xt = cross_track_distance_m(
            prev.lat, prev.lon,
            nxt.lat,  nxt.lon,
            cur.lat,  cur.lon,
        )

        if xt <= max_crosstrack_m:
            log(VERBOSITY_TRACE, verbosity, "trace",
                f"    xt-keep  {cur.lat:.5f},{cur.lon:.5f}  xt={xt:.0f} m")
            kept.append(cur)
            continue

        # Point is geometrically far off the prev→next line.
        # Check if the time span to its *nearest* neighbour is large — if so,
        # the boat was simply in a different part of the ocean at a different
        # time and the self-crossing guard should let it through.
        #
        # We use the MINIMUM of the two leg gaps (prev→cur, cur→nxt) so that
        # a spike which is only 2 minutes from one neighbour is caught even
        # if the other neighbour is hours away.
        gap_prev_h = time_gap_hours(prev, cur)
        gap_next_h = time_gap_hours(cur, nxt)

        if gap_prev_h is None and gap_next_h is None:
            # No timestamps — can't apply rate guard, keep the point
            log(VERBOSITY_TRACE, verbosity, "trace",
                f"    xt-keep (no time)  {cur.lat:.5f},{cur.lon:.5f}  xt={xt:.0f} m")
            kept.append(cur)
            continue

        # Use whichever gap we have; prefer the smaller one
        available = [g for g in (gap_prev_h, gap_next_h) if g is not None]
        gap_h = min(available)   # tightest constraint wins

        if gap_h < 1e-6:
            # Simultaneous neighbour — deviation is definitely an error
            rate = float("inf")
        else:
            rate = xt / gap_h   # metres per hour

        if rate > max_crosstrack_rate_m_per_h:
            dropped += 1
            log(
                VERBOSITY_DEBUG, verbosity, "warn",
                f"    xt-DROP  [{cur.source}] {cur.lat:.5f},{cur.lon:.5f}  "
                f"xt={xt:.0f} m  min-gap={gap_h*60:.1f} min  rate={rate:.0f} m/h",
            )
        else:
            # Large deviation but spread over many hours — legitimate crossing
            log(
                VERBOSITY_DEBUG, verbosity, "debug",
                f"    xt-keep (self-crossing guard)  {cur.lat:.5f},{cur.lon:.5f}  "
                f"xt={xt:.0f} m  min-gap={gap_h*60:.1f} min  rate={rate:.0f} m/h",
            )
            kept.append(cur)

    # Always keep the last point
    kept.append(points[-1])
    return kept, dropped


def filter_crosstrack_anomalies(
    points: list[Point],
    max_crosstrack_m: float,
    max_crosstrack_rate_m_per_h: float,
    max_passes: int,
    verbosity: int,
    stats: Stats,
) -> list[Point]:
    """
    Iteratively apply the cross-track filter until no more points are dropped
    or max_passes is reached.

    Iteration is necessary because dropping one outlier can unmask the next:
    e.g. a cluster of three consecutive bad points where each looks reasonable
    relative to its immediate neighbours will only be fully cleaned after
    successive passes.
    """
    log(
        VERBOSITY_INFO, verbosity, "info",
        f"📐  Cross-track sanity filter: max deviation {max_crosstrack_m:.0f} m, "
        f"rate guard {max_crosstrack_rate_m_per_h:.0f} m/h, "
        f"up to {max_passes} pass(es) …",
    )

    pass_num = 0
    total_dropped = 0

    while pass_num < max_passes:
        pass_num += 1
        points, n_dropped = _crosstrack_pass(
            points, max_crosstrack_m, max_crosstrack_rate_m_per_h, verbosity
        )
        total_dropped += n_dropped
        stats.crosstrack_passes = pass_num

        log(
            VERBOSITY_INFO, verbosity, "info",
            f"    Pass {pass_num}: dropped {n_dropped:,} points "
            f"({len(points):,} remain).",
        )

        if n_dropped == 0:
            log(VERBOSITY_INFO, verbosity, "good",
                f"    ✓ Converged after {pass_num} pass(es).")
            break
    else:
        if total_dropped > 0:
            log(VERBOSITY_INFO, verbosity, "warn",
                f"    ⚠  Reached pass limit ({max_passes}); "
                f"{total_dropped:,} total points dropped. "
                "Consider increasing --passes.")

    stats.points_crosstrack_drop += total_dropped
    return points


# ── phase 5: distance decimation ──────────────────────────────────────────────
def decimate_points(
    points: list[Point],
    min_distance_m: float,
    merge_distance_m: float,
    verbosity: int,
    stats: Stats,
) -> list[Point]:
    """
    Walk the sorted point list and produce a new list where:
    • Points closer than merge_distance_m to the previous *output* point are dropped.
    • Once accumulated distance >= min_distance_m, the centroid of accumulated
      points is emitted as the next output point.

    The centroid approach avoids simply picking one of a cluster of GPS fixes;
    instead it averages them so the emitted point sits in the middle of a tight
    cluster from overlapping tracks.
    """
    log(VERBOSITY_INFO, verbosity, "info",
        f"📏  Decimating: keep points ≥ {min_distance_m:.0f} m apart "
        f"(merge threshold {merge_distance_m:.0f} m) …")

    if not points:
        return points

    output: list[Point] = []

    # Accumulator for the current "cluster" being merged
    cluster_lats: list[float] = []
    cluster_lons: list[float] = []
    cluster_eles: list[float] = []
    cluster_times: list[datetime] = []
    cluster_source: str = ""

    # Last *emitted* position — used for distance check
    last_emitted_lat: float = points[0].lat
    last_emitted_lon: float = points[0].lon
    accumulated_dist: float = 0.0

    def mean_longitude(lons: list[float]) -> float:
        """
        Compute the mean longitude, correctly handling the antimeridian (±180°).

        Naive averaging of e.g. [-179.99, +179.99] gives 0°, which is wrong —
        the two points are less than 0.02° apart across the date line.

        We convert each longitude to a unit vector on the circle (cos, sin),
        average the vectors, and take atan2 of the result.  This gives the
        correct circular mean regardless of whether the cluster straddles ±180°.
        """
        import math as _math
        sx = sum(_math.cos(_math.radians(lon)) for lon in lons)
        sy = sum(_math.sin(_math.radians(lon)) for lon in lons)
        return _math.degrees(_math.atan2(sy, sx))

    def flush_cluster() -> Optional[Point]:
        """Emit the centroid of the current cluster as one output point."""
        if not cluster_lats:
            return None
        avg_lat = sum(cluster_lats) / len(cluster_lats)
        avg_lon = mean_longitude(cluster_lons)
        avg_ele = (sum(cluster_eles) / len(cluster_eles)) if cluster_eles else None
        # Use median time (middle index) to keep a real timestamp
        t = sorted(cluster_times)[len(cluster_times) // 2] if cluster_times else None
        return Point(lat=avg_lat, lon=avg_lon, ele=avg_ele, time=t, source=cluster_source)

    def reset_cluster(p: Point) -> None:
        cluster_lats.clear()
        cluster_lons.clear()
        cluster_eles.clear()
        cluster_times.clear()
        cluster_lats.append(p.lat)
        cluster_lons.append(p.lon)
        if p.ele is not None:
            cluster_eles.append(p.ele)
        if p.time is not None:
            cluster_times.append(p.time)
        nonlocal cluster_source
        cluster_source = p.source

    with Progress(
        SpinnerColumn(),
        TextColumn("[info]Decimating …"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("points", total=len(points))

        for pt in points:
            d = haversine_m(last_emitted_lat, last_emitted_lon, pt.lat, pt.lon)

            if d < merge_distance_m:
                # Too close to the last emitted point — merge into current cluster
                stats.points_merge_drop += 1
                log(VERBOSITY_TRACE, verbosity, "trace",
                    f"    merge [{pt.source}] {pt.lat:.5f},{pt.lon:.5f}  d={d:.1f} m")
                cluster_lats.append(pt.lat)
                cluster_lons.append(pt.lon)
                if pt.ele is not None:
                    cluster_eles.append(pt.ele)
                if pt.time is not None:
                    cluster_times.append(pt.time)
            else:
                accumulated_dist += d

                if accumulated_dist >= min_distance_m:
                    # Emit the cluster centroid
                    emitted = flush_cluster()
                    if emitted:
                        output.append(emitted)
                        stats.points_out += 1
                        last_emitted_lat = emitted.lat
                        last_emitted_lon = emitted.lon

                        # Running distance tally
                        if len(output) > 1:
                            stats.total_dist_km += haversine_m(
                                output[-2].lat, output[-2].lon,
                                emitted.lat, emitted.lon
                            ) / 1000.0
                        stats.update_bbox(emitted.lat, emitted.lon)

                        log(VERBOSITY_TRACE, verbosity, "trace",
                            f"    emit  {emitted.lat:.5f},{emitted.lon:.5f} "
                            f"(cluster of {len(cluster_lats)} pts, "
                            f"cum dist {accumulated_dist:.0f} m)")

                        accumulated_dist = 0.0
                    reset_cluster(pt)
                else:
                    # Not far enough yet — accumulate
                    cluster_lats.append(pt.lat)
                    cluster_lons.append(pt.lon)
                    if pt.ele is not None:
                        cluster_eles.append(pt.ele)
                    if pt.time is not None:
                        cluster_times.append(pt.time)

            progress.advance(task)

        # Flush any remaining cluster
        emitted = flush_cluster()
        if emitted:
            output.append(emitted)
            stats.points_out += 1
            stats.update_bbox(emitted.lat, emitted.lon)

    log(VERBOSITY_INFO, verbosity, "info",
        f"    Emitted {stats.points_out:,} output points "
        f"({stats.points_merge_drop:,} merged).")
    return output


# ── phase 6: elevation-spike filter (post-decimation) ────────────────────────
def filter_elevation_anomalies(
    points: list[Point],
    max_ele_change_m: float,
    verbosity: int,
    stats: Stats,
) -> list[Point]:
    """
    Scrub implausible elevation values from the already-decimated point list.

    This runs AFTER decimation deliberately.  Running it before decimation
    caused a severe problem: dropping 30%+ of points before the distance
    accumulator ran meant that tiny track fragments survived decimation as
    individual points, inflating the output count from ~5k to ~114k on a
    real 40 MB file.

    Rather than dropping the whole point (which would remove a valid position
    and could re-introduce the same fragmentation problem), we NULL OUT the
    bad elevation value while keeping the lat/lon.  The point stays in the
    track; it just won't have an altitude tag in the output.

    A sailing GPS should never record altitude jumps of tens of metres between
    adjacent fixes at sea.  Any such jump is sensor noise.
    """
    log(VERBOSITY_INFO, verbosity, "info",
        f"⛰️   Scrubbing elevation spikes > ±{max_ele_change_m:.0f} m …")

    if not points:
        return points

    last_ele: Optional[float] = None   # last clean elevation seen

    with Progress(
        SpinnerColumn(),
        TextColumn("[info]Elevation filter …"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("points", total=len(points))

        for pt in points:
            if pt.ele is None or last_ele is None:
                if pt.ele is not None:
                    last_ele = pt.ele
                progress.advance(task)
                continue

            delta = abs(pt.ele - last_ele)

            if delta > max_ele_change_m:
                stats.points_ele_drop += 1
                log(
                    VERBOSITY_DEBUG, verbosity, "warn",
                    f"    ele-SCRUB  [{pt.source}] {pt.lat:.5f},{pt.lon:.5f}  "
                    f"ele={pt.ele:.1f} m  prev={last_ele:.1f} m  delta={delta:.1f} m",
                )
                pt.ele = None   # null out bad elevation, keep position
            else:
                last_ele = pt.ele
                log(VERBOSITY_TRACE, verbosity, "trace",
                    f"    ele-ok  {pt.lat:.5f},{pt.lon:.5f}  "
                    f"ele={pt.ele:.1f} m  delta={delta:.1f} m")

            progress.advance(task)

    log(VERBOSITY_INFO, verbosity, "info",
        f"    Scrubbed elevation on {stats.points_ele_drop:,} points "
        f"({len(points):,} positions unchanged).")
    return points


# ── phase 7: duplicate-timestamp deduplication ───────────────────────────────
def deduplicate_timestamps(
    points: list[Point],
    verbosity: int,
    stats: Stats,
) -> list[Point]:
    """
    Remove any output point that shares an identical timestamp with the
    immediately preceding point.

    This can happen after the centroid-averaging step when two source tracks
    both have a fix at the same clock second but at different positions — both
    survive the distance filter but end up adjacent in the output with the same
    time value. Many GPX applications (including GPX Editor on macOS) attempt
    to compute speed or heading between consecutive points by dividing distance
    by time delta; a zero delta causes a divide-by-zero or infinite loop.

    When a duplicate is found, the *first* of the pair is kept (it represents
    the earlier-arriving source data) and the second is dropped.
    Points without timestamps are never dropped by this pass.
    """
    log(VERBOSITY_INFO, verbosity, "info",
        "🕐  Deduplicating adjacent same-timestamp points …")

    if not points:
        return points

    kept: list[Point] = [points[0]]

    for pt in points[1:]:
        prev = kept[-1]
        if (pt.time is not None
                and prev.time is not None
                and pt.time == prev.time):
            stats.points_duptime_drop += 1
            log(VERBOSITY_DEBUG, verbosity, "debug",
                f"    dup-DROP  {pt.lat:.6f},{pt.lon:.6f}  t={pt.time.isoformat()}")
        else:
            kept.append(pt)

    if stats.points_duptime_drop:
        log(VERBOSITY_INFO, verbosity, "info",
            f"    Dropped {stats.points_duptime_drop:,} duplicate-timestamp points.  "
            f"{len(kept):,} remain.")
    else:
        log(VERBOSITY_INFO, verbosity, "info",
            "    No duplicate timestamps found.")

    return kept


# ── phase 7b: post-decimation speed filter ───────────────────────────────────
def filter_output_speed(
    points: list[Point],
    max_speed_knots: float,
    verbosity: int,
    stats: Stats,
) -> list[Point]:
    """
    Re-apply the impossible-distance speed check to the decimated output.

    After decimation, centroid centroids from two interleaved GPS sources can
    end up adjacent in the output with a physically impossible apparent speed
    (e.g. 494 m in 1 second = 961 kn), even though no individual raw point
    exceeded the speed threshold.  This happens because:

      • Both sources are internally consistent at slow speeds.
      • Each source's cluster is just over the min-distance threshold apart.
      • The centroid timestamps (median of cluster times) happen to be only
        1–14 seconds apart across the two adjacent clusters.

    The pre-decimation speed filter cannot catch this because it operates on
    raw points, not on the averaged centroids.

    This pass uses the same one-sided impossible-distance check: if the
    distance from the previous *kept* output point is greater than
    max_speed × elapsed_time, the point is dropped.
    """
    log(VERBOSITY_INFO, verbosity, "info",
        f"🚀  Post-decimation speed check (max {max_speed_knots:.0f} kn) …")

    if not points:
        return points

    kept: list[Point] = [points[0]]
    dropped = 0
    max_mps = max_speed_knots / 1.94384

    for pt in points[1:]:
        prev = kept[-1]
        if prev.time and pt.time:
            dt_s = abs((pt.time - prev.time).total_seconds())
            if dt_s > 0:
                max_plaus_m = max_mps * dt_s
                actual_m = haversine_m(prev.lat, prev.lon, pt.lat, pt.lon)
                if actual_m > max_plaus_m:
                    dropped += 1
                    stats.points_speed_drop += 1
                    log(VERBOSITY_DEBUG, verbosity, "debug",
                        f"    out-spd-DROP  {pt.lat:.6f},{pt.lon:.6f}  "
                        f"d={actual_m:.0f}m  dt={dt_s:.0f}s  "
                        f"max={max_plaus_m:.0f}m  t={pt.time}")
                    continue
        kept.append(pt)

    if dropped:
        log(VERBOSITY_INFO, verbosity, "info",
            f"    Dropped {dropped:,} impossible-speed output points.  "
            f"{len(kept):,} remain.")
    else:
        log(VERBOSITY_INFO, verbosity, "info",
            "    No impossible-speed output points.")

    return kept


# ── phase 8: duplicate-position deduplication ────────────────────────────────
def deduplicate_positions(
    points: list[Point],
    verbosity: int,
    stats: Stats,
) -> list[Point]:
    """
    Remove any output point that has an identical rounded lat/lon to the
    immediately preceding point.

    After coordinates are rounded to 6 decimal places (~11 cm) at write time,
    adjacent points near the antimeridian (lon ≈ ±179.999°) can map to the
    same rounded position even if they were distinct before rounding.  Such
    zero-distance steps cause divide-by-zero or infinite-speed calculations in
    downstream GPX tools (including GPX Editor on macOS).

    We round to 6 dp here to match the write_gpx rounding, so what we discard
    is exactly what would produce zero-distance steps in the output file.

    When a duplicate is found, the *first* of the pair is kept.
    """
    log(VERBOSITY_INFO, verbosity, "info",
        "📍  Deduplicating adjacent same-position points (after 6-dp rounding) …")

    if not points:
        return points

    kept: list[Point] = [points[0]]

    for pt in points[1:]:
        prev = kept[-1]
        if (round(pt.lat, 6) == round(prev.lat, 6)
                and round(pt.lon, 6) == round(prev.lon, 6)):
            stats.points_duppos_drop += 1
            log(VERBOSITY_DEBUG, verbosity, "debug",
                f"    duppos-DROP  {pt.lat:.6f},{pt.lon:.6f}  t={pt.time}")
        else:
            kept.append(pt)

    if stats.points_duppos_drop:
        log(VERBOSITY_INFO, verbosity, "info",
            f"    Dropped {stats.points_duppos_drop:,} duplicate-position points.  "
            f"{len(kept):,} remain.")
    else:
        log(VERBOSITY_INFO, verbosity, "info",
            "    No duplicate positions found.")

    return kept


# ── phase 8b: zero-speed ghost filter ────────────────────────────────────────
def filter_zero_speed_ghosts(
    points: list[Point],
    neighbour_window: int,
    max_neighbour_dist_m: float,
    verbosity: int,
    stats: Stats,
) -> list[Point]:
    """
    Scan the decimated output for adjacent point pairs that are within a
    negligible distance of each other (effectively zero speed between them).
    For each such pair, examine the `neighbour_window` points on each side,
    **excluding both points in the zero-distance pair itself**.  If no
    external neighbour is within `max_neighbour_dist_m`, the pair is an
    isolated ghost island: both points are dropped.

    If the pair's position IS within `max_neighbour_dist_m` of at least one
    external neighbour the position is plausible (boat at anchor, etc.) and
    neither point is dropped.

    Why exclude both pair members from the neighbour set?
    A ghost island is exactly two consecutive points at the same position,
    thousands of kilometres from the real track.  If we included point i in
    the neighbour set when evaluating point i+1 (or vice versa), the inter-
    pair distance of 0 m would always satisfy the threshold, masking the
    isolation.  Excluding the pair itself forces the test to look at genuine
    external context.

    A single isolated output point (not preceded by a zero-distance step) is
    not caught here — it is handled by the single-point segment drop in
    split_into_segments.
    """
    log(VERBOSITY_INFO, verbosity, "info",
        "🔍  Phase 8b: zero-speed ghost filter …")

    if len(points) < 3:
        log(VERBOSITY_INFO, verbosity, "info",
            "    Too few points to filter — skipping.")
        return points

    # Threshold for "zero speed": adjacent output points closer than 1 m
    ZERO_DIST_M = 1.0

    drop_indices: set[int] = set()

    i = 0
    while i < len(points) - 1:
        if i in drop_indices:
            i += 1
            continue

        p1, p2 = points[i], points[i + 1]
        d = haversine_m(p1.lat, p1.lon, p2.lat, p2.lon)
        if d >= ZERO_DIST_M:
            i += 1
            continue

        # Found a zero-distance pair (i, i+1).  Collect external neighbours —
        # points within the window that are NOT part of this pair.
        pair_indices = {i, i + 1}
        lo = max(0, i - neighbour_window)
        hi = min(len(points) - 1, i + 1 + neighbour_window)
        neighbours: list[tuple[float, float]] = [
            (points[j].lat, points[j].lon)
            for j in range(lo, hi + 1)
            if j not in pair_indices and j not in drop_indices
        ]

        if not neighbours:
            # No external context available (e.g. start/end of track).
            # Cannot make a safe determination — keep both points.
            log(VERBOSITY_TRACE, verbosity, "trace",
                f"    zero-spd  NO-CONTEXT  [{i},{i+1}]  "
                f"{p1.lat:.5f},{p1.lon:.5f}  (keeping, no external neighbours)")
            i += 1
            continue

        # Minimum distance from the pair's position to any external neighbour.
        min_dist = min(
            haversine_m(p1.lat, p1.lon, nlat, nlon)
            for nlat, nlon in neighbours
        )

        if min_dist <= max_neighbour_dist_m:
            # Position is consistent with local context — genuine stationary.
            log(VERBOSITY_TRACE, verbosity, "trace",
                f"    zero-spd  OK  [{i},{i+1}]  {p1.lat:.5f},{p1.lon:.5f}  "
                f"min_ext_neighbour={min_dist/1000:.1f} km  (stationary)")
            i += 1
            continue

        # Both points are remote from all external neighbours — ghost island.
        log(VERBOSITY_DEBUG, verbosity, "debug",
            f"    DROP (zero-spd ghost pair) [{i},{i+1}]  "
            f"{p1.lat:.5f},{p1.lon:.5f}  "
            f"t={p1.time}  min_ext_neighbour={min_dist/1000:.1f} km")
        drop_indices.add(i)
        drop_indices.add(i + 1)
        stats.points_zerospd_drop += 2
        i += 2  # skip both — the next pair check starts fresh

    if drop_indices:
        kept = [p for j, p in enumerate(points) if j not in drop_indices]
        log(VERBOSITY_INFO, verbosity, "info",
            f"    Dropped {stats.points_zerospd_drop:,} zero-speed ghost point(s).  "
            f"{len(kept):,} remain.")
    else:
        kept = points
        log(VERBOSITY_INFO, verbosity, "info",
            "    No zero-speed ghosts found.")

    return kept


# ── reverse geocoding (Nominatim / OpenStreetMap) ─────────────────────────────
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_NOMINATIM_UA  = "gpx_simplify/1.0 (sailing track tool; contact via github.com/farotherside/gpx_simplify)"
_geocode_cache: dict[tuple[float, float], str] = {}


def reverse_geocode(lat: float, lon: float, timeout: float = 5.0) -> str:
    """
    Return a human-readable location description for (lat, lon) using the
    Nominatim reverse-geocoding API (OpenStreetMap).

    The result is a short, landmark-first string built from the response's
    address fields, e.g.:
        "Watsons Bay, Sydney, New South Wales, Australia"
        "Tasman Sea (~480 km E of Sydney)"   ← ocean fallback
        "13.4521°N, 144.7937°E"              ← network-error fallback

    Results are cached by rounded coordinate (0.01°) so repeated lookups
    for nearby points don't hammer the API.  A polite User-Agent is sent
    as required by Nominatim's usage policy.
    """
    cache_key = (round(lat, 2), round(lon, 2))
    if cache_key in _geocode_cache:
        return _geocode_cache[cache_key]

    params = urllib.parse.urlencode({
        "lat": f"{lat:.6f}",
        "lon": f"{lon:.6f}",
        "format": "jsonv2",
        "zoom": 10,          # city / suburb level
        "addressdetails": 1,
        "accept-language": "en",
    })
    url = f"{_NOMINATIM_URL}?{params}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": _NOMINATIM_UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        # Network error, rate-limit, or JSON parse failure — fall back to coords.
        result = f"{abs(lat):.4f}°{'N' if lat >= 0 else 'S'}, {abs(lon):.4f}°{'E' if lon >= 0 else 'W'}"
        _geocode_cache[cache_key] = result
        return result

    addr = data.get("address", {})
    display = data.get("display_name", "")

    # Build a concise landmark-first description from available address fields.
    # Priority: named place / suburb / town → city/county → state → country.
    parts: list[str] = []
    for key in ("tourism", "amenity", "leisure", "natural", "hamlet",
                "village", "suburb", "quarter", "neighbourhood",
                "town", "city", "municipality"):
        v = addr.get(key)
        if v and v not in parts:
            parts.append(v)
        if len(parts) >= 2:
            break

    for key in ("county", "state_district", "state", "region", "country"):
        v = addr.get(key)
        if v and v not in parts:
            parts.append(v)
        if len(parts) >= 4:
            break

    if parts:
        result = ", ".join(parts)
    elif display:
        # Nominatim returned something but address fields were empty — use the
        # display_name but trim it to the first 3 comma-separated tokens.
        tokens = [t.strip() for t in display.split(",")]
        result = ", ".join(tokens[:3])
    else:
        result = f"{abs(lat):.4f}°{'N' if lat >= 0 else 'S'}, {abs(lon):.4f}°{'E' if lon >= 0 else 'W'}"

    _geocode_cache[cache_key] = result
    return result


def location_label(lat: float, lon: float) -> str:
    """
    Return a combined coordinate + landmark string for display, e.g.:
        -33.8568°, 151.2153° (Watsons Bay, Sydney, New South Wales, Australia)
    Falls back to just the coordinate string if geocoding fails silently.
    """
    coord = (f"{abs(lat):.4f}°{'N' if lat >= 0 else 'S'}, "
             f"{abs(lon):.4f}°{'E' if lon >= 0 else 'W'}")
    place = reverse_geocode(lat, lon)
    # If place IS the coordinate (fallback), don't duplicate it.
    if place.startswith(coord[:6]):
        return coord
    return f"{coord}  ({place})"


# ── phase 8c: gap detection and interpolation ─────────────────────────────────
CONTEXT_WINDOW = 10   # number of points before/after a gap used to estimate speed


@dataclass
class GapInfo:
    """Describes a single detected gap between two adjacent output points."""
    idx_before:     int           # index of last point before the gap
    idx_after:      int           # index of first point after the gap
    pt_before:      Point         # last point before the gap
    pt_after:       Point         # first point after the gap
    gap_dist_m:     float         # great-circle distance across the gap
    gap_time_h:     float         # time span of the gap in hours
    before_kn:      float         # average speed (kn) of CONTEXT_WINDOW pts before gap
    after_kn:       float         # average speed (kn) of CONTEXT_WINDOW pts after gap
    fill_kn:        float         # speed used for interpolation (mean of before/after)


def _context_speed_kn(points: list[Point], centre_idx: int, look_before: bool) -> float:
    """
    Compute average speed (knots) over up to CONTEXT_WINDOW consecutive
    point-to-point legs immediately before or after `centre_idx`.

    Returns 0.0 if there are too few points or all legs have zero time.
    """
    if look_before:
        lo = max(0, centre_idx - CONTEXT_WINDOW)
        window = points[lo : centre_idx + 1]
    else:
        hi = min(len(points), centre_idx + CONTEXT_WINDOW + 1)
        window = points[centre_idx : hi]

    if len(window) < 2:
        return 0.0

    total_dist_m = 0.0
    total_dt_s   = 0.0
    for a, b in zip(window[:-1], window[1:]):
        if a.time is None or b.time is None:
            continue
        dt = abs((b.time - a.time).total_seconds())
        if dt < 1e-6:
            continue
        total_dist_m += haversine_m(a.lat, a.lon, b.lat, b.lon)
        total_dt_s   += dt

    if total_dt_s < 1e-6:
        return 0.0
    mps = total_dist_m / total_dt_s
    return mps * 1.94384  # m/s → knots


def detect_gaps(
    points: list[Point],
    split_gap_hours: float,
    verbosity: int,
) -> list[GapInfo]:
    """
    Walk the point list and identify every adjacent pair (i, i+1) where:
      - the time gap exceeds split_gap_hours, AND
      - the great-circle distance between them is greater than zero
        (i.e. the vessel has moved — it's not just stopped in port).

    Returns a list of GapInfo objects in order of occurrence.
    """
    gaps: list[GapInfo] = []
    for i in range(len(points) - 1):
        pa, pb = points[i], points[i + 1]
        if pa.time is None or pb.time is None:
            continue
        dt_h = (pb.time - pa.time).total_seconds() / 3600.0
        if dt_h <= split_gap_hours:
            continue
        dist_m = haversine_m(pa.lat, pa.lon, pb.lat, pb.lon)
        # Gaps under 0.5 nm are already handled by bridge_small_gaps and
        # should never reach here.  Treat anything under 0.5 nm as a
        # stationary or near-stationary stop — nothing to prompt about.
        if dist_m < 926.0:   # 0.5 nm in metres
            log(VERBOSITY_DEBUG, verbosity, "debug",
                f"    gap at [{i}→{i+1}]  dt={dt_h:.1f} h  dist={dist_m:.0f} m  "
                f"(< 0.5 nm, skip)")
            continue

        before_kn = _context_speed_kn(points, i,     look_before=True)
        after_kn  = _context_speed_kn(points, i + 1, look_before=False)
        # Use the mean of the two context speeds; if one side is 0 use the other.
        if before_kn > 0 and after_kn > 0:
            fill_kn = (before_kn + after_kn) / 2.0
        else:
            fill_kn = max(before_kn, after_kn)
        # Clamp to a sensible sailing range if context is unavailable
        if fill_kn < 0.1:
            fill_kn = 5.0   # 5 kn default if we have no useful context

        gaps.append(GapInfo(
            idx_before=i,
            idx_after=i + 1,
            pt_before=pa,
            pt_after=pb,
            gap_dist_m=dist_m,
            gap_time_h=dt_h,
            before_kn=before_kn,
            after_kn=after_kn,
            fill_kn=fill_kn,
        ))
        log(VERBOSITY_DEBUG, verbosity, "debug",
            f"    gap at [{i}→{i+1}]  dt={dt_h:.1f} h  "
            f"dist={dist_m/1852:.1f} nm  "
            f"ctx_speed={fill_kn:.1f} kn")

    return gaps


def interpolate_gap(gap: GapInfo, min_distance_m: float) -> list[Point]:
    """
    Generate interpolated track points that fill `gap` at `min_distance_m`
    spacing, travelling along the great-circle from pt_before to pt_after.

    The points are timed by dividing the gap interval proportionally to
    distance (constant speed = fill_kn across the whole gap).  Elevation
    is linearly interpolated if both endpoints have elevation data.

    Returns the list of new interior points (does NOT include the endpoints).
    """
    pa, pb = gap.pt_before, gap.pt_after
    total_dist_m = gap.gap_dist_m

    if total_dist_m < min_distance_m:
        # Gap is shorter than one output step — no interior points needed.
        return []

    bearing = initial_bearing_rad(pa.lat, pa.lon, pb.lat, pb.lon)

    # Number of interior steps
    n_steps = int(total_dist_m / min_distance_m)
    step_m  = total_dist_m / (n_steps + 1)   # evenly divide so last pt != pb

    # Time and elevation interpolation helpers
    total_dt_s = (pb.time - pa.time).total_seconds()   # type: ignore[operator]
    has_ele = pa.ele is not None and pb.ele is not None

    new_points: list[Point] = []
    for k in range(1, n_steps + 1):
        frac = (k * step_m) / total_dist_m
        lat, lon = destination_point(pa.lat, pa.lon, bearing, k * step_m)
        t = pa.time + timedelta(seconds=frac * total_dt_s)   # type: ignore[operator]
        ele = (pa.ele + frac * (pb.ele - pa.ele)) if has_ele else None   # type: ignore[operator]
        new_points.append(Point(
            lat=lat,
            lon=lon,
            ele=ele,
            time=t,
            source="interpolated",
        ))

    return new_points


def bridge_small_gaps(
    points: list[Point],
    max_bridge_dist_m: float,
    min_distance_m: float,
    verbosity: int,
    stats: Stats,
) -> list[Point]:
    """
    Silently fill any gap between adjacent output points whose great-circle
    distance is less than `max_bridge_dist_m` (default 926 m = 0.5 nm).

    These are GPS logger dropouts: the logger paused briefly and resumed
    at a position that is nearby but not within the normal decimation
    clustering distance.  They leave a visible nick in the rendered track.

    Unlike fix_gaps (which handles large underway gaps interactively), this
    phase runs unconditionally and silently — no prompt, no time-gap
    condition.  A single great-circle-interpolated point is inserted at the
    midpoint for gaps shorter than one output step; for slightly longer gaps
    the normal interpolate_gap spacing applies.

    Inserted points carry source='bridged' to distinguish them from both
    real GPS fixes and manually-approved gap-fills.
    """
    log(VERBOSITY_INFO, verbosity, "info",
        f"🔗  Phase 8d: bridge small gaps (< {max_bridge_dist_m/1852:.2f} nm) …")

    if len(points) < 2:
        log(VERBOSITY_INFO, verbosity, "info", "    Too few points — skipping.")
        return points

    # Build a GapInfo-compatible structure and reuse interpolate_gap.
    # We scan for adjacent pairs where distance is in (min_distance_m, max_bridge_dist_m).
    # Pairs under min_distance_m were already deduplicated; nothing to insert.
    inserts: list[tuple[int, list[Point]]] = []

    for i in range(len(points) - 1):
        pa, pb = points[i], points[i + 1]
        d = haversine_m(pa.lat, pa.lon, pb.lat, pb.lon)
        if d <= min_distance_m or d >= max_bridge_dist_m:
            continue

        # Build a minimal GapInfo for interpolate_gap.
        if pa.time and pb.time:
            gap_time_h = (pb.time - pa.time).total_seconds() / 3600.0
        else:
            gap_time_h = 0.0

        before_kn = _context_speed_kn(points, i,     look_before=True)
        after_kn  = _context_speed_kn(points, i + 1, look_before=False)
        fill_kn   = (before_kn + after_kn) / 2.0 if (before_kn > 0 and after_kn > 0) else max(before_kn, after_kn)
        if fill_kn < 0.1:
            fill_kn = 5.0

        gap = GapInfo(
            idx_before=i, idx_after=i + 1,
            pt_before=pa, pt_after=pb,
            gap_dist_m=d,
            gap_time_h=gap_time_h,
            before_kn=before_kn, after_kn=after_kn, fill_kn=fill_kn,
        )
        new_pts = interpolate_gap(gap, min_distance_m)
        # Tag as bridged
        for p in new_pts:
            p.source = "bridged"

        inserts.append((i, new_pts))
        log(VERBOSITY_DEBUG, verbosity, "debug",
            f"    bridge [{i}→{i+1}]  d={d:.0f} m  inserting {len(new_pts)} pt(s)")
        stats.points_bridge_fill += len(new_pts)

    if not inserts:
        log(VERBOSITY_INFO, verbosity, "info", "    No small gaps found.")
        return points

    # Apply in reverse order to preserve indices
    result: list[Point] = list(points)
    for insert_after, new_pts in sorted(inserts, key=lambda x: x[0], reverse=True):
        for k, pt in enumerate(new_pts):
            result.insert(insert_after + 1 + k, pt)

    log(VERBOSITY_INFO, verbosity, "info",
        f"    Bridged {len(inserts):,} small gap(s), "
        f"inserted {stats.points_bridge_fill:,} point(s).  "
        f"{len(result):,} points total.")
    return result


def fix_gaps(
    points: list[Point],
    split_gap_hours: float,
    min_distance_m: float,
    auto: bool,
    verbosity: int,
    stats: Stats,
) -> list[Point]:
    """
    Detect underway gaps in the output track and offer to fill them with
    interpolated points.

    If `auto` is True, all gaps are filled without prompting.
    Otherwise, each gap is described on the console and the user is asked
    whether to fill it (y/n).  The loop runs until all gaps are resolved.

    Filling is iterative: after a gap is filled the indices of subsequent
    gaps shift, so detection is re-run after each fill.  In practice the
    number of gaps is small (tens at most for a 10-year voyage) so the
    re-scan cost is negligible.
    """
    log(VERBOSITY_INFO, verbosity, "info",
        "🔗  Phase 8c: gap detection and interpolation …")

    iteration = 0
    total_inserted = 0

    while True:
        gaps = detect_gaps(points, split_gap_hours, verbosity)
        if not gaps:
            break

        if iteration == 0:
            stats.gaps_found = len(gaps)
            console.print(
                f"[warn]  Found {len(gaps):,} underway gap(s) in the track.[/warn]"
            )

        any_filled = False
        inserts: list[tuple[int, list[Point]]] = []   # (insert-after-index, new_pts)

        for gap in gaps:
            pa, pb = gap.pt_before, gap.pt_after
            dist_nm = gap.gap_dist_m / 1852.0

            # Reverse-geocode both endpoints (may take a moment on first call).
            from_label = location_label(pa.lat, pa.lon)
            to_label   = location_label(pb.lat, pb.lon)

            console.print(
                f"\n[heading]  Gap:[/heading]  "
                f"{pa.time.strftime('%Y-%m-%d %H:%M') if pa.time else '?'}  →  "   # type: ignore[union-attr]
                f"{pb.time.strftime('%Y-%m-%d %H:%M') if pb.time else '?'}\n"       # type: ignore[union-attr]
                f"         Duration:  {gap.gap_time_h:.1f} h\n"
                f"         Distance:  {dist_nm:.1f} nm\n"
                f"         From:      {from_label}\n"
                f"         To:        {to_label}\n"
                f"         Ctx speed: {gap.before_kn:.1f} kn (before) / "
                f"{gap.after_kn:.1f} kn (after)  →  fill at {gap.fill_kn:.1f} kn\n"
                f"         Est. pts:  "
                f"{max(0, int(gap.gap_dist_m / min_distance_m)):,} "
                f"at {min_distance_m/1000:.0f} km spacing"
            )

            if auto:
                fill = True
            else:
                try:
                    ans = input("  Fill this gap? [y/N] ").strip().lower()
                except EOFError:
                    ans = "n"
                fill = ans in ("y", "yes")

            if fill:
                new_pts = interpolate_gap(gap, min_distance_m)
                inserts.append((gap.idx_before, new_pts))
                n = len(new_pts)
                console.print(
                    f"  [good]✔  Filling — inserting {n:,} point(s).[/good]"
                )
                any_filled = True
                total_inserted += n
                stats.gaps_filled += 1
                stats.points_gap_fill += n
            else:
                console.print("  [dim]–  Skipped.[/dim]")

        if not any_filled:
            break

        # Apply inserts in reverse order so earlier insertions don't shift later indices.
        result: list[Point] = list(points)
        for insert_after, new_pts in sorted(inserts, key=lambda x: x[0], reverse=True):
            for k, pt in enumerate(new_pts):
                result.insert(insert_after + 1 + k, pt)
        points = result
        iteration += 1

    if total_inserted:
        log(VERBOSITY_INFO, verbosity, "info",
            f"    Inserted {total_inserted:,} interpolated point(s) across "
            f"{stats.gaps_filled:,} gap(s).  {len(points):,} points total.")
    elif stats.gaps_found == 0:
        log(VERBOSITY_INFO, verbosity, "info",
            "    No underway gaps found.")
    else:
        log(VERBOSITY_INFO, verbosity, "info",
            f"    {stats.gaps_found:,} gap(s) found — none filled.")

    return points


# ── phase 9: write output ─────────────────────────────────────────────────────
def split_into_segments(
    points: list[Point],
    split_gap_hours: float,
    verbosity: int,
    stats: Stats,
) -> list[list[Point]]:
    """
    Split the flat point list into sub-lists wherever the time gap between
    consecutive points exceeds split_gap_hours.

    This restores the natural voyage-leg structure that the input file had as
    separate track segments.  GPX viewers (including GPX Editor on macOS) draw
    a connecting line between every adjacent point in a segment; without
    splitting, a 15-month gap between two points produces a straight line drawn
    across the entire Pacific, which can trigger O(n²) rendering or spatial-
    index bugs and cause the application to hang.

    Returns a list of segments (each segment is a list[Point]).
    A split_gap_hours of 0 means no splitting — everything stays in one segment.
    """
    if split_gap_hours <= 0 or not points:
        stats.segments_out = 1
        return [points]

    raw_segments: list[list[Point]] = []
    current: list[Point] = [points[0]]

    for pt in points[1:]:
        prev = current[-1]
        if prev.time and pt.time:
            dt_h = (pt.time - prev.time).total_seconds() / 3600.0
            if dt_h > split_gap_hours:
                raw_segments.append(current)
                log(
                    VERBOSITY_DEBUG, verbosity, "debug",
                    f"    segment break: gap {dt_h:.1f} h  "
                    f"({prev.time} -> {pt.time})",
                )
                current = []
        current.append(pt)

    if current:
        raw_segments.append(current)

    # Drop single-point segments — they have zero length and no track to render.
    # They arise when decimation emits exactly one point in a time window (e.g.
    # a brief GPS fix while in port), but a solo point with no neighbours is not
    # a useful track segment and can confuse some GPX applications.
    #
    # Also drop 2-point segments where the total distance is negligible (< 10 m).
    # These arise when two ghost-fix centroids both survive into the same short
    # time window; the segment looks like a 0-knot stop but the positions are
    # effectively identical, producing a zero-length artefact.
    MIN_SEGMENT_DIST_M = 10.0

    def segment_ok(s: list[Point]) -> bool:
        if len(s) < 2:
            return False
        if len(s) == 2:
            d = haversine_m(s[0].lat, s[0].lon, s[1].lat, s[1].lon)
            if d < MIN_SEGMENT_DIST_M:
                log(VERBOSITY_DEBUG, verbosity, "debug",
                    f"    segment drop: 2-pt zero-dist  "
                    f"({s[0].lat:.5f},{s[0].lon:.5f} -> "
                    f"{s[1].lat:.5f},{s[1].lon:.5f}  d={d:.2f} m)")
                return False
        return True

    segments = [s for s in raw_segments if segment_ok(s)]
    n_dropped = len(raw_segments) - len(segments)
    if n_dropped:
        log(
            VERBOSITY_INFO, verbosity, "info",
            f"    Dropped {n_dropped:,} degenerate segment(s) "
            f"(single-point or zero-distance 2-point).",
        )

    stats.segments_out = len(segments)
    log(
        VERBOSITY_INFO, verbosity, "info",
        f"    Split into {len(segments):,} segment(s) "
        f"(gap threshold: {split_gap_hours:.0f} h).",
    )

    # Bridge segment joins so GPX viewers don't draw a connecting line across
    # large gaps.  Copy the last point of each segment as the first point of
    # the next segment: the viewer then draws a zero-length step at the join
    # (same position repeated) rather than a straight line across the ocean.
    # This preserves the segment structure (for GPX semantics / voyage legs)
    # while making the rendered track appear visually continuous.
    if len(segments) > 1:
        bridged: list[list[Point]] = [segments[0]]
        for seg in segments[1:]:
            prev_last = bridged[-1][-1]
            bridged.append([prev_last] + seg)
        segments = bridged
        log(VERBOSITY_DEBUG, verbosity, "debug",
            f"    Duplicated segment endpoints to bridge {len(segments) - 1} join(s).")

    return segments


def write_gpx(
    out_path: Path,
    points: list[Point],
    waypoints: list,
    include_waypoints: bool,
    split_gap_hours: float,
    source_path: Path,
    verbosity: int,
    stats: Stats,
) -> None:
    log(VERBOSITY_INFO, verbosity, "info", f"💾  Writing {out_path} …")

    segments = split_into_segments(points, split_gap_hours, verbosity, stats)

    gpx_out = gpxpy.gpx.GPX()
    gpx_out.creator = "gpx_simplify.py"
    gpx_out.name = f"Simplified track from {source_path.name}"
    gpx_out.description = (
        f"Processed {stats.points_in:,} input points -> {stats.points_out:,} output points "
        f"in {stats.segments_out:,} segment(s). "
        f"Speed filter: dropped {stats.points_speed_drop:,}. "
        f"Longitude-jump filter: dropped {stats.points_lonjump_drop:,}. "
        f"Elevation filter: dropped {stats.points_ele_drop:,}. "
        f"Cross-track filter: dropped {stats.points_crosstrack_drop:,} "
        f"in {stats.crosstrack_passes} pass(es). "
        f"Merge-distance drops: {stats.points_merge_drop:,}. "
        f"Duplicate-position drops: {stats.points_duppos_drop:,}."
    )

    track = gpxpy.gpx.GPXTrack()
    track.name = "Simplified Track"
    gpx_out.tracks.append(track)

    with Progress(
        SpinnerColumn(),
        TextColumn("[info]Building output …"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("writing", total=len(points))
        for seg_points in segments:
            segment = gpxpy.gpx.GPXTrackSegment()
            track.segments.append(segment)
            for pt in seg_points:
                tp = gpxpy.gpx.GPXTrackPoint(
                    latitude=round(pt.lat, 6),
                    longitude=round(pt.lon, 6),
                    elevation=round(pt.ele, 1) if pt.ele is not None else None,
                    time=pt.time,
                )
                segment.points.append(tp)
                progress.advance(task)

    if include_waypoints:
        for wp in waypoints:
            gpx_out.waypoints.append(wp)
        stats.waypoints_out = len(waypoints)
        log(VERBOSITY_DEBUG, verbosity, "debug",
            f"    Copied {stats.waypoints_out} waypoints.")

    xml = gpx_out.to_xml()

    # gpxpy always emits an xsi:schemaLocation pointing to topografix.com.
    # Some XML parsers (including GPX Editor on macOS) attempt to fetch that
    # URL for schema validation at load time.  If the request is slow or the
    # app does it synchronously, the UI hangs.  Strip it from the output.
    import re
    xml = re.sub(
        r'\s+xmlns:xsi="[^"]*"', '', xml, count=1
    )
    xml = re.sub(
        r'\s+xsi:schemaLocation="[^"]*"', '', xml, count=1
    )

    out_path.write_text(xml, encoding="utf-8")

    out_size = out_path.stat().st_size
    log(VERBOSITY_INFO, verbosity, "info",
        f"    Written {out_size / 1_048_576:.2f} MB  "
        f"({stats.segments_out:,} segment(s)) → {out_path}")


# ── summary table ─────────────────────────────────────────────────────────────
def print_summary(stats: Stats, in_path: Path, out_path: Path, dry_run: bool) -> None:
    table = Table(title="✅  GPX Simplification Summary", title_style="heading",
                  show_header=True, header_style="bold cyan")
    table.add_column("Metric", style="stat", no_wrap=True)
    table.add_column("Value", justify="right")

    table.add_row("Input file",      str(in_path))
    table.add_row("Output file",     str(out_path) if not dry_run else "[dim](dry run — not written)[/dim]")
    table.add_row("Tracks in",       f"{stats.tracks_in:,}")
    table.add_row("Segments in",     f"{stats.segments_in:,}")
    table.add_row("Points in",       f"{stats.points_in:,}")
    table.add_row("  ↳ no timestamp",f"{stats.points_no_time:,}")
    table.add_row("Speed anomalies dropped",
                  f"[warn]{stats.points_speed_drop:,}[/warn]")
    table.add_row("Longitude-jump anomalies dropped",
                  f"[warn]{stats.points_lonjump_drop:,}[/warn]")
    table.add_row("Elevation spikes dropped",
                  f"[warn]{stats.points_ele_drop:,}[/warn]")
    table.add_row("Cross-track anomalies dropped",
                  f"[warn]{stats.points_crosstrack_drop:,}[/warn] "
                  f"[dim](in {stats.crosstrack_passes} pass(es))[/dim]")
    table.add_row("Merge drops",       f"{stats.points_merge_drop:,}")
    table.add_row("Duplicate-time drops", f"{stats.points_duptime_drop:,}")
    table.add_row("Duplicate-position drops", f"{stats.points_duppos_drop:,}")
    table.add_row("Zero-speed ghost drops",
                  f"[warn]{stats.points_zerospd_drop:,}[/warn]")
    if stats.points_bridge_fill:
        table.add_row("Small-gap bridge points", f"{stats.points_bridge_fill:,}")
    if stats.gaps_found:
        table.add_row("Underway gaps found",  f"[warn]{stats.gaps_found:,}[/warn]")
        table.add_row("Gaps filled",          f"[good]{stats.gaps_filled:,}[/good]")
        table.add_row("Gap-fill points added",f"[good]{stats.points_gap_fill:,}[/good]")
    table.add_row("Segments out",      f"[good]{stats.segments_out:,}[/good]")
    table.add_row("Points out",       f"[good]{stats.points_out:,}[/good]")
    if stats.points_in:
        pct = (1 - stats.points_out / stats.points_in) * 100
        table.add_row("Reduction",   f"{pct:.1f}%")
    table.add_row("Approx distance", f"{stats.total_dist_km:,.0f} km")
    table.add_row("Waypoints in/out",
                  f"{stats.waypoints_in} / {stats.waypoints_out}")
    if stats.bbox[0] < 90:
        table.add_row(
            "Bounding box",
            f"{stats.bbox[0]:.4f}°, {stats.bbox[1]:.4f}°  ->  "
            f"{stats.bbox[2]:.4f}°, {stats.bbox[3]:.4f}°",
        )

    console.print()
    console.print(table)


# ── argument parsing ──────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gpx_simplify",
        description=(
            "Simplify a large GPX file into a single clean track.\n"
            "Merges all tracks/segments chronologically, removes speed and\n"
            "cross-track anomalies, and decimates to a target point spacing."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic run with defaults (100 m spacing, 50 kn speed limit, 3 sanity passes):
  python gpx_simplify.py -i voyage.gpx

  # 200 m spacing, 40-knot cap, 5 sanity passes, verbose:
  python gpx_simplify.py -i voyage.gpx -o out.gpx -d 200 -s 40 --passes 5 -vv

  # Dry run to see stats without writing:
  python gpx_simplify.py -i voyage.gpx --dry-run -v

  # Tighter cross-track tolerance (500 m), extra verbose:
  python gpx_simplify.py -i voyage.gpx --max-crosstrack 500 -vvv

  # Drop waypoints:
  python gpx_simplify.py -i voyage.gpx --no-waypoints
""",
    )

    p.add_argument(
        "-i", "--input", required=True, metavar="FILE",
        help="Input GPX file path (required).",
    )
    p.add_argument(
        "-o", "--output", default=None, metavar="FILE",
        help="Output GPX file path. Default: <input>_simplified.gpx",
    )
    p.add_argument(
        "-d", "--min-distance", type=float, default=1000.0, metavar="METRES",
        help=(
            "Minimum distance between output track points in metres. Default: 1000. "
            "For long ocean voyages (thousands of km), 1000m gives a clean track "
            "that any viewer can handle. Use 100-500m for shorter detailed passages."
        ),
    )
    p.add_argument(
        "-m", "--merge-distance", type=float, default=100.0, metavar="METRES",
        help=(
            "Points closer than this distance (m) to the last emitted point "
            "are merged/skipped. Default: 100"
        ),
    )
    p.add_argument(
        "-s", "--max-speed", type=float, default=50.0, metavar="KNOTS",
        help=(
            "Maximum plausible speed in knots. Points implying a higher speed "
            "are treated as GPS errors and dropped. Default: 50"
        ),
    )
    p.add_argument(
        "--max-ele-change", type=float, default=50.0, metavar="METRES",
        help=(
            "Maximum elevation change in metres between adjacent points. "
            "Points whose elevation differs from the previous kept point by more "
            "than this value are treated as sensor errors and dropped. "
            "Points without elevation data are unaffected. Default: 50"
        ),
    )
    p.add_argument(
        "--max-lon-jump", type=float, default=90.0, metavar="DEGREES",
        help=(
            "Maximum longitude difference in degrees between a point and BOTH its "
            "neighbours before the point is treated as a GPS hemisphere jump. "
            "Catches antimeridian glitches that the speed filter misses. Default: 90"
        ),
    )
    p.add_argument(
        "--max-crosstrack", type=float, default=1000.0, metavar="METRES",
        help=(
            "Maximum perpendicular deviation (m) from the line between a point's "
            "neighbours before it is considered a geometric outlier. "
            "The self-crossing guard (--max-crosstrack-rate) prevents legitimate "
            "track crossings from being dropped. Default: 1000"
        ),
    )
    p.add_argument(
        "--max-crosstrack-rate", type=float, default=93000.0, metavar="M_PER_HOUR",
        help=(
            "Cross-track outlier rate threshold in metres per hour. "
            "If the deviation divided by the time gap between neighbours is below "
            "this value, the point is kept (the track has legitimately crossed itself). "
            "Default: 93000 (≈ 50 knots — same as default speed cap)."
        ),
    )
    p.add_argument(
        "--passes", type=int, default=3, metavar="N",
        help=(
            "Maximum number of cross-track filter passes to run. "
            "The filter stops early if a pass drops nothing. Default: 3"
        ),
    )
    p.add_argument(
        "--split-gap", type=float, default=24.0, metavar="HOURS",
        help=(
            "Split the output into separate track segments wherever the time gap "
            "between consecutive points exceeds this many hours. Default: 24. "
            "This prevents GPX viewers from drawing a straight connecting line "
            "across multi-day or multi-month gaps (e.g. when the boat was in port). "
            "Use 0 to disable splitting and write a single continuous segment."
        ),
    )
    p.add_argument(
        "--fix-gaps", action="store_true", default=False,
        help=(
            "After processing, detect underway gaps in the output track — "
            "time breaks longer than --split-gap hours where the vessel has "
            "also moved — and offer to fill each one with interpolated points "
            "spaced at --min-distance.  Interpolation speed is the mean of the "
            "average speeds of the 10 output points immediately before and after "
            "the gap.  Without --fix-gaps-auto the tool prompts for each gap."
        ),
    )
    p.add_argument(
        "--fix-gaps-auto", action="store_true", default=False,
        help=(
            "Fill all detected underway gaps automatically without prompting. "
            "Implies --fix-gaps."
        ),
    )
    p.add_argument(
        "--zerospd-window", type=int, default=10, metavar="N",
        help=(
            "Number of neighbouring output points to examine on each side when "
            "evaluating a zero-speed (zero-distance) step. Default: 10."
        ),
    )
    p.add_argument(
        "--zerospd-max-dist", type=float, default=50.0, metavar="KM",
        help=(
            "Maximum distance in km from all local neighbours for a zero-speed "
            "point to be considered a ghost fix and dropped. If the point is "
            "within this distance of at least one neighbour it is treated as a "
            "genuine stationary position (boat at anchor, etc.). Default: 50."
        ),
    )
    wp_group = p.add_mutually_exclusive_group()
    wp_group.add_argument(
        "--waypoints", dest="waypoints", action="store_true", default=True,
        help="Copy waypoints from input to output (default).",
    )
    wp_group.add_argument(
        "--no-waypoints", dest="waypoints", action="store_false",
        help="Do not copy waypoints to output.",
    )
    p.add_argument(
        "-v", "--verbose", action="count", default=0,
        help=(
            "Increase verbosity. "
            "-v = info, -vv = debug (per-segment), -vvv = trace (every point)."
        ),
    )
    p.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Parse and process but do not write the output file.",
    )
    return p


# ── main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    verbosity: int = min(args.verbose, VERBOSITY_TRACE)

    in_path  = Path(args.input).expanduser().resolve()
    out_path = (
        Path(args.output).expanduser().resolve()
        if args.output
        else in_path.with_name(in_path.stem + "_simplified.gpx")
    )

    # ── banner ────────────────────────────────────────────────────────────────
    console.print(Panel(
        Text.from_markup(
            "[heading]gpx_simplify[/heading]  —  sailing track optimizer\n\n"
            f"[info]Input :[/info]              {in_path}\n"
            f"[info]Output:[/info]              {out_path}"
            f"{'  [warn](dry run)[/warn]' if args.dry_run else ''}\n"
            f"[info]Min distance:[/info]        {args.min_distance:.0f} m\n"
            f"[info]Merge distance:[/info]      {args.merge_distance:.0f} m\n"
            f"[info]Max speed:[/info]           {args.max_speed:.0f} kn\n"
            f"[info]Max lon jump:[/info]        {args.max_lon_jump:.0f}°\n"
            f"[info]Max ele change:[/info]      {args.max_ele_change:.0f} m\n"
            f"[info]Max cross-track:[/info]     {args.max_crosstrack:.0f} m\n"
            f"[info]Cross-track rate:[/info]    {args.max_crosstrack_rate:.0f} m/h\n"
            f"[info]Sanity passes:[/info]       {args.passes}\n"
            f"[info]Split gap:[/info]           "
            f"{args.split_gap:.0f} h{'  [dim](disabled)[/dim]' if args.split_gap <= 0 else ''}\n"
            f"[info]Waypoints:[/info]           {'yes' if args.waypoints else 'no'}\n"
            f"[info]Verbosity:[/info]           {verbosity} "
            f"({'quiet' if verbosity == 0 else 'info' if verbosity == 1 else 'debug' if verbosity == 2 else 'trace'})",
        ),
        title="⛵  GPX Simplify",
        border_style="cyan",
    ))
    console.print()

    # ── validate input ────────────────────────────────────────────────────────
    if not in_path.exists():
        console.print(f"[error]ERROR:[/error] Input file not found: {in_path}")
        return 1

    if out_path == in_path and not args.dry_run:
        console.print("[error]ERROR:[/error] Output path is the same as input — "
                      "use -o to specify a different file.")
        return 1

    if args.passes < 1:
        console.print("[error]ERROR:[/error] --passes must be at least 1.")
        return 1

    stats = Stats()

    # ── pipeline ──────────────────────────────────────────────────────────────
    points, waypoints = parse_gpx(in_path, verbosity, stats)

    if not points:
        console.print("[error]ERROR:[/error] No track points found in input file.")
        return 1

    points = sort_points(points, verbosity)
    points = filter_speed_anomalies(points, args.max_speed, verbosity, stats)
    points = filter_crosstrack_anomalies(
        points,
        max_crosstrack_m=args.max_crosstrack,
        max_crosstrack_rate_m_per_h=args.max_crosstrack_rate,
        max_passes=args.passes,
        verbosity=verbosity,
        stats=stats,
    )
    points = decimate_points(
        points,
        min_distance_m=args.min_distance,
        merge_distance_m=args.merge_distance,
        verbosity=verbosity,
        stats=stats,
    )
    points = filter_elevation_anomalies(points, args.max_ele_change, verbosity, stats)
    points = filter_output_speed(points, args.max_speed, verbosity, stats)
    points = filter_longitude_jumps(points, args.max_lon_jump, verbosity, stats)
    points = deduplicate_timestamps(points, verbosity, stats)
    points = deduplicate_positions(points, verbosity, stats)
    points = filter_zero_speed_ghosts(
        points,
        neighbour_window=args.zerospd_window,
        max_neighbour_dist_m=args.zerospd_max_dist * 1000.0,
        verbosity=verbosity,
        stats=stats,
    )

    points = bridge_small_gaps(
        points,
        max_bridge_dist_m=0.5 * 1852.0,   # 0.5 nm in metres
        min_distance_m=args.min_distance,
        verbosity=verbosity,
        stats=stats,
    )

    if args.fix_gaps or args.fix_gaps_auto:
        points = fix_gaps(
            points,
            split_gap_hours=args.split_gap,
            min_distance_m=args.min_distance,
            auto=args.fix_gaps_auto,
            verbosity=verbosity,
            stats=stats,
        )

    if not args.dry_run:
        write_gpx(out_path, points, waypoints, args.waypoints,
                  args.split_gap, in_path, verbosity, stats)
    else:
        log(VERBOSITY_INFO, verbosity, "warn",
            "⚠  Dry run — output file not written.")

    print_summary(stats, in_path, out_path, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
