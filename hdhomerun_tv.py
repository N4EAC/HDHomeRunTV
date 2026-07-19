from __future__ import annotations

import json
import gzip
import io
import hashlib
import xml.etree.ElementTree as ET
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from PIL import Image, ImageDraw, ImageFont, ImageTk
except ImportError:
    Image = ImageDraw = ImageFont = ImageTk = None
import tkinter as tk
from tkinter import messagebox, ttk

APP_NAME = "HDHomeRun TV Player"
APP_VERSION = "1.0.10"
SSDP_ADDR = ("239.255.255.250", 1900)
SSDP_ST = "urn:schemas-upnp-org:device:MediaServer:1"

BG = "#080b0f"
PANEL = "#101419"
PANEL_2 = "#151a20"
BEZEL = "#1b1f24"
BLUE = "#2f9cff"
BLUE_DARK = "#194f93"
TEXT = "#eef4fb"
MUTED = "#9ba6b2"
RED = "#ff2a2a"
GREEN = "#34c759"


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", app_dir()))
    return base / relative


def engine_storage_dir() -> Path:
    root = Path(os.getenv("LOCALAPPDATA", Path.home())) / "HDHomeRunTV" / "engine"
    root.mkdir(parents=True, exist_ok=True)
    return root


def config_path() -> Path:
    root = Path(os.getenv("APPDATA", Path.home())) / "HDHomeRunTV"
    root.mkdir(parents=True, exist_ok=True)
    return root / "settings.json"


