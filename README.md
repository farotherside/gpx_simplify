# gpx_simplify

A Python tool for simplifying large GPX files from long-distance sailing voyages.

Merges all tracks and segments from multiple GPS sources into a single, clean, chronologically-sorted track — filtering out GPS anomalies and decimating to a target point spacing.

Built for a real-world use case: ten years of sailing across the Pacific, around Australia, and back to North America, captured across two overlapping GPS sources in a single ~100 MB GPX file.

## Features

- **Multi-source merge** — combines all tracks and segments from any number of sources, sorted chronologically into one unified track
- **Speed anomaly filter** — drops points that imply physically impossible speeds (e.g. a sailboat "teleporting" across the ocean between two fixes)
- **Distance decimation** — reduces point density to a target spacing in metres, averaging clusters of nearby points into a centroid rather than simply discarding them
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

# Drop waypoints, maximum debug output:
python gpx_simplify.py -i voyage.gpx --no-waypoints -vvv
```

## Options

| Flag | Default | Description |
|---|---|---|
| `-i / --input FILE` | *(required)* | Input GPX file |
| `-o / --output FILE` | `<input>_simplified.gpx` | Output GPX file |
| `-d / --min-distance METRES` | `100` | Minimum distance between output points |
| `-m / --merge-distance METRES` | `100` | Points closer than this to the last emitted point are merged |
| `-s / --max-speed KNOTS` | `50` | Speed above which a point is treated as a GPS error |
| `--waypoints / --no-waypoints` | waypoints on | Copy waypoints to output |
| `-v / -vv / -vvv` | quiet | Verbosity: info / debug / trace |
| `--dry-run` | off | Parse and filter but do not write output |

## How it works

**1. Parse** — reads the input GPX with [gpxpy](https://github.com/tkrajina/gpxpy), collecting every track point with its timestamp and source label.

**2. Sort** — merges all points from all tracks and segments into a single chronological list (points without timestamps are appended at the end).

**3. Speed filter** — walks the sorted list and drops any point where *both* the incoming and outgoing legs exceed the speed threshold. The two-sided check avoids falsely dropping a valid point that happens to follow or precede a tight cluster.

**4. Decimate** — walks the filtered list, accumulating nearby points into clusters. When the accumulated distance from the last emitted point reaches `--min-distance`, the centroid of the current cluster is emitted as one output point. This averages overlapping fixes from multiple sources rather than arbitrarily picking one.

**5. Write** — writes a new GPX file with a single track segment, optional waypoints, and metadata describing the processing parameters.

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
