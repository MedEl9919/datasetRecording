# OAK Camera Dataset Recorder

A desktop app (Tkinter) for recording video datasets with a **Luxonis OAK-D Pro PoE AF** color camera (CAM_A, Sony IMX378).
It shows a live preview and lets you set resolution, FPS, codec, focus, exposure, ISO and white balance.
Each recording is saved with per-frame metadata and a summary file, so every clip can be checked and compared.

Built for collecting crowd-counting and tracking data (conversion, commitment and retention KPIs).

---

## Setup

Requires [uv](https://docs.astral.sh/uv/) (it also installs the right Python version).

```powershell
# once, in the project folder
uv sync

# run
uv run recording.py
```

The camera must be powered over PoE and reachable on the same network as the PC (e.g. `192.168.0.x`).

If `uv` isn't recognized right after installing it, restart VS Code or the terminal, or run:
`$env:Path = "C:\Users\$env:USERNAME\.local\bin;$env:Path"`

Dependencies (`pyproject.toml`): `depthai>=3`, `opencv-python`, `pillow`, `av` (PyAV).

---

## Using the app

### 1. Stream (Apply restarts the camera, ~10–20 s)

| Option | Values |
|---|---|
| Resolution | 720p, 1080p, 2016×1512, 4K, 4000×3000 (12 MP) |
| FPS | 5–60, automatically capped to what the resolution supports |
| Codec | H.265 (default), H.264, MJPEG |
| Bitrate / quality | H.264/H.265: `auto` (≈20 Mbit/s at 1080p30) or 10–80 Mbit/s. MJPEG: JPEG quality 80–100 |

Video is **encoded on the camera**, so only the compressed stream goes over the network and the full frame rate is kept.

### 2. Image controls (applied instantly, locked while recording)

| Control | Range | Presets |
|---|---|---|
| Focus: lens position | 0 (far) … 255 (near) | — |
| Exposure time | 20 µs … 1/FPS (33 333 µs at 30 fps) | 1, 2, 5, 10, 20, 30 ms |
| ISO | 100 … 1600 | 100, 200, 400, 800, 1600 |
| White balance | 1000 … 12000 K | 3000, 4000, 5000, 6500 K |

Ways to set a value:
- Click the **−/+** buttons (hold to repeat).
- Type the value and press **Enter**.
- Use the **mouse wheel** over the value box.
- Click a **preset**.

Untick **Auto** to use your manual value. ISO uses the same Auto checkbox as exposure.

**Lock current auto values** copies the focus, exposure, ISO and white balance that auto mode has settled on into the manual fields.
Let auto mode run for a few seconds before clicking it.

### 3. Readout line

This line shows what the camera is **actually** using: lens position, exposure, ISO, white balance, measured fps and Mbit/s.
While recording it also shows the number of dropped frames.
The line turns **red** if the fps falls below the target or frames are dropped.

### 4. Recording

- **● Start recording**: recording begins on the next keyframe (within 1 s).
- **⏸ Pause / ▶ Continue**: the video continues seamlessly, with no gap.
- **■ Stop recording**: asks for a name, then saves the files.

---

## Output files

Each recording writes three files to `recordings/`:

| File | Contents |
|---|---|
| `<name>.mp4` | The video. Each frame carries its real capture timestamp, so playback speed is always correct. |
| `<name>_frames.csv` | One row per recorded frame (columns below) |
| `<name>_meta.json` | Device, stream and encoder settings, control settings, and summary metrics |

### `_frames.csv` columns

| Column | Meaning |
|---|---|
| `frame_index` | Index of the frame in the video (0, 1, 2 …) |
| `sequence_num` | Camera frame counter. A gap means a dropped frame. |
| `pts_s` | Time of the frame in the video (s) |
| `device_time_s` | Capture time on the camera clock (s) |
| `host_time_s` | Capture time converted to the PC's monotonic clock (s) |
| `host_unix_time` | PC wall-clock time when the frame was received (Unix seconds). Use it to align sessions and to get time of day. |
| `frame_type` | `I` (keyframe) or `P` |
| `size_bytes` | Size of the encoded frame |
| `exposure_us`, `iso`, `lens_position`, `color_temp_k` | Values the camera **actually** used for this frame |

### `_meta.json` sections

- **`device`**: model, sensor and serial number
- **`stream`**:
  - actual and requested size
  - fps
  - codec and profile
  - bitrate or JPEG quality
  - keyframe interval
- **`controls`**: auto/manual mode and the values you set
- **`results`**:
  - `duration_s`, `frames_written`, `frames_dropped`, `avg_fps`
  - `avg_bitrate_mbps`, `file_size_mb`, `pauses`
- **`actual_control_ranges`**: min/max of each value the camera actually used. With manual controls, min equals max.
- **`software`**: depthai and PyAV versions

> For time-based KPIs (dwell time, time of day, returns), use the CSV timestamps, not `frame_index / fps`.

---

## Tested limits (with the live preview running)

| Resolution | Codec | Max fps | Typical bitrate | Storage per hour |
|---|---|---|---|---|
| 720p / 1080p | H.265 or H.264 | 30 | ~20 Mbit/s (auto) | ~10 GB |
| 2016×1512 | H.265 / H.264 | 30 | auto | — |
| 3840×2160 (4K) | H.265 / H.264 | 20 | ~55 Mbit/s (auto) | ~25 GB |
| 4000×3000 (12 MP) | MJPEG only | 15 | ~320 Mbit/s | ~145 GB |

The 720p/1080p, 4K and 12 MP modes were recorded with 0 dropped frames.
1080p at 60 fps and 2016×1512 are allowed in the app but haven't been tested yet.

---

## Recommended settings for crowd counting / tracking

| Setting | Value |
|---|---|
| Stream | **1080p · 30 fps · H.265 · 40 Mbit/s**. Use 4K at 20 fps only if people look small (< ~50 px tall). |
| Focus | Manual, focused on where the people stand |
| Exposure | Manual **10 ms**. It keeps walking people sharp and avoids flicker from 50 Hz mains lighting. |
| ISO | Raise until faces and clothing are clearly visible |
| White balance | Manual, matched to the light on people's faces (indoor ≈ 3500–4500 K) |

**Camera facing a glass wall (backlight):**
- Set exposure for the **people** and let the glass turn white.
- Exclude the glass area in your analysis, because reflections can be detected as "ghost" people.
- Re-check ISO before each session (daylight changes during the day).
- Prefer sessions of 30–60 minutes.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `uv` is not recognized | Restart VS Code or the terminal (see Setup) |
| Stuck on "Connecting…" or `No available devices` | Wait ~20 s after Apply or after closing the app, because the camera is rebooting. Check the Ethernet/PoE link. |
| "Device has crashed" | Unplug the Ethernet cable for a few seconds and reconnect it, then restart the app |
| Readout is red / frames dropped | Lower the resolution or FPS, or check the network link (it should be 1 Gbit/s) |
| Image too dark with manual exposure | Raise ISO. If you're already at 1600, the scene is too dark for 10 ms. |