def set_windows_app_id() -> None:
    """Give the frameless executable a stable Windows taskbar identity."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "EduardoADeCarvalho.HDHomeRunTVPlayer.1"
        )
    except Exception:
        pass


def get_json(url: str, timeout: float = 4.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


@dataclass
class Device:
    name: str
    device_id: str
    ip: str
    base_url: str
    lineup_url: str
    model: str = "HDHomeRun"
    device_auth: str = ""


@dataclass
class Channel:
    guide_number: str
    guide_name: str
    url: str
    favorite: bool = False
    drm: bool = False
    logo_url: str = ""

    @property
    def station_badge(self) -> str:
        """Return a compact, always-visible station badge for the channel list.

        HDHomeRun lineup.json does not normally include logo artwork, so the app
        derives a short badge from the station/network name instead of showing
        an empty logo area.
        """
        name = self.guide_name.upper().replace("-TV", "").strip()
        known = ("ABC", "CBS", "NBC", "FOX", "PBS", "CW", "ION", "MY", "UNIV", "TELE", "ANT")
        for token in known:
            if token in name:
                return token[:4]
        words = [w for w in name.replace("/", " ").split() if w]
        if not words:
            return "TV"
        if len(words) == 1:
            return words[0][:4]
        initials = "".join(w[0] for w in words[:4])
        return initials[:4]

    @property
    def label(self) -> str:
        lock = "  🔒" if self.drm else ""
        return f"{self.guide_number:<7} {self.guide_name}{lock}"


@dataclass
class Program:
    channel_number: str
    title: str
    start: int
    end: int
    synopsis: str = ""
    episode: str = ""

    @property
    def time_text(self) -> str:
        return f"{time.strftime('%-I:%M %p', time.localtime(self.start)) if os.name != 'nt' else time.strftime('%I:%M %p', time.localtime(self.start)).lstrip('0')}–{time.strftime('%-I:%M %p', time.localtime(self.end)) if os.name != 'nt' else time.strftime('%I:%M %p', time.localtime(self.end)).lstrip('0')}"


class MPVController:
    def __init__(self, window_id: int) -> None:
        self.window_id = window_id
        self.process: subprocess.Popen[Any] | None = None
        self.current_url = ""
        self.last_error = ""
        self._stderr_handle = None
        self.pipe_name = rf"\\.\pipe\hdhomerun_tv_{os.getpid()}"
        self._pipe_lock = threading.Lock()

    @property
    def executable(self) -> Path | None:
        candidates = [
            app_dir() / "engine" / "mpv.exe",
            app_dir() / "mpv.exe",
            engine_storage_dir() / "mpv.exe",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    @staticmethod
    def install_engine() -> Path:
        if os.name != "nt":
            raise OSError("Automatic mpv installation is available on Windows only.")
        engine_dir = engine_storage_dir()
        archive_url = (
            "https://github.com/shinchiro/mpv-winbuild-cmake/releases/download/"
            "20260610/mpv-x86_64-20260610-git-304426c.7z"
        )
        archive = Path(tempfile.gettempdir()) / "hdhomerun_tv_mpv.7z"
        ps = (
            "$ProgressPreference='SilentlyContinue'; "
            f"Invoke-WebRequest -UseBasicParsing -Uri '{archive_url}' -OutFile '{archive}'; "
            f"& tar.exe -xf '{archive}' -C '{engine_dir}'; "
            "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }"
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=180,
        )
        try:
            archive.unlink(missing_ok=True)
        except OSError:
            pass
        exe = engine_dir / "mpv.exe"
        if completed.returncode != 0 or not exe.exists():
            detail = completed.stderr.strip() or "The playback engine could not be downloaded or extracted."
            raise OSError(detail)
        return exe

    def play(self, url: str, volume: int, boost_db: float) -> None:
        self.stop()
        exe = self.executable
        if exe is None:
            raise FileNotFoundError("The built-in playback engine has not been installed yet.")
        args = [
            str(exe),
            f"--wid={self.window_id}",
            "--no-config",
            "--no-terminal",
            "--force-window=yes",
            "--keep-open=no",
            "--osc=no",
            "--input-default-bindings=no",
            f"--input-ipc-server={self.pipe_name}",
            "--cache=yes",
            "--cache-secs=4",
            "--demuxer-lavf-probescore=25",
            "--video-sync=audio",
            "--hwdec=auto-safe",
            f"--volume={volume}",
            f"--volume-gain={boost_db:.1f}",
            url,
        ]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        log_path = Path(tempfile.gettempdir()) / "hdhomerun_tv_mpv.log"
        try:
            self._stderr_handle = open(log_path, "w", encoding="utf-8", errors="replace")
        except OSError:
            self._stderr_handle = subprocess.DEVNULL
        self.process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=self._stderr_handle,
            creationflags=flags,
        )
        self.current_url = url
        self.last_error = ""
        time.sleep(0.18)
        if self.process.poll() is not None:
            try:
                if hasattr(self._stderr_handle, "flush"):
                    self._stderr_handle.flush()
                self.last_error = log_path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                self.last_error = ""
            raise OSError(self.last_error or "The playback engine exited before the channel could open.")

    def command(self, command: list[Any]) -> None:
        if os.name != "nt" or not self.process or self.process.poll() is not None:
            return
        payload = (json.dumps({"command": command}) + "\n").encode("utf-8")
        with self._pipe_lock:
            for _ in range(8):
                try:
                    with open(self.pipe_name, "r+b", buffering=0) as pipe:
                        pipe.write(payload)
                    return
                except OSError:
                    time.sleep(0.08)

    def set_volume(self, value: int) -> None:
        threading.Thread(target=self.command, args=(["set_property", "volume", value],), daemon=True).start()

    def set_mute(self, muted: bool) -> None:
        threading.Thread(target=self.command, args=(["set_property", "mute", muted],), daemon=True).start()

    def set_boost(self, value: float) -> None:
        threading.Thread(target=self.command, args=(["set_property", "volume-gain", value],), daemon=True).start()

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        self.current_url = ""
        if self._stderr_handle not in (None, subprocess.DEVNULL):
            try:
                self._stderr_handle.close()
            except Exception:
                pass
        self._stderr_handle = None


class TVApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.settings = self.load_settings()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.overrideredirect(True)
        self.configure(bg="#030507")

        # Keep every control visible even when an older release saved a window
        # that was too small.  The limits adapt to low-resolution displays.
        screen_w = max(800, self.winfo_screenwidth())
        screen_h = max(600, self.winfo_screenheight())
        self._min_width = min(1120, screen_w)
        self._min_height = min(680, max(600, screen_h - 40))
        self.minsize(self._min_width, self._min_height)
        self.geometry(self._validated_geometry(self.settings.get("geometry", "1280x760")))
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        icon = resource_path("assets/hdhomerun_tv.ico")
        png_icon = resource_path("assets/hdhomerun_tv.png")
        if icon.exists():
            try:
                self.iconbitmap(default=str(icon))
            except tk.TclError:
                pass
        if png_icon.exists():
            try:
                self._taskbar_icon = tk.PhotoImage(file=str(png_icon))
                self.iconphoto(True, self._taskbar_icon)
            except tk.TclError:
                self._taskbar_icon = None

        self.devices: list[Device] = []
        self.channels: list[Channel] = []
        self.filtered_channels: list[Channel] = []
        self.player: MPVController | None = None
        self.volume = tk.IntVar(value=int(self.settings.get("volume", 70)))
        self.boost = tk.DoubleVar(value=float(self.settings.get("boost_db", 3.0)))
        self.search_var = tk.StringVar()
        self.muted = False
        self.powered = False
        self._drag_x = 0
        self._drag_y = 0
        self._resize_edge = ""
        self._resize_start = None
        self._maximized = False
        self._normal_geometry = self.geometry()
        self._osd_after: str | None = None
        self.programs: dict[str, list[Program]] = {}
        self.channel_images: dict[str, Any] = {}
        self.guide_visible = False
        self._fullscreen = False
        self.guide_resume_channel: Channel | None = None
        self.guide_was_playing = False
        self.about_resume_channel: Channel | None = None
        self.about_was_playing = False
        self.favorites_only = tk.BooleanVar(value=bool(self.settings.get("favorites_only", False)))
        self.favorite_numbers: set[str] = set(str(x) for x in self.settings.get("favorite_channels", []))

        self._build_styles()
        self._build_ui()
        self.after(250, self.initialize_player)
        self.after(200, self._force_taskbar_icon)
        self.after(700, self._force_taskbar_icon)
        self.after(500, self.discover_devices)

    def _validated_geometry(self, value: str) -> str:
        """Clamp saved geometry so the complete TV controls remain on screen."""
        import re
        match = re.match(r"^(\d+)x(\d+)([+-]\d+)([+-]\d+)$", str(value or ""))
        if match:
            width, height, x, y = map(int, match.groups())
        else:
            width, height, x, y = 1280, 760, 40, 40
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        width = max(self._min_width, min(width, screen_w))
        height = max(self._min_height, min(height, screen_h))
        x = max(0, min(x, max(0, screen_w - width)))
        y = max(0, min(y, max(0, screen_h - height)))
        return f"{width}x{height}+{x}+{y}"

    def _install_resize_grips(self) -> None:
        """Provide normal edge/corner resizing for the frameless window."""
        grips = {
            "n":  (0, 0, 1, 5, "sb_v_double_arrow"),
            "s":  (0, -5, 1, 5, "sb_v_double_arrow"),
            "w":  (0, 0, 5, 1, "sb_h_double_arrow"),
            "e":  (-5, 0, 5, 1, "sb_h_double_arrow"),
            "nw": (0, 0, 7, 7, "size_nw_se"),
            "ne": (-7, 0, 7, 7, "size_ne_sw"),
            "sw": (0, -7, 7, 7, "size_ne_sw"),
            "se": (-7, -7, 7, 7, "size_nw_se"),
        }
        for edge, (x, y, w, h, cursor) in grips.items():
            grip = tk.Frame(self, bg="#2b3036", cursor=cursor)
            kwargs = {"x": x, "y": y, "width": w, "height": h}
            if x < 0: kwargs["relx"] = 1.0
            if y < 0: kwargs["rely"] = 1.0
            if w == 1: kwargs["relwidth"] = 1.0
            if h == 1: kwargs["relheight"] = 1.0
            grip.place(**kwargs)
            grip.lift()
            grip.bind("<ButtonPress-1>", lambda e, side=edge: self._begin_resize(e, side))
            grip.bind("<B1-Motion>", self._perform_resize)

    def _begin_resize(self, event: tk.Event, edge: str) -> None:
        if self._maximized:
            return
        self._resize_edge = edge
        self._resize_start = (event.x_root, event.y_root, self.winfo_x(), self.winfo_y(), self.winfo_width(), self.winfo_height())

    def _perform_resize(self, event: tk.Event) -> None:
        if not self._resize_start or self._maximized:
            return
        sx, sy, x, y, width, height = self._resize_start
        dx, dy = event.x_root - sx, event.y_root - sy
        edge = self._resize_edge
        if "e" in edge: width += dx
        if "s" in edge: height += dy
        if "w" in edge:
            new_width = width - dx
            if new_width >= self._min_width: x += dx; width = new_width
        if "n" in edge:
            new_height = height - dy
            if new_height >= self._min_height: y += dy; height = new_height
        width = max(self._min_width, width)
        height = max(self._min_height, height)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _build_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "TV.TCombobox",
            fieldbackground=PANEL_2,
            background=PANEL_2,
            foreground=TEXT,
            arrowcolor=TEXT,
            bordercolor="#434b55",
            lightcolor="#434b55",
            darkcolor=PANEL_2,
            padding=7,
        )
        style.map(
            "TV.TCombobox",
            fieldbackground=[("readonly", PANEL_2)],
            foreground=[("readonly", TEXT)],
            selectbackground=[("readonly", PANEL_2)],
            selectforeground=[("readonly", TEXT)],
        )
        style.configure("TV.Vertical.TScrollbar", background="#343b44", troughcolor=PANEL, arrowcolor=TEXT)
        style.configure("Channels.Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        rowheight=42, borderwidth=0, font=("Segoe UI", 10))
        style.configure("Channels.Treeview.Heading", background=PANEL, foreground=MUTED, borderwidth=0,
                        font=("Segoe UI", 8, "bold"))
        style.map("Channels.Treeview", background=[("selected", BLUE_DARK)], foreground=[("selected", "white")])
        style.configure("Guide.Treeview", background="#0b0f14", fieldbackground="#0b0f14", foreground=TEXT,
                        rowheight=54, borderwidth=0, font=("Segoe UI", 9))
        style.configure("Guide.Treeview.Heading", background="#18212b", foreground=BLUE, borderwidth=0,
                        font=("Segoe UI", 9, "bold"))
        style.map("Guide.Treeview", background=[("selected", BLUE_DARK)], foreground=[("selected", "white")])
        self.option_add("*TCombobox*Listbox.background", PANEL_2)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", BLUE_DARK)
        self.option_add("*TCombobox*Listbox.selectForeground", "white")

    def _build_ui(self) -> None:
        outer = tk.Frame(self, bg="#2b3036", bd=2, relief="solid")
        outer.pack(fill="both", expand=True, padx=2, pady=2)
        self._install_resize_grips()

        titlebar = tk.Frame(outer, bg="#090c10", height=42)
        self.outer_frame = outer
        self.titlebar = titlebar
        titlebar.pack(fill="x")
        titlebar.pack_propagate(False)
        try:
            self._icon_img = tk.PhotoImage(file=str(resource_path("assets/hdhomerun_tv.png"))).subsample(8, 8)
            icon_label = tk.Label(titlebar, image=self._icon_img, bg="#090c10")
            icon_label.pack(side="left", padx=(16, 8))
        except Exception:
            icon_label = tk.Label(titlebar, text="▣", bg="#090c10", fg=BLUE, font=("Segoe UI", 18, "bold"))
            icon_label.pack(side="left", padx=(16, 8))
        title = tk.Label(titlebar, text=APP_NAME, bg="#090c10", fg=TEXT, font=("Segoe UI", 14), anchor="w")
        title.pack(side="left", fill="y")
        for widget in (titlebar, title, icon_label):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag_window)
            widget.bind("<Double-Button-1>", lambda _e: self.toggle_maximize())
        self._title_button(titlebar, "✕", self.on_close, hover="#8f1d24").pack(side="right", fill="y")
        self._title_button(titlebar, "□", self.toggle_maximize).pack(side="right", fill="y")
        self._title_button(titlebar, "—", self.minimize_window).pack(side="right", fill="y")

        body = tk.Frame(outer, bg=BG)
        body.pack(fill="both", expand=True)

        sidebar = tk.Frame(body, bg=PANEL, width=300)
        self.body_frame = body
        self.sidebar = sidebar
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        main = tk.Frame(body, bg=BEZEL)
        self.main_frame = main
        main.pack(side="left", fill="both", expand=True)

        tk.Label(sidebar, text="HDHOMERUN", bg=PANEL, fg=BLUE, font=("Segoe UI", 11, "bold"), anchor="w").pack(fill="x", padx=14, pady=(12, 6))
        self.device_combo = ttk.Combobox(sidebar, state="readonly", style="TV.TCombobox")
        self.device_combo.pack(fill="x", padx=14)
        self.device_combo.bind("<<ComboboxSelected>>", lambda _e: self.load_channels_for_selected_device())

        tk.Label(sidebar, text="CHANNEL", bg=PANEL, fg=BLUE, font=("Segoe UI", 11, "bold"), anchor="w").pack(fill="x", padx=14, pady=(12, 6))
        search_box = tk.Frame(sidebar, bg=PANEL_2, highlightbackground="#424a55", highlightthickness=1)
        search_box.pack(fill="x", padx=14)
        search_entry = tk.Entry(search_box, textvariable=self.search_var, bg=PANEL_2, fg=TEXT, insertbackground=TEXT, bd=0, font=("Segoe UI", 10))
        search_entry.pack(side="left", fill="x", expand=True, padx=10, pady=9)
        tk.Label(search_box, text="⌕", bg=PANEL_2, fg=TEXT, font=("Segoe UI", 15)).pack(side="right", padx=9)
        self.search_var.trace_add("write", lambda *_: self.filter_channels())

        list_outer = tk.Frame(sidebar, bg=PANEL, bd=0)
        list_outer.pack(fill="both", expand=True, padx=10, pady=(8, 3))
        scrollbar = ttk.Scrollbar(list_outer, orient="vertical", style="TV.Vertical.TScrollbar")
        scrollbar.pack(side="right", fill="y")
        self.channel_list = ttk.Treeview(
            list_outer, columns=("number", "name"), show="tree headings",
            selectmode="browse", style="Channels.Treeview", yscrollcommand=scrollbar.set,
        )
        self.channel_list.heading("#0", text="")
        self.channel_list.heading("number", text="CH")
        self.channel_list.heading("name", text="STATION")
        self.channel_list.column("#0", width=42, minwidth=42, stretch=False, anchor="center")
        self.channel_list.column("number", width=52, minwidth=48, stretch=False, anchor="center")
        self.channel_list.column("name", width=170, anchor="w")
        self.channel_list.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.channel_list.yview)
        self.channel_list.bind("<Double-Button-1>", lambda _e: self.play_selected())
        self.channel_list.bind("<Return>", lambda _e: self.play_selected())
        self.channel_list.bind("<ButtonRelease-1>", self._channel_clicked)
        self.channel_list.bind("<Button-3>", self._show_channel_menu)
        self.channel_list.bind("<Key-f>", lambda _e: self.toggle_selected_favorite())
        self.channel_list.bind("<Key-F>", lambda _e: self.toggle_selected_favorite())
        self.channel_menu = tk.Menu(self, tearoff=False, bg=PANEL_2, fg=TEXT,
                                    activebackground=BLUE_DARK, activeforeground="white")
        self.channel_menu.add_command(label="Add/remove favorite", command=self.toggle_selected_favorite)

        self._slider_group(sidebar, "AUDIO BOOST", self.boost, 0, 6, self.on_boost, lambda: f"+{self.boost.get():.0f} dB")
        self._slider_group(sidebar, "VOLUME", self.volume, 0, 100, self.on_volume, lambda: f"{self.volume.get()}%")

        bottom_side = tk.Frame(sidebar, bg=PANEL)
        bottom_side.pack(fill="x", padx=10, pady=(2, 8))
        self._small_button(bottom_side, "▦  GUIDE", self.show_guide).pack(side="left", padx=3)
        self.favorites_button = self._small_button(bottom_side, "★  FAVORITES", self.toggle_favorites_view)
        self.favorites_button.pack(side="left", padx=3)
        self._small_button(bottom_side, "⚙  ABOUT", self.show_about).pack(side="right", padx=3)

        self.screen_frame = tk.Frame(main, bg="black", bd=4, relief="sunken")
        self.screen_frame.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        self.screen_frame.update_idletasks()
        self.screen_message = tk.Label(self.screen_frame, text="SEARCHING FOR HDHOMERUN…", bg="black", fg="#b6c2cf", font=("Segoe UI", 18, "bold"))
        self.screen_message.place(relx=0.5, rely=0.5, anchor="center")

        self.osd = tk.Frame(self.screen_frame, bg="#090b0d", bd=0)
        self.osd_channel = tk.Label(self.osd, text="--", bg="#090b0d", fg="white", font=("Segoe UI", 24, "bold"))
        self.osd_channel.pack(side="left", padx=(20, 16), pady=14)
        osd_text = tk.Frame(self.osd, bg="#090b0d")
        osd_text.pack(side="left", fill="both", expand=True, pady=10)
        self.osd_name = tk.Label(osd_text, text="", bg="#090b0d", fg="white", font=("Segoe UI", 18, "bold"), anchor="w")
        self.osd_name.pack(fill="x")
        self.osd_detail = tk.Label(osd_text, text="LIVE  •  HDHomeRun network stream", bg="#090b0d", fg=BLUE, font=("Segoe UI", 10), anchor="w")
        self.osd_detail.pack(fill="x", pady=(4, 0))
        self.osd_clock = tk.Label(self.osd, text="", bg="#090b0d", fg="white", font=("Segoe UI", 16, "bold"), justify="right")
        self.osd_clock.pack(side="right", padx=22)

        # Integrated TV guide. It is placed over the picture, never in a separate window.
        self.guide_panel = tk.Frame(self.screen_frame, bg="#0b0f14", highlightbackground="#39424d", highlightthickness=1)
        guide_header = tk.Frame(self.guide_panel, bg="#111820", height=48)
        guide_header.pack(fill="x"); guide_header.pack_propagate(False)
        tk.Label(guide_header, text="LIVE TV GUIDE", bg="#111820", fg=BLUE,
                 font=("Segoe UI", 13, "bold"), anchor="w").pack(side="left", padx=16, fill="y")
        self.guide_date_label = tk.Label(guide_header, text="", bg="#111820", fg=MUTED, font=("Segoe UI", 9))
        self.guide_date_label.pack(side="left", padx=12)
        tk.Button(guide_header, text="✕", command=self.hide_guide, bg="#111820", fg=TEXT,
                  activebackground="#8f1d24", activeforeground="white", bd=0, width=5,
                  font=("Segoe UI", 11)).pack(side="right", fill="y")
        guide_body = tk.Frame(self.guide_panel, bg="#0b0f14")
        guide_body.pack(fill="both", expand=True, padx=12, pady=12)
        self.guide_tree = ttk.Treeview(guide_body, columns=("channel", "now", "next"), show="headings",
                                       selectmode="browse", style="Guide.Treeview")
        self.guide_tree.heading("channel", text="CHANNEL")
        self.guide_tree.heading("now", text="NOW")
        self.guide_tree.heading("next", text="NEXT")
        self.guide_tree.column("channel", width=170, minwidth=140, stretch=False)
        self.guide_tree.column("now", width=360, minwidth=240)
        self.guide_tree.column("next", width=360, minwidth=240)
        guide_scroll = ttk.Scrollbar(guide_body, orient="vertical", command=self.guide_tree.yview, style="TV.Vertical.TScrollbar")
        self.guide_tree.configure(yscrollcommand=guide_scroll.set)
        self.guide_tree.pack(side="left", fill="both", expand=True)
        guide_scroll.pack(side="right", fill="y")
        self.guide_tree.bind("<Double-Button-1>", self._guide_tune)
        self.guide_tree.bind("<Return>", self._guide_tune)
        self.guide_detail = tk.Label(self.guide_panel, text="", bg="#111820", fg=TEXT,
                                     font=("Segoe UI", 9), anchor="w", justify="left", wraplength=700)
        self.guide_detail.pack(fill="x", padx=12, pady=(0, 12), ipady=8)
        self.guide_tree.bind("<<TreeviewSelect>>", self._guide_selection_changed)

        # Dark, in-screen About panel matching the television interface.
        self.about_panel = tk.Frame(self.screen_frame, bg="#0b0f14", highlightbackground="#39424d", highlightthickness=1)
        about_header = tk.Frame(self.about_panel, bg="#111820", height=48)
        about_header.pack(fill="x")
        about_header.pack_propagate(False)
        tk.Label(about_header, text="ABOUT", bg="#111820", fg=BLUE,
                 font=("Segoe UI", 13, "bold"), anchor="w").pack(side="left", padx=16, fill="y")
        tk.Button(about_header, text="✕", command=self.hide_about, bg="#111820", fg=TEXT,
                  activebackground="#8f1d24", activeforeground="white", bd=0, width=5,
                  font=("Segoe UI", 11)).pack(side="right", fill="y")
        about_body = tk.Frame(self.about_panel, bg="#0b0f14")
        about_body.pack(fill="both", expand=True, padx=28, pady=24)
        try:
            self._about_icon = tk.PhotoImage(file=str(resource_path("assets/hdhomerun_tv.png"))).subsample(3, 3)
            tk.Label(about_body, image=self._about_icon, bg="#0b0f14").pack(pady=(4, 12))
        except Exception:
            pass
        tk.Label(about_body, text=APP_NAME, bg="#0b0f14", fg=TEXT,
                 font=("Segoe UI", 20, "bold")).pack()
        tk.Label(about_body, text=f"Version {APP_VERSION}", bg="#0b0f14", fg=BLUE,
                 font=("Segoe UI", 11, "bold")).pack(pady=(4, 14))
        tk.Label(about_body,
                 text="A television-style Windows player for HDHomeRun network tuners.\nLive TV, integrated guide, favorites, and fullscreen viewing.",
                 bg="#0b0f14", fg=MUTED, font=("Segoe UI", 10), justify="center").pack()
        tk.Label(about_body, text="Created by Eduardo A. de Carvalho", bg="#0b0f14", fg=TEXT,
                 font=("Segoe UI", 9)).pack(pady=(22, 0))

        brand = tk.Label(main, text="HDHomeRun TV", bg=BEZEL, fg="#8c949d", font=("Segoe UI", 12, "bold"))
        self.brand_label = brand
        brand.pack(fill="x", pady=(5, 4))

        control_bar = tk.Frame(main, bg="#111419", height=92)
        self.control_bar = control_bar
        control_bar.pack(fill="x", padx=8, pady=(0, 8))
        control_bar.pack_propagate(False)

        power_area = tk.Frame(control_bar, bg="#111419")
        power_area.pack(side="left", padx=12, pady=8)
        tk.Label(power_area, text="POWER", bg="#111419", fg=MUTED, font=("Segoe UI", 8)).pack(anchor="w")
        row = tk.Frame(power_area, bg="#111419")
        row.pack()
        self.power_button = self._tv_button(row, "⏻", self.toggle_power, 4, 2, font=("Segoe UI Symbol", 19))
        self.power_button.pack(side="left")
        self.power_led = tk.Canvas(row, width=16, height=16, bg="#111419", highlightthickness=0)
        self.power_led.pack(side="left", padx=8)
        self.led_dot = self.power_led.create_oval(5, 5, 11, 11, fill=RED, outline="#7b0000")

        self._tv_button(control_bar, "🔇\nMUTE", self.toggle_mute, 7, 2).pack(side="left", padx=4)
        self._tv_button(control_bar, "◀ CH", self.channel_up, 7, 2).pack(side="left", padx=2)
        center = tk.Frame(control_bar, bg="#111419")
        center.pack(side="left", padx=2)
        self._tv_button(center, "▲", self.channel_up, 7, 1).pack()
        self._tv_button(center, "OK", self.play_selected, 7, 1).pack()
        self._tv_button(center, "▼", self.channel_down, 7, 1).pack()
        self._tv_button(control_bar, "CH ▶", self.channel_down, 7, 2).pack(side="left", padx=2)
        self._tv_button(control_bar, "↶\nBACK", self.stop_playback, 7, 2).pack(side="left", padx=6)
        self._tv_button(control_bar, "⛶\nFULL", self.toggle_fullscreen, 8, 2).pack(side="left", padx=2)

        self.status_label = tk.Label(control_bar, text="READY", bg="#111419", fg=MUTED, font=("Segoe UI", 9), anchor="e")
        self.status_label.pack(side="right", fill="y", padx=8)

        self.bind("<Up>", lambda _e: self.channel_up())
        self.bind("<Down>", lambda _e: self.channel_down())
        self.bind("<Return>", lambda _e: self.play_selected())
        self.bind("<F11>", lambda _e: self.toggle_fullscreen())
        self.bind("<Escape>", lambda _e: self.exit_fullscreen())
        self.after(1000, self.update_clock)

    def _title_button(self, parent: tk.Widget, text: str, command, hover: str = "#292f36") -> tk.Button:
        return tk.Button(parent, text=text, command=command, bg="#090c10", fg=TEXT, activebackground=hover, activeforeground="white", bd=0, width=5, font=("Segoe UI", 11))

    def _small_button(self, parent: tk.Widget, text: str, command) -> tk.Button:
        return tk.Button(parent, text=text, command=command, bg=PANEL_2, fg=TEXT, activebackground="#252c34", activeforeground="white", bd=1, relief="solid", padx=7, pady=7, font=("Segoe UI", 8))

    def _tv_button(self, parent: tk.Widget, text: str, command, width: int, height: int, font=("Segoe UI", 9)) -> tk.Button:
        return tk.Button(parent, text=text, command=command, width=width, height=height, bg="#171b20", fg=TEXT, activebackground="#272e36", activeforeground="white", relief="raised", bd=2, font=font)

    def _slider_group(self, parent, title, variable, frm, to, command, value_text) -> None:
        block = tk.Frame(parent, bg=PANEL)
        block.pack(fill="x", padx=14, pady=(2, 1))
        tk.Label(block, text=title, bg=PANEL, fg=BLUE, font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x")
        row = tk.Frame(block, bg=PANEL)
        row.pack(fill="x")
        tk.Label(row, text="🔊", bg=PANEL, fg=TEXT, font=("Segoe UI Emoji", 10)).pack(side="left")
        scale = tk.Scale(row, from_=frm, to=to, resolution=1, orient="horizontal", variable=variable, bg=PANEL, troughcolor="#2a3037", activebackground=BLUE, highlightthickness=0, showvalue=False, command=command)
        scale.pack(side="left", fill="x", expand=True, padx=5)
        label = tk.Label(row, text=value_text(), bg=PANEL, fg=TEXT, font=("Segoe UI", 9), width=6, anchor="e")
        label.pack(side="right")
        if variable is self.boost:
            self.boost_value_label = label
        else:
            self.volume_value_label = label

    def initialize_player(self) -> None:
        self.screen_frame.update_idletasks()
        self.player = MPVController(self.screen_frame.winfo_id())

    def update_clock(self) -> None:
        now = time.strftime("%I:%M %p").lstrip("0")
        date = time.strftime("%b %d, %Y")
        if hasattr(self, "osd_clock"):
            self.osd_clock.config(text=f"{now}\n{date}")
        self.after(1000, self.update_clock)

    def status(self, text: str) -> None:
        self.status_label.config(text=text.upper())

    def discover_devices(self) -> None:
        self.status("Scanning network")
        threading.Thread(target=self._discover_worker, daemon=True).start()

    def _discover_worker(self) -> None:
        discovered: dict[str, Device] = {}
        message = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\nST: " + SSDP_ST + "\r\n\r\n").encode("ascii")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.settimeout(0.5)
        try:
            sock.sendto(message, SSDP_ADDR)
            end = time.time() + 2.5
            while time.time() < end:
                try:
                    data, address = sock.recvfrom(65535)
                    text = data.decode("utf-8", errors="ignore").lower()
                    if "hdhomerun" in text or "silicondust" in text:
                        dev = self._device_from_ip(address[0])
                        if dev:
                            discovered[dev.device_id] = dev
                except socket.timeout:
                    continue
        finally:
            sock.close()
        try:
            for item in get_json("https://ipv4-api.hdhomerun.com/discover", timeout=4):
                base = str(item.get("BaseURL", "")).rstrip("/")
                if base:
                    ip = base.split("//", 1)[-1].split(":", 1)[0]
                    dev = self._device_from_info(item, ip)
                    discovered[dev.device_id or dev.ip] = dev
        except Exception:
            pass
        self.after(0, lambda: self._apply_devices(list(discovered.values())))

    def _device_from_ip(self, ip: str) -> Device | None:
        try:
            return self._device_from_info(get_json(f"http://{ip}/discover.json", timeout=1.5), ip)
        except Exception:
            return None

    def _device_from_info(self, info: dict[str, Any], ip: str) -> Device:
        base = str(info.get("BaseURL") or f"http://{ip}").rstrip("/")
        lineup = str(info.get("LineupURL") or f"{base}/lineup.json")
        device_id = str(info.get("DeviceID") or ip)
        model = str(info.get("ModelNumber") or info.get("FriendlyName") or "HDHomeRun")
        return Device(f"{model}  •  {device_id}", device_id, ip, base, lineup, model, str(info.get("DeviceAuth") or ""))

    def _apply_devices(self, devices: list[Device]) -> None:
        self.devices = sorted(devices, key=lambda d: d.name.lower())
        self.device_combo["values"] = [d.name for d in self.devices]
        if not self.devices:
            self.status("No HDHomeRun found")
            self.screen_message.config(text="NO HDHOMERUN FOUND\nCHECK NETWORK AND RESCAN")
            return
        preferred = self.settings.get("device_id", "")
        index = next((i for i, d in enumerate(self.devices) if d.device_id == preferred), 0)
        self.device_combo.current(index)
        self.status(f"Found {len(self.devices)} device(s)")
        self.load_channels_for_selected_device()

    def load_channels_for_selected_device(self) -> None:
        idx = self.device_combo.current()
        if 0 <= idx < len(self.devices):
            device = self.devices[idx]
            self.status(f"Loading channels from {device.ip}")
            threading.Thread(target=self._lineup_worker, args=(device,), daemon=True).start()

    def _lineup_worker(self, device: Device) -> None:
        try:
            data = get_json(device.lineup_url, timeout=6)
            channels = []
            for item in data if isinstance(data, list) else []:
                url = str(item.get("URL", "")); number = str(item.get("GuideNumber", "")); name = str(item.get("GuideName", "Unknown Channel"))
                if url and number:
                    channels.append(Channel(number, name, url, bool(item.get("Favorite")), bool(item.get("DRM"))))
            channels.sort(key=self._channel_sort_key)
            self.after(0, lambda: self._apply_channels(device, channels))
        except Exception as exc:
            self.after(0, lambda: self._lineup_error(str(exc)))

    @staticmethod
    def _channel_sort_key(channel: Channel):
        try:
            return tuple(int(p) for p in channel.guide_number.replace("-", ".").split("."))
        except ValueError:
            return (999999, channel.guide_number)

    def _apply_channels(self, device: Device, channels: list[Channel]) -> None:
        self.channels = channels
        for channel in self.channels:
            if channel.favorite:
                self.favorite_numbers.add(channel.guide_number)
            channel.favorite = channel.guide_number in self.favorite_numbers
        self.settings["device_id"] = device.device_id
        self.filter_channels()
        if channels:
            self.screen_message.config(text="SELECT A CHANNEL")
            self.status(f"{len(channels)} channels loaded")
            last = self.settings.get("last_channel", "")
            idx = next((i for i, c in enumerate(self.filtered_channels) if c.guide_number == last), 0)
            item = str(idx)
            self.channel_list.selection_set(item); self.channel_list.focus(item); self.channel_list.see(item)
            self.load_guide_data(device)
        else:
            self.screen_message.config(text="NO CHANNELS IN LINEUP")
            self.status("Channel scan may be required")

    def _lineup_error(self, error: str) -> None:
        self.status("Unable to load lineup")
        self.screen_message.config(text="CHANNEL LINEUP ERROR")
        messagebox.showerror(APP_NAME, f"Could not read the HDHomeRun channel lineup.\n\n{error}", parent=self)

    def _badge_image(self, channel: Channel):
        key = channel.guide_number
        if key in self.channel_images:
            return self.channel_images[key]
        if Image is None or ImageTk is None:
            self.channel_images[key] = ""
            return ""
        img = Image.new("RGBA", (34, 28), "#173a68")
        draw = ImageDraw.Draw(img)
        text = channel.station_badge[:4]
        try:
            font = ImageFont.truetype("arialbd.ttf", 10)
        except Exception:
            font = ImageFont.load_default()
        box = draw.textbbox((0, 0), text, font=font)
        draw.text(((34-(box[2]-box[0]))/2, (28-(box[3]-box[1]))/2-1), text, fill="white", font=font)
        photo = ImageTk.PhotoImage(img)
        self.channel_images[key] = photo
        return photo

    def filter_channels(self) -> None:
        query = self.search_var.get().strip().lower()
        self.filtered_channels = [
            c for c in self.channels
            if (not self.favorites_only.get() or c.favorite)
            and (not query or query in c.guide_number.lower() or query in c.guide_name.lower())
        ]
        for item in self.channel_list.get_children():
            self.channel_list.delete(item)
        for index, channel in enumerate(self.filtered_channels):
            marks = ("  ★" if channel.favorite else "") + ("  🔒" if channel.drm else "")
            self.channel_list.insert("", "end", iid=str(index), image=self._badge_image(channel),
                                     values=(channel.guide_number, channel.guide_name + marks))
        if hasattr(self, "favorites_button"):
            self.favorites_button.config(text="★  ALL CHANNELS" if self.favorites_only.get() else "★  FAVORITES")
        if self.filtered_channels:
            self.channel_list.selection_set("0"); self.channel_list.focus("0")
        elif self.favorites_only.get():
            self.status("No favorite channels selected")

    def toggle_favorites_view(self) -> None:
        self.favorites_only.set(not self.favorites_only.get())
        self.filter_channels()
        self.status("Favorite channels" if self.favorites_only.get() else "All channels")

    def toggle_selected_favorite(self) -> None:
        channel = self.selected_channel()
        if not channel:
            return
        channel.favorite = not channel.favorite
        if channel.favorite:
            self.favorite_numbers.add(channel.guide_number)
            self.status(f"Added {channel.guide_number} to favorites")
        else:
            self.favorite_numbers.discard(channel.guide_number)
            self.status(f"Removed {channel.guide_number} from favorites")
        self.settings["favorite_channels"] = sorted(self.favorite_numbers)
        self.filter_channels()

    def _show_channel_menu(self, event: tk.Event) -> None:
        item = self.channel_list.identify_row(event.y)
        if not item:
            return
        self.channel_list.selection_set(item)
        self.channel_list.focus(item)
        channel = self.selected_channel()
        if channel:
            self.channel_menu.entryconfig(0, label="Remove from favorites" if channel.favorite else "Add to favorites")
            self.channel_menu.tk_popup(event.x_root, event.y_root)

    def _channel_clicked(self, event: tk.Event) -> None:
        item = self.channel_list.identify_row(event.y)
        if item:
            self.channel_list.selection_set(item); self.channel_list.focus(item)
            self.after(40, self.play_selected)

    def selected_channel(self) -> Channel | None:
        selection = self.channel_list.selection()
        if not selection:
            return None
        try:
            return self.filtered_channels[int(selection[0])]
        except (ValueError, IndexError):
            return None

    def play_selected(self) -> None:
        channel = self.selected_channel()
        if not channel or not self.player:
            return
        if channel.drm:
            open_official = messagebox.askyesno(
                APP_NAME,
                "This channel is DRM-protected. DRM channels require the official HDHomeRun app and cannot be decoded by the bundled mpv engine.\n\nOpen the official HDHomeRun app page?",
                parent=self,
            )
            if open_official:
                try:
                    os.startfile("ms-windows-store://pdp/?ProductId=9NBLGGH58VWK")
                except Exception:
                    webbrowser.open("https://apps.microsoft.com/detail/9nblggh58vwk")
            self.status("DRM channel requires official HDHomeRun app")
            return
        try:
            self.screen_message.place_forget()
            self.player.play(channel.url, 0 if self.muted else self.volume.get(), self.boost.get())
            self.powered = True
            self._update_power_led()
            self.settings["last_channel"] = channel.guide_number
            self.status(f"Playing {channel.guide_number} — {channel.guide_name}")
            self.show_osd(channel)
        except FileNotFoundError:
            self.screen_message.place(relx=0.5, rely=0.5, anchor="center")
            self.screen_message.config(text="INSTALLING PLAYBACK ENGINE…")
            self.status("Downloading playback engine")
            threading.Thread(target=self._install_and_play, args=(channel,), daemon=True).start()
        except OSError as exc:
            self.powered = False
            self._update_power_led()
            self.screen_message.config(text="CHANNEL COULD NOT BE OPENED")
            self.screen_message.place(relx=0.5, rely=0.5, anchor="center")
            detail = str(exc).strip()
            messagebox.showerror(APP_NAME, f"Unable to start this channel.\n\n{detail}", parent=self)

    def _install_and_play(self, channel: Channel) -> None:
        try:
            MPVController.install_engine()
            self.after(0, self.play_selected)
        except Exception as exc:
            self.after(0, lambda: self._engine_install_failed(str(exc)))

    def _engine_install_failed(self, error: str) -> None:
        self.screen_message.place(relx=0.5, rely=0.5, anchor="center")
        self.screen_message.config(text="PLAYBACK ENGINE INSTALL FAILED")
        self.status("Playback engine installation failed")
        messagebox.showerror(APP_NAME, f"The playback engine could not be installed automatically.\n\n{error}", parent=self)

    def show_osd(self, channel: Channel) -> None:
        self.osd_channel.config(text=channel.guide_number)
        self.osd_name.config(text=channel.guide_name)
        self.osd.place(relx=0.02, rely=0.77, relwidth=0.96, height=112)
        if self._osd_after:
            self.after_cancel(self._osd_after)
        self._osd_after = self.after(4500, self.osd.place_forget)

    def channel_up(self) -> None:
        self._move_channel(-1)

    def channel_down(self) -> None:
        self._move_channel(1)

    def _move_channel(self, delta: int) -> None:
        if not self.filtered_channels:
            return
        current = self.channel_list.selection()
        idx = ((int(current[0]) if current else 0) + delta) % len(self.filtered_channels)
        item = str(idx)
        self.channel_list.selection_set(item); self.channel_list.focus(item); self.channel_list.see(item)
        self.play_selected()

    def toggle_power(self) -> None:
        if self.powered:
            self.stop_playback()
        else:
            self.play_selected()

    def stop_playback(self) -> None:
        if self.player:
            self.player.stop()
        self.powered = False
        self._update_power_led()
        self.screen_message.config(text="POWER OFF")
        self.screen_message.place(relx=0.5, rely=0.5, anchor="center")
        self.osd.place_forget()
        self.hide_guide(resume=False)
        self.hide_about(resume=False)
        self.status("Power off")

    def _update_power_led(self) -> None:
        self.power_led.itemconfig(self.led_dot, fill="#451010" if self.powered else RED, outline="#250000" if self.powered else "#8d0000")

    def toggle_mute(self) -> None:
        self.muted = not self.muted
        if self.player:
            self.player.set_mute(self.muted)
        self.status("Muted" if self.muted else f"Volume {self.volume.get()}%")

    def on_volume(self, _value: str) -> None:
        self.muted = False
        self.volume_value_label.config(text=f"{self.volume.get()}%")
        if self.player:
            self.player.set_mute(False)
            self.player.set_volume(self.volume.get())
        self.status(f"Volume {self.volume.get()}%")

    def on_boost(self, _value: str) -> None:
        value = float(self.boost.get())
        self.boost_value_label.config(text=f"+{value:.0f} dB")
        if self.player:
            self.player.set_boost(value)
        self.status(f"Audio boost +{value:.0f} dB")

    def toggle_fullscreen(self) -> None:
        """Toggle a true screen-only viewing mode."""
        if self._fullscreen:
            self.exit_fullscreen()
            return
        self.hide_about()
        if self.guide_visible:
            self.hide_guide(resume=True)
        self._fullscreen = True
        self.titlebar.pack_forget()
        self.sidebar.pack_forget()
        self.brand_label.pack_forget()
        self.control_bar.pack_forget()
        self.outer_frame.configure(bd=0)
        self.screen_frame.pack_configure(padx=0, pady=0)
        self.attributes("-fullscreen", True)
        self.screen_frame.focus_set()
        self.status("Fullscreen — press Esc to exit")

    def exit_fullscreen(self) -> None:
        if not self._fullscreen and not bool(self.attributes("-fullscreen")):
            return
        self.attributes("-fullscreen", False)
        self._fullscreen = False
        self.titlebar.pack(fill="x", before=self.body_frame)
        self.sidebar.pack(side="left", fill="y", before=self.main_frame)
        self.screen_frame.pack_configure(padx=8, pady=(8, 0))
        self.brand_label.pack(fill="x", pady=(5, 4), after=self.screen_frame)
        self.control_bar.pack(fill="x", padx=8, pady=(0, 8), after=self.brand_label)
        self.outer_frame.configure(bd=2)
        self.after(80, self._force_taskbar_icon)
        self.status("Ready")

    def load_guide_data(self, device: Device) -> None:
        self.status("Loading program guide")
        threading.Thread(target=self._guide_worker, args=(device,), daemon=True).start()

    def _refresh_device_auth(self, device: Device) -> str:
        """Refresh DeviceAuth because SiliconDust periodically rotates it."""
        try:
            info = get_json(f"{device.base_url}/discover.json", timeout=4)
            auth = str(info.get("DeviceAuth") or "")
            if auth:
                device.device_auth = auth
        except Exception:
            pass
        return device.device_auth

    def _guide_worker(self, device: Device) -> None:
        try:
            auth = self._refresh_device_auth(device)
            if not auth:
                raise RuntimeError("The tuner did not provide a DeviceAuth token.")

            # The JSON guide endpoint is the same limited guide service used by
            # HDHomeRun clients and works without requiring XMLTV access.
            endpoint = f"https://api.hdhomerun.com/api/guide?DeviceAuth={urllib.parse.quote(auth)}&Duration=24&SynopsisLength=200"
            app_data = urllib.parse.urlencode({
                "AppName": "HDHomeRun",
                "AppVersion": "20241007",
                "DeviceAuth": auth,
                "Platform": "WINDOWS",
            }).encode("utf-8")
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) HDHomeRunTV/1.0.6",
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "Content-Type": "application/x-www-form-urlencoded",
                "Cache-Control": "no-cache",
            }
            raw = b""
            last_error = None
            # POST is preferred; GET is retained as a compatibility fallback.
            for data in (app_data, None):
                try:
                    req = urllib.request.Request(endpoint, data=data, headers=headers, method="POST" if data is not None else "GET")
                    with urllib.request.urlopen(req, timeout=30) as response:
                        raw = response.read()
                        if response.headers.get("Content-Encoding", "").lower() == "gzip" or raw[:2] == b"\x1f\x8b":
                            raw = gzip.decompress(raw)
                    break
                except Exception as exc:
                    last_error = exc
            if not raw:
                raise RuntimeError(f"Guide service did not return data: {last_error}")

            payload = json.loads(raw.decode("utf-8", errors="replace"))
            if not isinstance(payload, list):
                raise RuntimeError("Unexpected guide response format.")

            programs: dict[str, list[Program]] = {}
            logos: dict[str, str] = {}
            names: dict[str, str] = {}
            for channel_data in payload:
                if not isinstance(channel_data, dict):
                    continue
                number = str(channel_data.get("GuideNumber") or "").strip()
                if not number:
                    continue
                names[number] = str(channel_data.get("GuideName") or channel_data.get("Affiliate") or "")
                logo = str(channel_data.get("ImageURL") or "")
                if logo:
                    logos[number] = logo
                for item in channel_data.get("Guide") or []:
                    if not isinstance(item, dict):
                        continue
                    start_time = int(item.get("StartTime") or 0)
                    end_time = int(item.get("EndTime") or 0)
                    if not start_time or not end_time:
                        continue
                    title = str(item.get("Title") or "Untitled")
                    synopsis = str(item.get("Synopsis") or "")
                    episode = str(item.get("EpisodeTitle") or item.get("EpisodeNumber") or "")
                    programs.setdefault(number, []).append(Program(number, title, start_time, end_time, synopsis, episode))
            for plist in programs.values():
                plist.sort(key=lambda p: p.start)
            if not programs:
                raise RuntimeError("The guide service returned channels but no program listings.")
            self.after(0, lambda: self._apply_guide(programs, logos, names))
        except Exception as exc:
            self.after(0, lambda: self._guide_error(str(exc)))

    @staticmethod
    def _xmltv_timestamp(value: str) -> int:
        import datetime as dt
        value = value.strip()
        for fmt in ("%Y%m%d%H%M%S %z", "%Y%m%d%H%M%S", "%Y%m%d%H%M %z", "%Y%m%d%H%M"):
            try:
                return int(dt.datetime.strptime(value, fmt).timestamp())
            except ValueError:
                continue
        return 0

    def _apply_guide(self, programs: dict[str, list[Program]], logos: dict[str, str], names: dict[str, str]) -> None:
        self.programs = programs
        # XMLTV supplies real station artwork on many lineups. Load it asynchronously;
        # generated TV badges remain as a clean fallback.
        for channel in self.channels:
            if channel.guide_number in logos:
                channel.logo_url = logos[channel.guide_number]
        if Image is not None and ImageTk is not None:
            threading.Thread(target=self._logo_worker, daemon=True).start()
        self.status("Program guide ready")
        if self.guide_visible:
            self._populate_guide()


    def _logo_worker(self) -> None:
        loaded: dict[str, Any] = {}
        for channel in self.channels:
            if not channel.logo_url:
                continue
            try:
                req = urllib.request.Request(channel.logo_url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
                with urllib.request.urlopen(req, timeout=6) as response:
                    raw = response.read(512_000)
                image = Image.open(io.BytesIO(raw)).convert("RGBA")
                image.thumbnail((36, 30), Image.Resampling.LANCZOS)
                canvas = Image.new("RGBA", (38, 32), (0, 0, 0, 0))
                canvas.alpha_composite(image, ((38-image.width)//2, (32-image.height)//2))
                loaded[channel.guide_number] = canvas
            except Exception:
                continue
        if loaded:
            self.after(0, lambda: self._apply_logo_images(loaded))

    def _apply_logo_images(self, loaded: dict[str, Any]) -> None:
        for number, image in loaded.items():
            self.channel_images[number] = ImageTk.PhotoImage(image)
        # Recreate rows so Treeview displays newly downloaded station artwork.
        selected = self.selected_channel()
        self.filter_channels()
        if selected in self.filtered_channels:
            item = str(self.filtered_channels.index(selected))
            self.channel_list.selection_set(item); self.channel_list.focus(item); self.channel_list.see(item)

    def _guide_error(self, error: str) -> None:
        self.status("Guide unavailable")
        self.programs = {}
        self.guide_error_text = error
        if self.guide_visible:
            self._populate_guide(message=f"Program listings could not be loaded. {error}")

    def _current_next(self, number: str) -> tuple[Program | None, Program | None]:
        now = int(time.time())
        plist = self.programs.get(number, [])
        current = next((p for p in plist if p.start <= now < p.end), None)
        upcoming = [p for p in plist if p.start >= (current.end if current else now)]
        return current, (upcoming[0] if upcoming else None)

    def _populate_guide(self, message: str = "") -> None:
        for item in self.guide_tree.get_children():
            self.guide_tree.delete(item)
        for index, channel in enumerate(self.channels):
            current, nxt = self._current_next(channel.guide_number)
            now_text = f"{current.time_text}  {current.title}" if current else "No current listing"
            next_text = f"{nxt.time_text}  {nxt.title}" if nxt else "No upcoming listing"
            self.guide_tree.insert("", "end", iid=str(index), values=(
                f"{channel.guide_number}  {channel.guide_name}", now_text, next_text))
        self.guide_date_label.config(text=time.strftime("%A, %B %d"))
        self.guide_detail.config(text=message or "Select a program row for details. Double-click or press Enter to watch the channel.")
        if self.channels:
            self.guide_tree.selection_set("0"); self.guide_tree.focus("0")

    def show_guide(self) -> None:
        self.hide_about(resume=False)
        if self.guide_visible:
            self.hide_guide(); return
        # mpv is a native child window and otherwise paints above Tk widgets.
        # Temporarily stop the stream so the in-screen guide is always visible,
        # then restore the channel when the guide closes.
        self.guide_resume_channel = self.selected_channel()
        self.guide_was_playing = bool(self.powered and self.player and self.player.process and self.player.process.poll() is None)
        if self.guide_was_playing and self.player:
            self.player.stop()
        self.osd.place_forget()
        self.screen_message.place_forget()
        self.guide_visible = True
        self._populate_guide("Loading program listings…" if not self.programs else "")
        self.guide_panel.place(relx=0.025, rely=0.04, relwidth=0.95, relheight=0.90)
        self.guide_panel.lift()
        self.guide_tree.focus_set()
        idx = self.device_combo.current()
        if not self.programs and 0 <= idx < len(self.devices):
            self.load_guide_data(self.devices[idx])

    def hide_guide(self, resume: bool = True) -> None:
        was_playing = self.guide_was_playing
        resume_channel = self.guide_resume_channel
        self.guide_visible = False
        self._fullscreen = False
        self.guide_panel.place_forget()
        self.guide_was_playing = False
        self.guide_resume_channel = None
        if resume and was_playing and resume_channel and not resume_channel.drm:
            try:
                idx = self.filtered_channels.index(resume_channel)
            except ValueError:
                self.search_var.set(""); self.filter_channels(); idx = self.filtered_channels.index(resume_channel)
            item = str(idx)
            self.channel_list.selection_set(item); self.channel_list.focus(item); self.channel_list.see(item)
            self.after(60, self.play_selected)
        elif not self.powered:
            self.screen_message.config(text="POWER OFF")
            self.screen_message.place(relx=0.5, rely=0.5, anchor="center")

    def _guide_tune(self, _event=None) -> None:
        selection = self.guide_tree.selection()
        if not selection:
            return
        channel = self.channels[int(selection[0])]
        try:
            idx = self.filtered_channels.index(channel)
        except ValueError:
            self.search_var.set(""); self.filter_channels(); idx = self.filtered_channels.index(channel)
        item = str(idx)
        self.channel_list.selection_set(item); self.channel_list.focus(item); self.channel_list.see(item)
        self.hide_guide(resume=False); self.after(40, self.play_selected)

    def _guide_selection_changed(self, _event=None) -> None:
        selection = self.guide_tree.selection()
        if not selection:
            return
        channel = self.channels[int(selection[0])]
        current, nxt = self._current_next(channel.guide_number)
        program = current or nxt
        if not program:
            self.guide_detail.config(text=f"{channel.guide_number} {channel.guide_name} — No program information available.")
            return
        parts = [f"{program.time_text}  •  {program.title}"]
        if program.episode: parts.append(program.episode)
        if program.synopsis: parts.append(program.synopsis)
        self.guide_detail.config(text="   ".join(parts))

    def show_about(self) -> None:
        if self.guide_visible:
            self.hide_guide(resume=False)
        # mpv renders in a native child window above Tk widgets. Stop it while
        # About is displayed, then resume the same channel when About closes.
        self.about_resume_channel = self.selected_channel()
        self.about_was_playing = bool(
            self.powered and self.player and self.player.process
            and self.player.process.poll() is None
        )
        if self.about_was_playing and self.player:
            self.player.stop()
        self.osd.place_forget()
        self.screen_message.place_forget()
        self.about_panel.place(relx=0.16, rely=0.12, relwidth=0.68, relheight=0.72)
        self.about_panel.lift()

    def hide_about(self, resume: bool = True) -> None:
        if not hasattr(self, "about_panel"):
            return
        was_playing = self.about_was_playing
        resume_channel = self.about_resume_channel
        self.about_panel.place_forget()
        self.about_was_playing = False
        self.about_resume_channel = None
        if resume and was_playing and resume_channel and not resume_channel.drm:
            try:
                idx = self.filtered_channels.index(resume_channel)
            except ValueError:
                self.favorites_only.set(False)
                self.search_var.set("")
                self.filter_channels()
                idx = self.filtered_channels.index(resume_channel)
            item = str(idx)
            self.channel_list.selection_set(item)
            self.channel_list.focus(item)
            self.channel_list.see(item)
            self.after(60, self.play_selected)
        elif not self.powered:
            self.screen_message.config(text="POWER OFF")
            self.screen_message.place(relx=0.5, rely=0.5, anchor="center")

    def _start_drag(self, event: tk.Event) -> None:
        if not self._maximized:
            self._drag_x = event.x_root - self.winfo_x(); self._drag_y = event.y_root - self.winfo_y()

    def _drag_window(self, event: tk.Event) -> None:
        if not self._maximized:
            self.geometry(f"+{event.x_root - self._drag_x}+{event.y_root - self._drag_y}")

    def _force_taskbar_icon(self) -> None:
        """Ensure a frameless Tk window is represented as a normal Windows app."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            if not hwnd:
                hwnd = self.winfo_id()
            GWL_EXSTYLE = -20
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_APPWINDOW = 0x00040000
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style = (style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
            ctypes.windll.user32.ShowWindow(hwnd, 5)
            ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0027)
        except Exception:
            pass

    def minimize_window(self) -> None:
        self.overrideredirect(False); self.iconify(); self.after(200, self._restore_frameless_after_minimize)

    def _restore_frameless_after_minimize(self) -> None:
        if self.state() == "normal":
            self.overrideredirect(True)
            self.after(50, self._force_taskbar_icon)
        else:
            self.after(200, self._restore_frameless_after_minimize)

    def toggle_maximize(self) -> None:
        if self._maximized:
            self.geometry(self._validated_geometry(self._normal_geometry)); self._maximized = False
        else:
            self._normal_geometry = self.geometry()
            self.geometry(f"{self.winfo_screenwidth()}x{self.winfo_screenheight()}+0+0"); self._maximized = True

    def load_settings(self) -> dict[str, Any]:
        try:
            return json.loads(config_path().read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_settings(self) -> None:
        self.settings.update({
            "volume": self.volume.get(),
            "boost_db": self.boost.get(),
            "geometry": self._normal_geometry if self._maximized else self.geometry(),
            "favorites_only": self.favorites_only.get(),
            "favorite_channels": sorted(self.favorite_numbers),
        })
        try:
            config_path().write_text(json.dumps(self.settings, indent=2), encoding="utf-8")
        except OSError:
            pass

    def on_close(self) -> None:
        self.save_settings()
        if self.player:
            self.player.stop()
        self.destroy()


if __name__ == "__main__":
    set_windows_app_id()
    TVApp().mainloop()
