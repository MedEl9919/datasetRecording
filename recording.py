"""
OAK camera recorder - color camera (CAM_A, IMX378) for dataset collection
Live preview with resolution / FPS / codec options and live image controls
(focus, exposure time, ISO, white balance), plus Start / Pause / Stop recording.

Video is compressed on the camera (H.265 / H.264 / MJPEG hardware encoder), so
only the compressed stream crosses the network and the full frame rate is kept
at high resolutions. Frames are written to the .mp4 with their real capture
timestamps, so playback speed is correct even if a frame is ever dropped.

Every recording produces three files in OUTPUT_DIR:
    <name>.mp4          the video
    <name>_frames.csv   one row per recorded frame: sequence number, timestamps,
                        frame type/size, and the exposure / ISO / lens position /
                        white balance of that frame
    <name>_meta.json    device, stream + encoder settings, control settings, and
                        summary metrics (duration, frames, dropped frames, avg
                        FPS, bitrate, min/max of each actual control value)

Requirements:
    uv sync   (or: pip install "depthai>=3" opencv-python pillow av)
"""
import csv
import json
import os
import threading
import time
from datetime import datetime
from fractions import Fraction
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

import av
import cv2
import depthai as dai
from PIL import Image, ImageTk

# ---------------- Settings ----------------
DISPLAY_W = 960                # max preview width in the window (and of the preview stream)
DISPLAY_MAX_H = 560            # max preview height, so 4:3 modes still fit on a laptop screen
OUTPUT_DIR = "recordings"      # where videos are saved
CONNECT_TIMEOUT = 40           # seconds to keep retrying while the PoE camera reboots

# Recorded size -> max FPS with H.26x, max FPS with MJPEG (None = not supported).
# Limits measured on the OAK-D Pro PoE AF with the preview stream running.
# H.26x needs width % 32 == 0 and height % 8 == 0.
COLOR_MODES = {
    "1280×720 (720p)": ((1280, 720), 60, 60),
    "1920×1080 (1080p)": ((1920, 1080), 60, 60),
    "2016×1512 (3 MP, 4:3)": ((2016, 1512), 30, 30),
    "3840×2160 (4K)": ((3840, 2160), 20, 20),
    "4000×3000 (12 MP, MJPEG only)": ((4000, 3000), None, 15),
}
FPS_CHOICES = ["5", "10", "15", "20", "25", "30", "45", "60"]

# name -> (encoder profile, PyAV codec name)
CODECS = {
    "H.265": (dai.VideoEncoderProperties.Profile.H265_MAIN, "hevc"),
    "H.264": (dai.VideoEncoderProperties.Profile.H264_HIGH, "h264"),
    "MJPEG": (dai.VideoEncoderProperties.Profile.MJPEG, "mjpeg"),
}
BITRATE_CHOICES = ["auto", "10", "20", "40", "60", "80"]   # Mbit/s, H.26x
QUALITY_CHOICES = ["80", "90", "95", "98", "100"]          # JPEG quality, MJPEG
AUTO_BITS_PER_PIXEL = 0.33     # "auto" bitrate = width * height * fps * this (≈20 Mbit/s at 1080p30)

# IMX378 control ranges
LENS_RANGE = (0, 255)          # 0 = far (infinity), 255 = macro
ISO_RANGE = (100, 1600)
WB_RANGE = (1000, 12000)       # Kelvin
EXPOSURE_MIN_US = 20

DEFAULT_CONTROLS = {
    "focus": {"auto": True, "lens_position": 120},
    "exposure": {"auto": True, "exposure_us": 10000, "iso": 400},
    "white_balance": {"auto": True, "kelvin": 5000},
}

CSV_COLUMNS = ["frame_index", "sequence_num", "pts_s", "device_time_s", "host_time_s",
               "host_unix_time", "frame_type", "size_bytes", "exposure_us", "iso",
               "lens_position", "color_temp_k"]
METRIC_FIELDS = ["exposure_us", "iso", "lens_position", "color_temp_k"]
# ------------------------------------------


def fmt_time(seconds):
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def frame_metadata(msg):
    """Per-frame metadata of an EncodedFrame."""
    return {
        "sequence_num": msg.getSequenceNum(),
        "device_time_s": round(msg.getTimestampDevice().total_seconds(), 6),
        "host_time_s": round(msg.getTimestamp().total_seconds(), 6),
        "host_unix_time": round(time.time(), 6),
        "frame_type": msg.getFrameType().name,
        "exposure_us": int(round(msg.getExposureTime().total_seconds() * 1e6)),
        "iso": msg.getSensitivity(),
        "lens_position": msg.getLensPosition(),
        "color_temp_k": msg.getColorTemperature(),
    }


