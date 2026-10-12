# Video Frame Extractor

A small Python/Tkinter tool that extracts JPEG frames from video and timestamps each image (filename, EXIF, and optionally Windows file times) using the video's recording-start metadata.

There are two separate programs, and their timestamps mean different things:

| Program | Timestamp is | Use when |
|---|---|---|
| `extractor_ui_pts.py` | PTS of the actual selected source frame | source-frame timing matters |
| `extractor_ui_fps.py` | Requested output sampling time | a simple regular sampling timeline is enough |

## Timestamps

`recording_start` comes from video metadata (Apple QuickTime creation date preferred, otherwise generic creation time). It is assumed to represent the time of the first frame; this is not independently verified. If no usable creation metadata is available, extraction cannot establish the recording-start timestamp and there is no fallback to filesystem time.

**PTS-based:** `frame_time = recording_start + (selected_pts - first_selected_pts)`. FPS defines the sampling grid, and each grid point is matched to the nearest source frame.

**FPS-grid:** `sampling_time = recording_start + frame_index / fps`, with `frame_index` starting at 0. FFmpeg's `fps` filter may duplicate frames; duplicates receive distinct sampling timestamps. The timestamp is the requested sampling time, not necessarily the source frame's PTS.

The same timestamp is used for the filename, EXIF, and optional Windows file times. Windows file-time synchronization is off by default and Windows-only.

Filenames use `YYYYMMDD_HHMMSS_fff_NNNNNN.jpg`, where `NNNNNN` is the 1-based output sequence number.

## Requirements

* Python 3, FFmpeg/FFprobe in `PATH`
* `pip install ffmpeg-python piexif`
* Linux: Tkinter may need to be installed separately

Tested with: Python 3.11, FFmpeg 7.x, Windows 11.

## Usage

~~~bash
python extractor_ui_pts.py
python extractor_ui_fps.py
~~~

Select a video, set FPS (the grid rate for PTS-based, the output rate for FPS-grid), then click **Extract**. JPEGs and the corresponding log file are written to the output folder. Existing files with the same generated name are overwritten.

## Diagnostics

The PTS-based extractor writes:

* `ffmpeg_last_run.log` — FFmpeg command, return code, stdout/stderr.
* `extraction_benchmark.log` — timing and frame-selection diagnostics.

Selection error is the temporal difference between the selected source-frame PTS and its requested sampling position. JPEG-size diagnostics describe the generated JPEG-size distribution and are observational only; neither diagnostic affects frame selection or output.

The FPS-grid extractor writes:

* `fps_extraction_log.log` — extraction parameters, frame counts, timings, FFmpeg diagnostics, and the output sampling-time map.

## Limitations

* The packet-PTS-to-FFmpeg-frame mapping has been empirically tested on the target iPhone MOV workflow. It is not guaranteed for other codecs, containers, or timestamp structures, and a mapping mismatch is not independently detected.
* DTS fallback is not equivalent to PTS when frames are reordered.
* The FPS-grid timestamp is a requested sampling time, not a measured source-frame time.