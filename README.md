# gpx_simplify

A Python tool for simplifying large GPX files from long-distance sailing voyages.

Merges all tracks and segments from multiple GPS sources into a single, clean, chronologically-sorted track — filtering out GPS anomalies (speed, elevation, and cross-track) and decimating to a target point spacing.

Built for a real-world use case: ten years of sailing across the Pacific, around Australia, and back to North America, captured across two overlapping GPS sources in a single ~100 MB GPX file.

## Features

- **Multi-source merge** — combines all tracks and segments from any number of sources, sorted chronologically into one unified track
- **Speed anomaly filter** — drops points that imply physically impossible speeds (e.g. a sailboat "teleporting" across the ocean between two fixes)
- **Antimeridian-safe centroid averaging** — cluster centroids near the date line (±180°) are computed using circular mean, preventing GPS fixes on opposite sides of the antimeridian from averaging to a point in the wrong hemisphere
- **Longitude-jump filter** — catches GPS hemisphere-jumps (e.g. a fix at lon=-60° while crossing the date line near lon=±179°) that the speed filter may miss due to antimeridian wrapping; runs after decimation
- **Elevation spike filter** — drops points whose altitude differs from the previous point by more than a configurable threshold (default 50 m); GPS elevation noise on a boat should never produce sudden multi-metre jumps
- **Distance decimation** — reduces point density to a target spacing in metres, averaging clusters of nearby points into a centroid rather than simply discarding them
- **Duplicate-position deduplication** — removes adjacent output points that map to the same rounded coordinate (prevents zero-distance steps in GPX viewers)
- **Segment splitting** — splits the output into separate track segments wherever the time gap between consecutive points exceeds a configurable threshold (default 24 h); prevents GPX viewers from drawing straight lines across multi-day gaps between passages
- **Waypoint passthrough** — optionally copies all named waypoints from the input to the output
- **Rich terminal UI** — colour output, animated progress bars, and a summary table; multiple verbosity levels for debugging
- **Dry-run mode** — analyse without writing any output

## Requirements

```
pip install gpxpy rich
```

Python 3.9 or later.

## Usage

```bash
# Basic run — 100 m point spacing, 50-knot speed cap, keep waypoints:
python gpx_simplify.py -i voyage.gpx

# Specify output file:
python gpx_simplify.py -i voyage.gpx -o simplified.gpx

# Dry run to check statistics before writing:
python gpx_simplify.py -i voyage.gpx --dry-run -v

# Custom spacing and speed threshold:
python gpx_simplify.py -i voyage.gpx -d 200 -s 40 -vv

# Wider elevation spike tolerance (100 m instead of default 50 m):
python gpx_simplify.py -i voyage.gpx --max-ele-change 100

# Drop waypoints, maximum debug output:
python gpx_simplify.py -i voyage.gpx --no-waypoints -vvv
```

## Options

| Flag | Default | Description |
|---|---|---|
| `-i / --input FILE` | *(required)* | Input GPX file |
| `-o / --output FILE` | `<input>_simplified.gpx` | Output GPX file |
| `-d / --min-distance METRES` | `1000` | Minimum distance between output points |
| `-m / --merge-distance METRES` | `100` | Points closer than this to the last emitted point are merged |
| `-s / --max-speed KNOTS` | `50` | Speed above which a point is treated as a GPS error |
| `--max-lon-jump DEGREES` | `90` | Max longitude difference (°) between a point and both its neighbours; catches antimeridian GPS glitches |
| `--max-ele-change METRES` | `50` | Max elevation change between adjacent points; larger jumps are dropped as sensor noise |
| `--max-crosstrack METRES` | `1000` | Max perpendicular deviation from the prev→next line before a point is an outlier |
| `--max-crosstrack-rate M_PER_HOUR` | `93000` | Rate guard for the cross-track filter — keeps legitimate self-crossing tracks |
| `--passes N` | `3` | Max cross-track filter passes (stops early when nothing is dropped) |
| `--split-gap HOURS` | `24` | Split output into separate segments at time gaps longer than this; use 0 to disable |
| `--waypoints / --no-waypoints` | waypoints on | Copy waypoints to output |
| `-v / -vv / -vvv` | quiet | Verbosity: info / debug / trace |
| `--dry-run` | off | Parse and filter but do not write output |