def build_control(c):
    """Turn a controls dict (see DEFAULT_CONTROLS) into a dai.CameraControl."""
    ctrl = dai.CameraControl()
    if c["focus"]["auto"]:
        ctrl.setAutoFocusMode(dai.CameraControl.AutoFocusMode.CONTINUOUS_VIDEO)
    else:
        ctrl.setManualFocus(c["focus"]["lens_position"])
    if c["exposure"]["auto"]:
        ctrl.setAutoExposureEnable()
    else:
        ctrl.setManualExposure(c["exposure"]["exposure_us"], c["exposure"]["iso"])
    if c["white_balance"]["auto"]:
        ctrl.setAutoWhiteBalanceMode(dai.CameraControl.AutoWhiteBalanceMode.AUTO)
    else:
        ctrl.setManualWhiteBalance(c["white_balance"]["kelvin"])
    return ctrl


def describe_controls(c):
    """Controls dict -> JSON-friendly description with explicit modes."""
    f, e, w = c["focus"], c["exposure"], c["white_balance"]
    return {
        "focus": {"mode": "auto"} if f["auto"] else
                 {"mode": "manual", "lens_position": f["lens_position"]},
        "exposure": {"mode": "auto"} if e["auto"] else
                    {"mode": "manual", "exposure_us": e["exposure_us"], "iso": e["iso"]},
        "white_balance": {"mode": "auto"} if w["auto"] else
                         {"mode": "manual", "kelvin": w["kelvin"]},
    }


class Mp4Muxer:
    """Writes already-encoded frames into an .mp4 with explicit timestamps
    (microsecond time base), without re-encoding."""

    def __init__(self, path, codec, size, fps):
        self.container = av.open(path, "w")
        stream = self.container.add_stream(CODECS[codec][1], rate=fps)
        stream.width, stream.height = size
        stream.time_base = Fraction(1, 1_000_000)
        if codec == "MJPEG":
            stream.pix_fmt = "yuvj420p"
        elif codec == "H.265":
            stream.codec_context.options = {"x265-params": "log-level=none"}
        self.stream = stream

    def write(self, data, pts_us):
        packet = av.Packet(bytes(data))
        packet.pts = packet.dts = pts_us
        packet.stream = self.stream
        self.container.mux(packet)

    def close(self):
        self.container.close()


class ValueControl(tk.Frame):
    """Exact-value editor: [−big] [−] [value] [+] [+big]  [preset] [preset] …

    The value can be typed (Enter or leaving the box applies it), stepped with
    the buttons (hold to repeat) or the mouse wheel, or set with a preset.
    Values are clamped to [lo, hi]; on_change() is called after every change."""

    def __init__(self, parent, var, lo, hi, step, big_step, unit, presets, on_change):
        super().__init__(parent)
        self.var, self.lo, self.hi = var, lo, hi
        self.on_change = on_change
        self.text = tk.StringVar(value=str(var.get()))

        def btn(label, delta):
            return tk.Button(self, text=label, width=4, repeatdelay=400, repeatinterval=80,
                             command=lambda: self.set(self.var.get() + delta))

        self.buttons = [btn(f"−{big_step}", -big_step), btn(f"−{step}", -step)]
        self.entry = tk.Entry(self, textvariable=self.text, width=7, justify="center",
                              font=("Consolas", 11, "bold"))
        self.entry.bind("<Return>", lambda _e: self.commit())
        self.entry.bind("<FocusOut>", lambda _e: self.commit())
        self.entry.bind("<MouseWheel>", lambda e: self.set(self.var.get() + (step if e.delta > 0 else -step)))
        self.buttons += [btn(f"+{step}", step), btn(f"+{big_step}", big_step)]

        for w in self.buttons[:2]:
            w.pack(side=tk.LEFT)
        self.entry.pack(side=tk.LEFT, padx=3)
        for w in self.buttons[2:]:
            w.pack(side=tk.LEFT)
        tk.Label(self, text=unit, width=3, anchor="w").pack(side=tk.LEFT, padx=(2, 8))

        for label, value in presets:
            b = tk.Button(self, text=label, command=lambda v=value: self.set(v))
            b.pack(side=tk.LEFT, padx=1)
            self.buttons.append(b)

        var.trace_add("write", lambda *_: self.text.set(str(self.var.get())))

    def set(self, value):
        value = max(self.lo, min(self.hi, int(value)))
        if value != self.var.get():
            self.var.set(value)
            self.on_change()
        self.text.set(str(value))

    def commit(self):
        try:
            self.set(float(self.text.get().replace(",", ".")))
        except ValueError:
            self.text.set(str(self.var.get()))   # not a number: show the current value again

    def set_range(self, lo, hi):
        self.lo, self.hi = lo, hi
        if self.var.get() > hi:
            self.var.set(hi)

    def set_state(self, state, greyed):
        for w in self.buttons:
            w.config(state=state)
        self.entry.config(state=state, disabledforeground="gray60" if greyed else "black")


