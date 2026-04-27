# gpx_simplify

A Python tool for simplifying large GPX files from long-distance sailing voyages.

Merges all tracks and segments from multiple GPS sources into a single, clean, chronologically-sorted track — filtering out GPS anomalies (speed, elevation, and cross-track) and decimating to a target point spacing.

Built for a real-world use case: ten years of sailing across the Pacific, around Australia, and back to North America, captured across two overlapping GPS sources in a single ~100 MB GPX file.

## Features

- **Multi-source merge** — combines all tracks and segments from any number of sources, sorted chronologically into one unified track
- **No-timestamp drop** — track points with no timestamp are discarded at parse time; they cannot be chronologically sorted or speed-checked and would corrupt the merged timeline if kept
- **Speed anomaly filter** — drops points that imply physically impossible speeds or positions. For points with normal timestamps, uses a combined one-sided impossibility check and two-sided speed check. For points sharing an identical timestamp with their predecessor (a common artefact when two GPS loggers record the same instant), uses a spatial distance check against the raw predecessor: if two simultaneous points are more than one second's worth of travel apart, the second is a *ghost fix* from another source and is dropped. A second speed pass runs after decimation to catch impossible-speed steps created by centroid averaging across interleaved GPS sources.
- **Zero-speed ghost filter** — after decimation, scans for adjacent output point pairs that are within 1 m of each other (zero apparent speed). For each such pair, examines the surrounding context points, excluding the pair itself. If the pair's position is more than a configurable distance (default 50 km) from every external neighbour, the pair is a geographically isolated ghost island — both points are dropped. Genuine stationary positions (boat at anchor, in port) are unaffected because their neighbours are within a few kilometres.
- **Antimeridian-safe centroid averaging** — cluster centroids near the date line (±180°) are computed using circular mean, preventing GPS fixes on opposite sides of the antimeridian from averaging to a point in the wrong hemisphere
- **Longitude-jump filter** — catches GPS hemisphere-jumps (e.g. a fix at lon=-60° while crossing the date line near lon=±179°) that the speed filter may miss due to antimeridian wrapping; runs after decimation
- **Elevation spike filter** — drops points whose altitude differs from the previous point by more than a configurable threshold (default 50 m); GPS elevation noise on a boat should never produce sudden multi-metre jumps
- **Distance decimation** — reduces point density to a target spacing in metres, averaging clusters of nearby points into a centroid rather than simply discarding them
- **Duplicate-position deduplication** — removes adjacent output points that map to the same rounded coordinate (prevents zero-distance steps in GPX viewers)
- **Segment splitting** — splits the output into separate track segments wherever the time gap between consecutive points exceeds a configurable threshold (default 24 h); prevents GPX viewers from drawing straight lines across multi-day gaps between passages
- **Small-gap bridging** — automatically fills any gap between adjacent output points smaller than 0.5 nm (926 m) with great-circle-interpolated points at `--min-distance` spacing. These are GPS logger dropouts — brief pauses that leave a visible nick in the rendered track. Runs silently during processing with no user interaction required. Bridged points are tagged `source="bridged"`.
- **Gap detection and filling** — optionally scans the output for *underway gaps*: time breaks longer than `--split-gap` hours where the vessel has also moved (distinguishing passage gaps from in-port layups). For each gap, prints the start/end time, distance, estimated fill speed, and reverse-geocoded landmark names for both endpoints, then prompts whether to fill it with great-circle-interpolated points spaced at `--min-distance`. Use `--fix-gaps-auto` to fill all gaps without prompting.
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

# Find and interactively fill underway gaps (prompts for each gap):
python gpx_simplify.py -i voyage.gpx --fix-gaps

# Automatically fill all underway gaps without prompting:
python gpx_simplify.py -i voyage.gpx --fix-gaps-auto
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
| `--zerospd-window N` | `10` | Number of context points to examine on each side when evaluating a zero-speed step |
| `--zerospd-max-dist KM` | `50` | Distance threshold (km) beyond which a zero-speed pair is treated as a ghost island and dropped |
| `--fix-gaps` | off | Detect underway gaps and prompt to fill each one interactively |
| `--fix-gaps-auto` | off | Detect and fill all underway gaps automatically without prompting (implies `--fix-gaps`) |
| `--waypoints / --no-waypoints` | waypoints on | Copy waypoints to output |
| `-v / -vv / -vvv` | quiet | Verbosity: info / debug / trace |
| `--dry-run` | off | Parse and filter but do not write output |

## How it works