## How it works

**1. Parse** — reads the input GPX with [gpxpy](https://github.com/tkrajina/gpxpy), collecting every track point with its timestamp and source label.

**2. Sort** — merges all points from all tracks and segments into a single chronological list (points without timestamps are appended at the end).

**3. Speed filter** — walks the sorted list and drops any point where *both* the incoming and outgoing legs exceed the speed threshold. The two-sided check avoids falsely dropping a valid point that happens to follow or precede a tight cluster.

**4. Elevation filter** — drops any point whose altitude differs from the previous kept point by more than `--max-ele-change` (default 50 m). GPS altitude on a boat should be close to sea level and stable; sudden jumps of tens of metres are always sensor noise. Points without elevation data are passed through unchanged.

**5. Cross-track filter (iterative)** — for each interior point, computes its perpendicular distance from the great-circle line between its two neighbours. If the deviation exceeds `--max-crosstrack` *and* the deviation-per-hour exceeds `--max-crosstrack-rate`, the point is dropped as a GPS jump. The rate guard uses the *minimum* of the two leg gaps so a spike 2 minutes from one neighbour is caught even if the other is hours away. Runs up to `--passes` times, stopping early when a pass drops nothing.

**6. Decimate** — walks the filtered list, accumulating nearby points into clusters. When the accumulated distance from the last emitted point reaches `--min-distance`, the centroid of the current cluster is emitted as one output point. Longitude is averaged using the circular mean (unit-vector method) so clusters near the antimeridian (±180°) are correctly averaged across the date line rather than collapsing to the wrong hemisphere.

**7. Elevation filter** — scrubs implausible elevation values from the decimated list, nulling out the altitude for any point whose elevation differs from the previous clean value by more than `--max-ele-change`. The position is kept; only the altitude tag is removed.

**8. Longitude-jump filter** — walks the decimated output and drops any point whose longitude differs from *both* its neighbours by more than `--max-lon-jump` degrees. Running after decimation catches rare antimeridian glitches that survive earlier filters because haversine wraps distances across ±180°.

**9. Deduplicate timestamps** — removes any output point whose timestamp is identical to the immediately preceding point. This can occur when two source tracks both have a fix at the same clock second but at different positions; both survive the distance filter but would appear as adjacent points with a zero time delta, causing divide-by-zero errors in speed/heading calculations in downstream tools.

**10. Deduplicate positions** — removes any output point whose latitude/longitude (rounded to 6 decimal places) is identical to the immediately preceding point. After rounding, adjacent fixes near the antimeridian can map to the same coordinate, producing zero-distance steps that cause errors in GPX viewers.

**11. Segment splitting** — before writing, splits the flat point list into separate track segments wherever the time gap between consecutive points exceeds `--split-gap` hours (default 24). GPX viewers draw a connecting line between every adjacent point in a segment; without splitting, a 15-month gap between passages produces a straight line across the Pacific that can cause O(n²) rendering or spatial-index hangs in some applications (including GPX Editor on macOS).

**12. Write** — writes a clean GPX 1.1 file with one track containing multiple segments (one per passage leg) and optional waypoints. The `xsi:schemaLocation` attribute that gpxpy normally includes is stripped from the output — some applications attempt to fetch the referenced XSD from the network on load, which causes a hang if the request is slow or firewalled. Output coordinates are rounded to 6 decimal places (~11 cm precision).

## Example output

```
╭──────────────────────────── ⛵  GPX Simplify ────────────────────────────╮
│ Input :          voyage.gpx                                               │
│ Output:          voyage_simplified.gpx                                    │
│ Min distance:    100 m                                                    │
│ Merge distance:  100 m                                                    │
│ Max speed:       50 kn                                                    │
│ Waypoints:       yes                                                      │
│ Verbosity:       1 (info)                                                 │
╰───────────────────────────────────────────────────────────────────────────╯

                   ✅  GPX Simplification Summary
┏━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━┓
┃ Metric                  ┃                Value ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━┩
│ Points in               │            1,432,887 │
│ Speed anomalies dropped │                  341 │
│ Merge drops             │            1,180,432 │
│ Points out              │               52,114 │
│ Reduction               │                96.4% │
│ Approx distance         │           48,320 km  │
└─────────────────────────┴──────────────────────┘
```

## License

MIT