class RecorderApp:
    def __init__(self, root):
        self.root = root
        root.title("OAK Camera Recorder")
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        # Shared state (camera thread <-> UI thread)
        self.lock = threading.Lock()
        self.latest_frame = None    # latest preview frame
        self.latest_meta = None     # metadata of the latest encoded frame
        self.frame_count = 0        # encoded frames received (for measured FPS)
        self.byte_count = 0         # encoded bytes received (for measured bitrate)
        self.muxer = None
        self.csv_file = None
        self.csv_writer = None
        self.recording = False
        self.paused = False
        self.need_keyframe = False  # start/resume must begin on an I-frame
        self.frames_written = 0
        self.bytes_written = 0
        self.frames_dropped = 0     # sequence-number gaps while recording
        self.last_seq = None
        self.pts_offset = None      # device time (s) that maps to pts 0 of the video
        self.last_pts = None        # pts (s) of the last written frame
        self.metric_ranges = {}     # field -> [min, max] over recorded frames
        self.running = True
        self.error = None
        self.segment_start = None   # monotonic() when the current active (non-paused) segment began
        self.elapsed_accum = 0.0    # seconds accumulated from completed segments (before this pause)
        self.pause_count = 0
        self.current_stem = None
        self.current_paths = {}     # "video" / "frames" -> path
        self.rec_info = {}          # settings snapshot taken at start of recording
        self.camera_ready = False
        self.device_info = {}
        self.encoded_size = None    # (w, h) the encoder actually produces
        self.ctrl_queue = None
        self.cfg = None             # active stream config (see apply_settings)
        self.gen = 0                # bumped on every camera restart
        self.stop_event = None
        self.cam_thread = None
        self.rate_t = time.monotonic()
        self.rate_counts = (0, 0)
        self.rate = None            # (fps, Mbit/s) of the encoded stream
        self._ctrl_after = None

        # ---------- UI: stream options (need a camera restart) ----------
        opts = tk.LabelFrame(root, text="Stream (Apply restarts the camera)")
        opts.pack(fill="x", padx=10, pady=(10, 0))

        self.color_mode = tk.StringVar(value="1920×1080 (1080p)")
        self.fps_var = tk.StringVar(value="30")
        self.codec_var = tk.StringVar(value="H.265")
        self.quality_var = tk.StringVar(value="auto")
        self.quality_label = tk.Label(opts, text="Bitrate (Mbit/s):")
        self.quality_box = ttk.Combobox(opts, textvariable=self.quality_var,
                                        values=BITRATE_CHOICES, state="readonly", width=5)
        codec_box = ttk.Combobox(opts, textvariable=self.codec_var, values=list(CODECS),
                                 state="readonly", width=6)
        codec_box.bind("<<ComboboxSelected>>", lambda _e: self.on_codec_change())
        self.stream_widgets = [
            tk.Label(opts, text="Resolution:"),
            ttk.Combobox(opts, textvariable=self.color_mode, values=list(COLOR_MODES),
                         state="readonly", width=27),
            tk.Label(opts, text="FPS:"),
            ttk.Combobox(opts, textvariable=self.fps_var, values=FPS_CHOICES,
                         state="readonly", width=4),
            tk.Label(opts, text="Codec:"),
            codec_box,
            self.quality_label,
            self.quality_box,
            tk.Button(opts, text="Apply", width=8, command=self.apply_settings),
        ]
        for w in self.stream_widgets:
            w.pack(side=tk.LEFT, padx=(6, 2), pady=6)

        # ---------- UI: image controls (applied live) ----------
        ctl = tk.LabelFrame(root, text="Image controls (applied live, locked while recording)")
        ctl.pack(fill="x", padx=10, pady=(6, 0))

        d = DEFAULT_CONTROLS
        self.focus_auto = tk.BooleanVar(value=d["focus"]["auto"])
        self.lens_var = tk.IntVar(value=d["focus"]["lens_position"])
        self.exp_auto = tk.BooleanVar(value=d["exposure"]["auto"])
        self.exp_var = tk.IntVar(value=d["exposure"]["exposure_us"])
        self.iso_var = tk.IntVar(value=d["exposure"]["iso"])
        self.wb_auto = tk.BooleanVar(value=d["white_balance"]["auto"])
        self.wb_var = tk.IntVar(value=d["white_balance"]["kelvin"])

        def value_control(var, lo, hi, step, big_step, unit, presets):
            return ValueControl(ctl, var, lo, hi, step, big_step, unit, presets,
                                on_change=self.schedule_controls)

        self.lens_ctl = value_control(self.lens_var, *LENS_RANGE, 1, 10, "", [])
        self.exp_ctl = value_control(self.exp_var, EXPOSURE_MIN_US, 33333, 100, 1000, "µs", [
            ("1 ms", 1000), ("2 ms", 2000), ("5 ms", 5000), ("10 ms", 10000),
            ("20 ms", 20000), ("30 ms", 30000)])
        self.iso_ctl = value_control(self.iso_var, *ISO_RANGE, 10, 100, "", [
            ("100", 100), ("200", 200), ("400", 400), ("800", 800), ("1600", 1600)])
        self.wb_ctl = value_control(self.wb_var, *WB_RANGE, 50, 500, "K", [
            ("3000 warm", 3000), ("4000", 4000), ("5000", 5000), ("6500 daylight", 6500)])

        def auto_box(var):
            return tk.Checkbutton(ctl, text="Auto", variable=var, command=self.on_auto_toggle)

        self.focus_box = auto_box(self.focus_auto)
        self.exp_box = auto_box(self.exp_auto)
        self.wb_box = auto_box(self.wb_auto)

        rows = [
            ("Focus", "Lens position (0 far … 255 near)", self.focus_box, self.lens_ctl),
            ("Exposure", "Exposure time", self.exp_box, self.exp_ctl),
            ("", "ISO (same Auto as exposure)", None, self.iso_ctl),
            ("White balance", "Color temperature", self.wb_box, self.wb_ctl),
        ]
        for r, (name, label, box, control) in enumerate(rows):
            tk.Label(ctl, text=name, font=("Arial", 9, "bold")).grid(row=r, column=0, sticky="w", padx=6)
            if box is not None:
                box.grid(row=r, column=1, sticky="w")
            tk.Label(ctl, text=label).grid(row=r, column=2, sticky="e", padx=(10, 4))
            control.grid(row=r, column=3, sticky="w", pady=2)

        self.lock_btn = tk.Button(ctl, text="Lock current\nauto values",
                                  command=self.lock_current_values)
        self.lock_btn.grid(row=0, column=4, rowspan=4, padx=12)

        self.readout = tk.Label(root, text="", anchor="w", font=("Consolas", 10))
        self.readout.pack(fill="x", padx=10, pady=(6, 0))

        # ---------- UI: preview + recording bar ----------
        # With an image set, Label width/height are pixels (without one they are characters)
        self.blank_image = tk.PhotoImage(width=1, height=1)
        self.video_label = tk.Label(root, bg="black", image=self.blank_image,
                                    width=DISPLAY_W, height=int(DISPLAY_W * 9 / 16))
        self.video_label.pack(padx=10, pady=10)

        bar = tk.Frame(root)
        bar.pack(pady=(0, 10))

        self.start_btn = tk.Button(bar, text="● Start recording", width=18,
                                   font=("Arial", 12, "bold"), fg="white", bg="#c0392b",
                                   state=tk.DISABLED, command=self.start_recording)
        self.start_btn.pack(side=tk.LEFT, padx=10)

        self.pause_btn = tk.Button(bar, text="⏸ Pause", width=14,
                                   font=("Arial", 12, "bold"),
                                   state=tk.DISABLED, command=self.toggle_pause)
        self.pause_btn.pack(side=tk.LEFT, padx=10)

        self.timer_label = tk.Label(bar, text="00:00:00", width=9,
                                    font=("Courier", 26, "bold"))
        self.timer_label.pack(side=tk.LEFT, padx=10)

        self.stop_btn = tk.Button(bar, text="■ Stop recording", width=18,
                                  font=("Arial", 12, "bold"),
                                  state=tk.DISABLED, command=self.stop_recording)
        self.stop_btn.pack(side=tk.LEFT, padx=10)

        self.status = tk.Label(root, text="", anchor="w")
        self.status.pack(fill="x", padx=10, pady=(0, 8))

        self.on_auto_toggle(send=False)
        self.apply_settings()
        self.update_ui()

    # ---------- Stream settings ----------
    def on_codec_change(self):
        if self.codec_var.get() == "MJPEG":
            self.quality_label.config(text="JPEG quality:")
            self.quality_box.config(values=QUALITY_CHOICES)
            self.quality_var.set("95")
        else:
            self.quality_label.config(text="Bitrate (Mbit/s):")
            self.quality_box.config(values=BITRATE_CHOICES)
            self.quality_var.set("auto")

    def apply_settings(self):
        size, max_fps_h26x, max_fps_mjpeg = COLOR_MODES[self.color_mode.get()]
        notes = []
        if max_fps_h26x is None and self.codec_var.get() != "MJPEG":
            self.codec_var.set("MJPEG")
            self.on_codec_change()
            notes.append("this resolution needs MJPEG, codec switched")
        codec = self.codec_var.get()
        max_fps = max_fps_mjpeg if codec == "MJPEG" else max_fps_h26x
        fps = int(self.fps_var.get())
        if fps > max_fps:
            fps = max_fps
            self.fps_var.set(str(fps))
            notes.append(f"FPS capped at {fps} for this resolution")

        if codec == "MJPEG":
            bitrate_kbps, quality = None, int(self.quality_var.get())
        else:
            q = self.quality_var.get()
            mbps = (size[0] * size[1] * fps * AUTO_BITS_PER_PIXEL / 1e6) if q == "auto" else float(q)
            bitrate_kbps, quality = int(mbps * 1000), None

        disp_w = min(DISPLAY_W, int(DISPLAY_MAX_H * size[0] / size[1]))
        preview_size = (disp_w // 2 * 2, int(round(disp_w * size[1] / size[0] / 2)) * 2)

        # Exposure can't be longer than one frame interval
        max_exp = int(1e6 / fps)
        self.exp_ctl.set_range(EXPOSURE_MIN_US, max_exp)

        # Stop the old camera thread; the new one waits for it to release the device.
        if self.stop_event is not None:
            self.stop_event.set()
        prev_thread = self.cam_thread

        with self.lock:
            self.gen += 1
            self.latest_frame = None
            self.latest_meta = None
            self.encoded_size = None
            self.frame_count = 0
            self.byte_count = 0
        self.ctrl_queue = None
        self.cfg = {"size": size, "fps": fps, "codec": codec, "bitrate_kbps": bitrate_kbps,
                    "quality": quality, "keyframe_interval": fps, "preview_size": preview_size}
        self.error = None
        self.camera_ready = False
        self.rate_t = time.monotonic()
        self.rate_counts = (0, 0)
        self.rate = None
        self.video_label.config(image=self.blank_image, width=preview_size[0], height=preview_size[1])
        self.video_label.image = None
        self.readout.config(text="")

        self.start_btn.config(state=tk.DISABLED)
        msg = "Connecting to OAK camera… (a PoE camera can take ~20 s to restart)"
        if notes:
            msg += "  —  " + "; ".join(notes)
        self.status.config(text=msg, fg="black")

        self.stop_event = threading.Event()
        self.cam_thread = threading.Thread(
            target=self.camera_loop,
            args=(dict(self.cfg), self.get_controls(), self.gen, self.stop_event, prev_thread),
            daemon=True)
        self.cam_thread.start()

    # ---------- Image controls ----------
    def get_controls(self):
        return {
            "focus": {"auto": self.focus_auto.get(), "lens_position": self.lens_var.get()},
            "exposure": {"auto": self.exp_auto.get(), "exposure_us": self.exp_var.get(),
                         "iso": self.iso_var.get()},
            "white_balance": {"auto": self.wb_auto.get(), "kelvin": self.wb_var.get()},
        }

    def on_auto_toggle(self, send=True):
        locked = self.recording
        for auto, controls in ((self.focus_auto, [self.lens_ctl]),
                               (self.exp_auto, [self.exp_ctl, self.iso_ctl]),
                               (self.wb_auto, [self.wb_ctl])):
            for c in controls:
                c.set_state(tk.DISABLED if auto.get() or locked else tk.NORMAL,
                            greyed=auto.get())
        if send:
            self.schedule_controls()

    def schedule_controls(self):
        # Debounce held +/- buttons: send once the value stops changing for 80 ms
        if self._ctrl_after is not None:
            self.root.after_cancel(self._ctrl_after)
        self._ctrl_after = self.root.after(80, self.send_controls)

    def send_controls(self):
        self._ctrl_after = None
        q = self.ctrl_queue
        if q is not None:
            try:
                q.send(build_control(self.get_controls()))
            except Exception as e:
                self.status.config(text=f"Could not send control: {e}", fg="red")

    def lock_current_values(self):
        """Freeze whatever auto focus / exposure / WB settled on, as manual values."""
        with self.lock:
            meta = self.latest_meta
        if meta is None:
            return
        self.lens_var.set(meta["lens_position"])
        self.exp_var.set(min(meta["exposure_us"], self.exp_ctl.hi))
        self.iso_var.set(max(ISO_RANGE[0], min(ISO_RANGE[1], meta["iso"])))
        self.wb_var.set(max(WB_RANGE[0], min(WB_RANGE[1], meta["color_temp_k"])))
        self.focus_auto.set(False)
        self.exp_auto.set(False)
        self.wb_auto.set(False)
        self.on_auto_toggle()

    def set_options_state(self, state):
        for w in self.stream_widgets:
            if isinstance(w, ttk.Combobox):
                w.config(state="readonly" if state == tk.NORMAL else tk.DISABLED)
            else:
                w.config(state=state)
        for w in (self.focus_box, self.exp_box, self.wb_box, self.lock_btn):
            w.config(state=state)
        self.on_auto_toggle(send=False)

    # ---------- Camera thread ----------
    def connect(self, stop_event):
        """Open the device, retrying while it is still rebooting after the last session."""
        deadline = time.monotonic() + CONNECT_TIMEOUT
        while True:
            try:
                return dai.Pipeline()
            except RuntimeError:
                if stop_event.is_set() or time.monotonic() > deadline:
                    raise
                time.sleep(1)

    def camera_loop(self, cfg, controls, gen, stop_event, prev_thread):
        try:
            if prev_thread is not None:
                prev_thread.join()
            if stop_event.is_set():
                return
            with self.connect(stop_event) as pipeline:
                device = pipeline.getDefaultDevice()
                self.device_info = {
                    "name": device.getDeviceName(),
                    "product": device.getProductName(),
                    "device_id": device.getDeviceId(),
                    "socket": "CAM_A",
                    "sensor": next((f.sensorName for f in device.getConnectedCameraFeatures()
                                    if f.socket == dai.CameraBoardSocket.CAM_A), "unknown"),
                }
                fps = cfg["fps"]
                cam = pipeline.create(dai.node.Camera).build(dai.CameraBoardSocket.CAM_A)
                rec_out = cam.requestOutput(cfg["size"], type=dai.ImgFrame.Type.NV12, fps=fps)
                encoder = pipeline.create(dai.node.VideoEncoder).build(
                    rec_out, frameRate=fps, profile=CODECS[cfg["codec"]][0],
                    keyframeFrequency=cfg["keyframe_interval"],
                    bitrate=(cfg["bitrate_kbps"] or 0) * 1000,
                    quality=cfg["quality"] or 80)
                # Blocking: if the host ever falls behind, frames are dropped on the
                # camera before encoding (a clean gap) instead of corrupting the stream.
                enc_q = encoder.out.createOutputQueue(maxSize=60, blocking=True)
                prev_q = cam.requestOutput(cfg["preview_size"], type=dai.ImgFrame.Type.NV12,
                                           fps=fps).createOutputQueue(maxSize=2, blocking=False)
                ctrl_queue = cam.inputControl.createInputQueue()

                pipeline.start()
                ctrl_queue.send(build_control(controls))
                self.ctrl_queue = ctrl_queue
                while not stop_event.is_set() and pipeline.isRunning():
                    got = False
                    for msg in enc_q.tryGetAll():
                        got = True
                        data = msg.getData()
                        meta = frame_metadata(msg)
                        meta["size_bytes"] = len(data)
                        with self.lock:
                            if gen != self.gen:
                                return
                            self.latest_meta = meta
                            self.encoded_size = (msg.getWidth(), msg.getHeight())
                            self.frame_count += 1
                            self.byte_count += len(data)
                            if self.recording and not self.paused and self.muxer is not None:
                                self.record_frame(data, meta, fps)
                    preview = prev_q.tryGet()
                    if preview is not None:
                        got = True
                        frame = preview.getCvFrame()
                        with self.lock:
                            if gen != self.gen:
                                return
                            self.latest_frame = frame
                    if not got:
                        time.sleep(0.001)
        except Exception as e:
            if not stop_event.is_set():
                self.error = str(e)

    def record_frame(self, data, meta, fps):
        """Called with self.lock held."""
        t = meta["device_time_s"]
        if self.need_keyframe:
            # A recording (or a resumed segment) must start on an I-frame to be decodable.
            if meta["frame_type"] != "I":
                return
            self.need_keyframe = False
            self.last_seq = None
            # Continue right after the previous segment, one frame interval later
            prev_end = self.last_pts + 1 / fps if self.last_pts is not None else 0.0
            self.pts_offset = t - prev_end

        seq = meta["sequence_num"]
        if self.last_seq is not None and seq > self.last_seq + 1:
            self.frames_dropped += seq - self.last_seq - 1
        self.last_seq = seq

        pts = t - self.pts_offset
        pts_us = int(round(pts * 1e6))
        if self.last_pts is not None and pts_us <= int(round(self.last_pts * 1e6)):
            pts_us = int(round(self.last_pts * 1e6)) + 1   # keep timestamps strictly increasing
        self.muxer.write(data, pts_us)
        self.last_pts = pts_us / 1e6

        self.csv_writer.writerow({"frame_index": self.frames_written,
                                  "pts_s": round(self.last_pts, 6), **meta})
        self.frames_written += 1
        self.bytes_written += meta["size_bytes"]
        for f in METRIC_FIELDS:
            r = self.metric_ranges.setdefault(f, [meta[f], meta[f]])
            r[0], r[1] = min(r[0], meta[f]), max(r[1], meta[f])

    # ---------- UI loop (main thread) ----------
    def update_ui(self):
        if self.error:
            self.status.config(text=f"Camera error: {self.error}", fg="red")
            self.start_btn.config(state=tk.DISABLED)
            if self.recording:
                self.stop_recording(ask_name=False)

        with self.lock:
            frame = self.latest_frame
            meta = self.latest_meta
            counts = (self.frame_count, self.byte_count)
            dropped = self.frames_dropped
            waiting = self.need_keyframe

        # Measured FPS / bitrate of the encoded stream, refreshed once a second
        now = time.monotonic()
        if now - self.rate_t >= 1.0:
            dt = now - self.rate_t
            self.rate = ((counts[0] - self.rate_counts[0]) / dt,
                         (counts[1] - self.rate_counts[1]) * 8 / dt / 1e6)
            self.rate_counts = counts
            self.rate_t = now

        if frame is not None and meta is not None and not self.camera_ready and not self.error:
            self.camera_ready = True
            if not self.recording:
                self.start_btn.config(state=tk.NORMAL)
            info = self.device_info
            self.status.config(text=f"Camera ready: {info.get('product')} · {info.get('sensor')} "
                                    f"· id {info.get('device_id')}", fg="black")

        if meta is not None:
            c = self.cfg
            text = (f"Actual  lens {meta['lens_position']:3d} │ exp {meta['exposure_us']:6d} µs │ "
                    f"ISO {meta['iso']:4d} │ WB {meta['color_temp_k']:5d} K │ "
                    f"{c['size'][0]}×{c['size'][1]} {c['codec']} @ {c['fps']}")
            if self.rate is not None:
                text += f" → {self.rate[0]:4.1f} fps, {self.rate[1]:5.1f} Mbit/s"
            if self.recording:
                text += " │ waiting for keyframe…" if waiting else f" │ dropped {dropped}"
            slow = self.rate is not None and self.rate[0] < 0.95 * c["fps"]
            self.readout.config(text=text, fg="#c0392b" if slow or dropped else "black")

        if frame is not None:
            small = frame.copy()   # the preview stream already has the display size
            if self.recording and not self.paused:
                cv2.circle(small, (25, 25), 10, (0, 0, 255), -1)
                cv2.putText(small, "REC", (42, 33), cv2.FONT_HERSHEY_SIMPLEX,
                            0.8, (0, 0, 255), 2)
            elif self.recording and self.paused:
                cv2.circle(small, (25, 25), 10, (0, 165, 255), -1)
                cv2.putText(small, "PAUSED", (42, 33), cv2.FONT_HERSHEY_SIMPLEX,
                            0.8, (0, 165, 255), 2)
            img = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(small, cv2.COLOR_BGR2RGB)))
            self.video_label.config(image=img, width=small.shape[1], height=small.shape[0])
            self.video_label.image = img  # keep a reference

        if self.recording:
            elapsed = self.elapsed_accum
            if not self.paused:
                elapsed += time.monotonic() - self.segment_start
            self.timer_label.config(text=fmt_time(elapsed))

        if self.running:
            self.root.after(30, self.update_ui)

    # ---------- Buttons ----------
    def start_recording(self):
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        c = self.cfg
        stem = datetime.now().strftime("rec_%Y%m%d_%H%M%S")
        paths = {"video": os.path.join(OUTPUT_DIR, f"{stem}.mp4"),
                 "frames": os.path.join(OUTPUT_DIR, f"{stem}_frames.csv")}
        try:
            muxer = Mp4Muxer(paths["video"], c["codec"], c["size"], c["fps"])
        except Exception as e:
            messagebox.showerror("Error", f"Could not create video file:\n{paths['video']}\n{e}")
            return
        csv_file = open(paths["frames"], "w", newline="", encoding="utf-8")
        csv_writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
        csv_writer.writeheader()

        # Apply a value typed without pressing Enter, and flush any pending
        # change, so the snapshot matches the camera
        for control in (self.lens_ctl, self.exp_ctl, self.iso_ctl, self.wb_ctl):
            control.commit()
        if self._ctrl_after is not None:
            self.root.after_cancel(self._ctrl_after)
            self.send_controls()

        self.rec_info = {
            "started": datetime.now().astimezone().isoformat(timespec="seconds"),
            "cfg": dict(c), "controls": self.get_controls(),
            "encoded_size": self.encoded_size,
        }
        with self.lock:
            self.muxer = muxer
            self.csv_file, self.csv_writer = csv_file, csv_writer
            self.frames_written = 0
            self.bytes_written = 0
            self.frames_dropped = 0
            self.last_seq = None
            self.last_pts = None
            self.need_keyframe = True
            self.metric_ranges = {}
            self.recording = True
            self.paused = False
        self.elapsed_accum = 0.0
        self.pause_count = 0
        self.segment_start = time.monotonic()
        self.current_stem = stem
        self.current_paths = paths

        self.set_options_state(tk.DISABLED)
        self.start_btn.config(state=tk.DISABLED)
        self.pause_btn.config(state=tk.NORMAL, text="⏸ Pause")
        self.stop_btn.config(state=tk.NORMAL)
        self.timer_label.config(text="00:00:00", fg="#c0392b")
        self.status.config(text=f"Recording → {paths['video']}", fg="black")

    def toggle_pause(self):
        with self.lock:
            if not self.recording:
                return
            if not self.paused:
                # Pausing: fold this segment's elapsed time into the accumulator.
                self.elapsed_accum += time.monotonic() - self.segment_start
                self.paused = True
                self.pause_count += 1
            else:
                # Resuming: start a fresh segment from now, on the next keyframe;
                # frames skipped while paused are not counted as dropped.
                self.segment_start = time.monotonic()
                self.paused = False
                self.need_keyframe = True

        if self.paused:
            self.pause_btn.config(text="▶ Continue")
            self.timer_label.config(fg="#e67e22")
            self.status.config(text=f"Paused → {self.current_paths['video']}")
        else:
            self.pause_btn.config(text="⏸ Pause")
            self.timer_label.config(fg="#c0392b")
            self.status.config(text=f"Recording → {self.current_paths['video']}")

    def stop_recording(self, ask_name=True):
        with self.lock:
            self.recording = False
            self.paused = False
            muxer, self.muxer = self.muxer, None
            csv_file, self.csv_file, self.csv_writer = self.csv_file, None, None
            n = self.frames_written
            nbytes = self.bytes_written
            dropped = self.frames_dropped
            last_pts = self.last_pts
            ranges = dict(self.metric_ranges)

        self.elapsed_accum = 0.0
        self.segment_start = None

        if muxer is not None:
            try:
                muxer.close()
            except Exception as e:
                messagebox.showerror("Error", f"Could not finalize video:\n{e}")
        if csv_file is not None:
            csv_file.close()

        if ask_name and muxer is not None:
            name = simpledialog.askstring(
                "Save recording", "Video name:",
                initialvalue=self.current_stem, parent=self.root,
            )
            if name:
                self._rename_recording(name)

        self.timer_label.config(text="00:00:00", fg="black")
        self.pause_btn.config(state=tk.DISABLED, text="⏸ Pause")
        self.stop_btn.config(state=tk.DISABLED)
        self.set_options_state(tk.NORMAL)
        if not self.error and self.camera_ready:
            self.start_btn.config(state=tk.NORMAL)
        if muxer is None:
            return

        c = self.rec_info["cfg"]
        fps = c["fps"]
        # Duration of the video itself, from the frame timestamps
        duration = last_pts + 1 / fps if last_pts is not None else 0.0
        avg_fps = n / duration if duration > 0 else 0
        avg_mbps = nbytes * 8 / duration / 1e6 if duration > 0 else 0
        encoder = {"codec": c["codec"], "profile": CODECS[c["codec"]][0].name,
                   "keyframe_interval_frames": c["keyframe_interval"]}
        if c["codec"] == "MJPEG":
            encoder["jpeg_quality"] = c["quality"]
        else:
            encoder["target_bitrate_kbps"] = c["bitrate_kbps"]
        enc_w, enc_h = self.rec_info["encoded_size"] or c["size"]
        meta_path = os.path.join(OUTPUT_DIR, f"{self.current_stem}_meta.json")
        meta = {
            "video_file": os.path.basename(self.current_paths["video"]),
            "frames_file": os.path.basename(self.current_paths["frames"]),
            "started": self.rec_info["started"],
            "device": self.device_info,
            "stream": {"width": enc_w, "height": enc_h, "requested_size": list(c["size"]),
                       "requested_fps": fps,
                       "encoded_on_device": True, **encoder,
                       "container": "mp4, per-frame timestamps from the camera clock"},
            "controls": describe_controls(self.rec_info["controls"]),
            "results": {
                "duration_s": round(duration, 3),
                "frames_written": n,
                "frames_dropped": dropped,
                "avg_fps": round(avg_fps, 3),
                "avg_bitrate_mbps": round(avg_mbps, 2),
                "file_size_mb": round(os.path.getsize(self.current_paths["video"]) / 1e6, 2),
                "pauses": self.pause_count,
            },
            "actual_control_ranges": {f: {"min": r[0], "max": r[1]} for f, r in ranges.items()},
            "software": {"depthai": dai.__version__, "pyav": av.__version__},
        }
        try:
            with open(meta_path, "w", encoding="utf-8") as fh:
                json.dump(meta, fh, indent=2)
        except OSError as e:
            messagebox.showerror("Error", f"Could not write metadata:\n{e}")

        text = (f"Saved {self.current_paths['video']} (+ _frames.csv, _meta.json)  —  "
                f"{fmt_time(duration)}, {n} frames, {dropped} dropped, avg {avg_fps:.1f} fps, "
                f"{avg_mbps:.1f} Mbit/s")
        warn = dropped > 0 or avg_fps < 0.95 * fps
        if warn:
            text += "  ⚠ check _frames.csv"
        self.status.config(text=text, fg="#d35400" if warn else "black")

    def _rename_recording(self, new_name):
        """Rename the just-saved files to <new_name>.mp4 / <new_name>_frames.csv,
        keeping them in OUTPUT_DIR and avoiding collisions. Updates
        current_stem / current_paths on success."""
        safe = "".join(c for c in new_name.strip() if c not in '<>:"/\\|?*').strip()
        if not safe or safe == self.current_stem:
            return

        def targets(stem):
            return {"video": os.path.join(OUTPUT_DIR, f"{stem}.mp4"),
                    "frames": os.path.join(OUTPUT_DIR, f"{stem}_frames.csv"),
                    "meta": os.path.join(OUTPUT_DIR, f"{stem}_meta.json")}

        stem, i = safe, 1
        while any(os.path.exists(p) for p in targets(stem).values()):
            stem = f"{safe}_{i}"
            i += 1

        new_paths = targets(stem)
        try:
            for key, old_path in list(self.current_paths.items()):
                os.rename(old_path, new_paths[key])
                self.current_paths[key] = new_paths[key]
            self.current_stem = stem
        except OSError as e:
            messagebox.showerror("Rename failed", f"Could not rename file:\n{e}")

    def on_close(self):
        if self.recording:
            self.stop_recording(ask_name=False)
        self.running = False
        if self.stop_event is not None:
            self.stop_event.set()
        if self.cam_thread is not None:
            self.cam_thread.join(timeout=3)
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    RecorderApp(root)
    root.mainloop()
