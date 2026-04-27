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
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    points_ele_drop:        int = 0
    points_crosstrack_drop: int = 0
    points_merge_drop:      int = 0
    points_duptime_drop:    int = 0
    points_out:             int = 0
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

                    # Normalise to UTC-aware datetime
                    t = pt.time
                    if t is not None and t.tzinfo is None:
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
            f"    ⚠  {stats.points_no_time:,} points have no timestamp "
            "(they will be kept but cannot be speed- or cross-track-checked).",
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
    Remove points that imply an impossible speed.

    A point is dropped if BOTH of the following are true:
      • the speed from the previous *kept* point to this point exceeds max_speed_knots
      • the speed from this point to the next point also exceeds max_speed_knots
    (This avoids dropping a valid point when two consecutive GPS fixes are very
    close in time but the boat just happened to be moving fast.)
    """
    log(VERBOSITY_INFO, verbosity, "info",
        f"🚀  Filtering speed anomalies > {max_speed_knots:.0f} kn …")

    if not points:
        return points

    kept: list[Point] = [points[0]]

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
            cur  = points[i]
            prev = kept[-1]
            nxt  = points[i + 1] if i + 1 < len(points) else None

            s_from_prev = speed_knots(prev, cur)
            s_to_next   = speed_knots(cur, nxt) if nxt else None

            # If we can't compute speed (missing timestamps), keep the point
            if s_from_prev is None:
                log(VERBOSITY_TRACE, verbosity, "trace",
                    f"    [{cur.source}] {cur.lat:.5f},{cur.lon:.5f} — no timestamp, kept")
                kept.append(cur)
                progress.advance(task)
                continue

            over_limit_from = s_from_prev > max_speed_knots
            over_limit_to   = (s_to_next is not None) and (s_to_next > max_speed_knots)

            if over_limit_from and (over_limit_to or nxt is None):
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

            progress.advance(task)

    log(VERBOSITY_INFO, verbosity, "info",
        f"    Dropped {stats.points_speed_drop:,} anomalous points.  "
        f"{len(kept):,} remain.")
    return kept


# ── phase 4: elevation-spike filter ──────────────────────────────────────────
def filter_elevation_anomalies(
    points: list[Point],
    max_ele_change_m: float,
    verbosity: int,
    stats: Stats,
) -> list[Point]:
    """
    Drop any point whose elevation differs from the previous *kept* point by
    more than max_ele_change_m.

    Rationale: a sailing GPS should never record a sudden altitude jump of
    tens or hundreds of metres between adjacent fixes. Such values are always
    sensor noise or a bad read. Points without elevation data are passed
    through unchanged (elevation is optional in GPX).

    The check is one-directional against the last *kept* point so that a run
    of bad elevation values doesn't cascade — each candidate is always judged
    against the most recent clean value.
    """
    log(VERBOSITY_INFO, verbosity, "info",
        f"⛰️   Filtering elevation spikes > ±{max_ele_change_m:.0f} m …")

    if not points:
        return points

    kept: list[Point] = []
    last_ele: Optional[float] = None   # elevation of last kept point that had one

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
                # No elevation data on this point or no reference yet — keep it
                # and update the reference if we just got our first elevation
                kept.append(pt)
                if pt.ele is not None:
                    last_ele = pt.ele
                log(VERBOSITY_TRACE, verbosity, "trace",
                    f"    ele-keep (no data)  {pt.lat:.5f},{pt.lon:.5f}")
                progress.advance(task)
                continue

            delta = abs(pt.ele - last_ele)

            if delta > max_ele_change_m:
                stats.points_ele_drop += 1
                log(
                    VERBOSITY_DEBUG, verbosity, "warn",
                    f"    ele-DROP  [{pt.source}] {pt.lat:.5f},{pt.lon:.5f}  "
                    f"ele={pt.ele:.1f} m  prev={last_ele:.1f} m  Δ={delta:.1f} m",
                )
            else:
                kept.append(pt)
                last_ele = pt.ele
                log(VERBOSITY_TRACE, verbosity, "trace",
                    f"    ele-keep  {pt.lat:.5f},{pt.lon:.5f}  "
                    f"ele={pt.ele:.1f} m  Δ={delta:.1f} m")

            progress.advance(task)

    log(VERBOSITY_INFO, verbosity, "info",
        f"    Dropped {stats.points_ele_drop:,} elevation-spike points.  "
        f"{len(kept):,} remain.")
    return kept


# ── phase 5: cross-track sanity filter (iterative) ───────────────────────────
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


# ── phase 6: distance decimation ──────────────────────────────────────────────
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

    def flush_cluster() -> Optional[Point]:
        """Emit the centroid of the current cluster as one output point."""
        if not cluster_lats:
            return None
        avg_lat = sum(cluster_lats) / len(cluster_lats)
        avg_lon = sum(cluster_lons) / len(cluster_lons)
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


# ── phase 8: write output ─────────────────────────────────────────────────────
def write_gpx(
    out_path: Path,
    points: list[Point],
    waypoints: list,
    include_waypoints: bool,
    source_path: Path,
    verbosity: int,
    stats: Stats,
) -> None:
    log(VERBOSITY_INFO, verbosity, "info", f"💾  Writing {out_path} …")

    gpx_out = gpxpy.gpx.GPX()
    gpx_out.creator = "gpx_simplify.py"
    gpx_out.name = f"Simplified track from {source_path.name}"
    gpx_out.description = (
        f"Processed {stats.points_in:,} input points → {stats.points_out:,} output points. "
        f"Speed filter: dropped {stats.points_speed_drop:,}. "
        f"Elevation filter: dropped {stats.points_ele_drop:,}. "
        f"Cross-track filter: dropped {stats.points_crosstrack_drop:,} "
        f"in {stats.crosstrack_passes} pass(es). "
        f"Merge-distance drops: {stats.points_merge_drop:,}."
    )

    track = gpxpy.gpx.GPXTrack()
    track.name = "Simplified Track"
    gpx_out.tracks.append(track)

    segment = gpxpy.gpx.GPXTrackSegment()
    track.segments.append(segment)

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
        for pt in points:
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
        f"    Written {out_size / 1_048_576:.2f} MB → {out_path}")


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
    table.add_row("Elevation spikes dropped",
                  f"[warn]{stats.points_ele_drop:,}[/warn]")
    table.add_row("Cross-track anomalies dropped",
                  f"[warn]{stats.points_crosstrack_drop:,}[/warn] "
                  f"[dim](in {stats.crosstrack_passes} pass(es))[/dim]")
    table.add_row("Merge drops",       f"{stats.points_merge_drop:,}")
    table.add_row("Duplicate-time drops", f"{stats.points_duptime_drop:,}")
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
            f"{stats.bbox[0]:.4f}°, {stats.bbox[1]:.4f}°  →  "
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
        "-d", "--min-distance", type=float, default=100.0, metavar="METRES",
        help="Minimum distance between output track points in metres. Default: 100",
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
            f"[info]Max ele change:[/info]      {args.max_ele_change:.0f} m\n"
            f"[info]Max cross-track:[/info]     {args.max_crosstrack:.0f} m\n"
            f"[info]Cross-track rate:[/info]    {args.max_crosstrack_rate:.0f} m/h\n"
            f"[info]Sanity passes:[/info]       {args.passes}\n"
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
    points = filter_elevation_anomalies(points, args.max_ele_change, verbosity, stats)
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
    points = deduplicate_timestamps(points, verbosity, stats)

    if not args.dry_run:
        write_gpx(out_path, points, waypoints, args.waypoints, in_path, verbosity, stats)
    else:
        log(VERBOSITY_INFO, verbosity, "warn",
            "⚠  Dry run — output file not written.")

    print_summary(stats, in_path, out_path, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