**1. Parse** — reads the input GPX with [gpxpy](https://github.com/tkrajina/gpxpy), collecting every track point with its timestamp and source label. Points with no timestamp are discarded immediately: they cannot be sorted chronologically or speed-checked, and keeping them would corrupt the merged timeline.

**2. Sort** — merges all timestamped points from all tracks and segments into a single chronological list.

**3. Speed filter** — walks the sorted list and drops GPS anomalies in two ways:

- *Normal case (dt > 0):* a point is dropped if *both* the incoming and outgoing legs exceed the speed threshold, or if the distance from the previous kept point is physically impossible regardless of elapsed time. The two-sided check avoids falsely dropping a valid point that happens to follow or precede a tight cluster.
- *Zero-dt case (same timestamp):* when two source points share an identical timestamp, the speed formula is undefined. Instead, the filter compares the point's position against its raw predecessor in the sorted array. If they share a timestamp but are more than one second's worth of travel apart (~26 m at 50 kn), the point is a *ghost fix* — a second GPS recording the same instant but placing the vessel somewhere else. These clusters of same-timestamp phantom positions (seen in real multi-source sailing data when redundant loggers store duplicate timestamps) are dropped unconditionally. True position duplicates (same timestamp, same position, a few metres apart) are kept.

**4. Elevation filter** — drops any point whose altitude differs from the previous kept point by more than `--max-ele-change` (default 50 m). GPS altitude on a boat should be close to sea level and stable; sudden jumps of tens of metres are always sensor noise. Points without elevation data are passed through unchanged.

**5. Cross-track filter (iterative)** — for each interior point, computes its perpendicular distance from the great-circle line between its two neighbours. If the deviation exceeds `--max-crosstrack` *and* the deviation-per-hour exceeds `--max-crosstrack-rate`, the point is dropped as a GPS jump. The rate guard uses the *minimum* of the two leg gaps so a spike 2 minutes from one neighbour is caught even if the other is hours away. Runs up to `--passes` times, stopping early when a pass drops nothing.

**6. Decimate** — walks the filtered list, accumulating nearby points into clusters. Longitude is averaged using the circular mean (see above). When the accumulated distance from the last emitted point reaches `--min-distance`, the centroid of the current cluster is emitted as one output point. Longitude is averaged using the circular mean (unit-vector method) so clusters near the antimeridian (±180°) are correctly averaged across the date line rather than collapsing to the wrong hemisphere.

**7. Elevation filter** — scrubs implausible elevation values from the decimated list, nulling out the altitude for any point whose elevation differs from the previous clean value by more than `--max-ele-change`. The position is kept; only the altitude tag is removed.

**7b. Post-decimation speed filter** — re-applies the impossible-distance check to the decimated output. Two interleaved GPS sources can each be internally consistent at low speed, yet produce adjacent output centroids with a physically impossible apparent speed (e.g. 494 m in 1 second = 961 kn) because their cluster median timestamps happen to fall 1–14 seconds apart. The pre-decimation filter cannot catch this; this second pass does.

**8. Longitude-jump filter** — walks the decimated output and drops any point whose longitude differs from *both* its neighbours by more than `--max-lon-jump` degrees. Running after decimation catches rare antimeridian glitches that survive earlier filters because haversine wraps distances across ±180°.

**9. Deduplicate timestamps** — removes any output point whose timestamp is identical to the immediately preceding point. This can occur when two source tracks both have a fix at the same clock second but at different positions; both survive the distance filter but would appear as adjacent points with a zero time delta, causing divide-by-zero errors in speed/heading calculations in downstream tools.

**10. Deduplicate positions** — removes any output point whose latitude/longitude (rounded to 6 decimal places) is identical to the immediately preceding point. After rounding, adjacent fixes near the antimeridian can map to the same coordinate, producing zero-distance steps that cause errors in GPX viewers.

**10b. Zero-speed ghost filter** — scans the deduplicated output for adjacent point pairs within 1 m of each other (zero apparent speed). For each such pair, it examines the `--zerospd-window` context points on each side, *excluding the pair itself*. If the pair's position is more than `--zerospd-max-dist` km (default 50) from every external neighbour, the pair is a geographically isolated ghost island — the signature of a ghost track segment that survived all earlier filters — and both points are dropped. Genuine stationary positions (boat at anchor) have local neighbours within a few kilometres and are not affected. Excluding both pair members from the neighbour set is essential: including the immediate predecessor would mask isolation, since two points at the same remote position are trivially 0 m apart.

**10c. Small-gap bridging** — scans adjacent output point pairs for spatial gaps between `--min-distance` and 0.5 nm (926 m). These are GPS logger dropouts: the logger paused briefly and resumed close by, leaving a visible nick in the rendered track. Each such gap is filled silently with great-circle-interpolated points at `--min-distance` spacing; timestamps are distributed proportionally to distance. Inserted points are tagged `source="bridged"`. No flag or user interaction required — this runs as part of every simplification.

**10d. Gap detection and filling** *(optional — `--fix-gaps` or `--fix-gaps-auto`)* — walks the processed point list and identifies *underway gaps*: adjacent pairs where the time difference exceeds `--split-gap` hours **and** the vessel has moved (great-circle distance > 0 between the two points). Pure time-only gaps where the boat didn't move (e.g. an extended port call) are skipped — there is nothing to interpolate. For each underway gap, the tool prints the start and end time, duration, distance, estimated fill speed (mean of the 10-point average speed immediately before and after the gap), and a **human-readable location** for each endpoint sourced from the Nominatim reverse-geocoding API (OpenStreetMap) — e.g. `33.8568°S, 151.2153°E  (Sydney, New South Wales, Australia)`. Results are cached by 0.01° grid cell; open-ocean points and network failures fall back gracefully to bare coordinates. In interactive mode (`--fix-gaps`) the user is prompted for each gap; `--fix-gaps-auto` fills all gaps without prompting. Accepted gaps are filled with great-circle-interpolated points at `--min-distance` spacing, with timestamps distributed proportionally to distance. Inserted points are tagged `source="interpolated"`.

**11. Segment splitting** — before writing, splits the flat point list into separate track segments wherever the time gap between consecutive points exceeds `--split-gap` hours (default 24). Single-point segments and 2-point segments with total distance below 10 m are dropped. GPX viewers do not draw a connecting line between separate segments, so the track renders correctly across multi-day or multi-month gaps without straight lines across the ocean.

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
