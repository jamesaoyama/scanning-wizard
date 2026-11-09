#!/usr/bin/env python3
"""
Scanning Wizard 2026, v4

Put the scans in the scans folder, pick the batch type and press Start. The
wizard straightens the photos, numbers them, saves high res and low res
copies, uploads the pictures and writes the eBay titles. Then you check the
titles and press Save: the CSV is written and the scans folder is cleared.
"""
import os
import platform
import queue
import subprocess
import sys
import threading
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

if sys.version_info < (3, 10):
    message = (f"Scanning Wizard needs Python 3.10 or newer.\n\nThis PC has Python "
               f"{platform.python_version()}. Install a newer Python from python.org, then try again.")
    try:
        import tkinter as _tk
        from tkinter import messagebox as _mb
        _r = _tk.Tk()
        _r.withdraw()
        _mb.showerror("Scanning Wizard", message)
    except Exception:
        print(message)
    sys.exit(1)

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, scrolledtext, ttk

# ─── Packages (installed automatically the first time) ────────────────────────

PACKAGES = [  # pip name, import name, needed to start at all
    ("Pillow", "PIL", True),
    ("requests", "requests", True),
    ("opencv-python-headless", "cv2", False),   # straightening
    ("numpy", "numpy", False),
    ("boto3", "boto3", False),                  # Amazon face match
    ("anthropic", "anthropic", False),          # Claude
    ("keyring", "keyring", False),              # keys in Windows Credential Manager
    ("send2trash", "send2trash", False),        # scans to the Recycle Bin
]


def missing_packages():
    import importlib.util
    return [p for p in PACKAGES if importlib.util.find_spec(p[1]) is None]


def install_missing(root):
    """Install missing packages once, with a small 'please wait' window.
    Returns the optional packages that still couldn't be installed."""
    missing = missing_packages()
    if not missing or getattr(sys, "frozen", False):
        return [p[0] for p in missing]
    names = ", ".join(p[0] for p in missing)
    splash = tk.Toplevel(root)
    splash.title("Scanning Wizard")
    splash.configure(bg="#1a1a2e")
    tk.Label(splash, text=f"Setting up Scanning Wizard (one time only)\n\nInstalling: {names}\n\n"
                          "This can take a few minutes.", bg="#1a1a2e", fg="#eaeaea",
             font=("TkDefaultFont", 12), padx=40, pady=30, justify="center").pack()
    splash.update()
    flags = 0x08000000 if sys.platform == "win32" else 0     # no console window
    try:
        proc = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet"] + [p[0] for p in missing],
                              capture_output=True, text=True, timeout=1200, creationflags=flags)
        output = (proc.stdout or "") + (proc.stderr or "")
    except Exception as e:
        output = str(e)
    if output.strip():
        logging.getLogger("scanning_wizard").warning("pip: %s", output[-4000:])
    splash.destroy()
    import importlib
    importlib.invalidate_caches()
    still = missing_packages()
    needed = [p[0] for p in still if p[2]]
    if needed:
        messagebox.showerror("Scanning Wizard",
                             f"Couldn't install {', '.join(needed)}, which Scanning Wizard needs.\n\n"
                             "Check the internet connection and try again.\n\n"
                             f"Details: {output.strip()[-600:]}")
        sys.exit(1)
    return [p[0] for p in still]


# ─── Look ─────────────────────────────────────────────────────────────────────

C = {
    "bg": "#1a1a2e", "bg2": "#16213e", "card": "#0f3460", "accent": "#e94560", "accent2": "#ff6b81",
    "green": "#2ed573", "yellow": "#ffa502", "red": "#ff4757", "text": "#eaeaea", "dim": "#8892a4",
    "bright": "#ffffff", "input": "#0d1b2a", "border": "#2a2a4a", "pending": "#3a3a5c",
    "row": "#1e2746", "row_check": "#2e2a2a",
}
FAMILY = "TkDefaultFont"


def font(size=11, weight="normal"):
    return (FAMILY, size, weight)


def open_path(path):
    path = str(path)
    try:
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        messagebox.showerror("Scanning Wizard", f"Couldn't open {path}\n\n{e}")


class FlatButton(tk.Label):
    """A coloured button that looks the same on Windows and Mac."""

    STYLES = {
        "primary": (C["accent"], "#ffffff", C["accent2"]),
        "good": (C["green"], "#0b1d10", "#5ae08f"),
        "normal": (C["card"], C["text"], "#16467f"),
        "quiet": (C["bg2"], C["text"], C["card"]),
    }

    def __init__(self, parent, text, command, kind="normal", size=11, bold=None, padx=16, pady=7):
        self.colors = self.STYLES[kind]
        bold = (kind == "primary") if bold is None else bold
        super().__init__(parent, text=text, bg=self.colors[0], fg=self.colors[1], padx=padx, pady=pady,
                         font=font(size, "bold" if bold else "normal"), cursor="hand2")
        self.command = command
        self.enabled = True
        self.bind("<Button-1>", lambda e: self.enabled and self.command and self.command())
        self.bind("<Enter>", lambda e: self.enabled and self.configure(bg=self.colors[2]))
        self.bind("<Leave>", lambda e: self.enabled and self.configure(bg=self.colors[0]))

    def set_enabled(self, on=True):
        self.enabled = on
        self.configure(bg=self.colors[0] if on else C["pending"], fg=self.colors[1] if on else C["dim"],
                       cursor="hand2" if on else "arrow")


def entry(parent, var, width=30, show=""):
    return tk.Entry(parent, textvariable=var, width=width, show=show, font=font(11), bg=C["input"],
                    fg=C["text"], insertbackground=C["text"], relief="flat", highlightthickness=1,
                    highlightbackground=C["border"], highlightcolor=C["accent"])


def label(parent, text="", size=11, color="text", weight="normal", bg="bg", **kw):
    return tk.Label(parent, text=text, font=font(size, weight), fg=C[color], bg=C[bg], **kw)


class Scrollable(tk.Frame):
    """A frame with a vertical scrollbar; put widgets in .inner."""

    def __init__(self, parent, bg="bg"):
        super().__init__(parent, bg=C[bg])
        self.canvas = tk.Canvas(self, bg=C[bg], highlightthickness=0)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=C[bg])
        self.window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.bar.pack(side="right", fill="y")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.window, width=e.width))

    def scroll(self, steps):
        if self.canvas.yview() != (0.0, 1.0):
            self.canvas.yview_scroll(steps, "units")

    def to_top(self):
        self.canvas.yview_moveto(0)


def on_wheel(e):
    """Scroll whichever list is under the mouse (set once for the whole app)."""
    try:
        w = e.widget.winfo_containing(e.x_root, e.y_root)
    except Exception:
        return
    while w is not None and not isinstance(w, Scrollable):
        w = w.master
    if w is None:
        return
    if e.num == 4:
        steps = -3
    elif e.num == 5:
        steps = 3
    elif e.delta:
        steps = (-int(e.delta / 120) if abs(e.delta) >= 120 else (-1 if e.delta > 0 else 1)) * 3
    else:
        return
    w.scroll(steps)


# ─── The app ──────────────────────────────────────────────────────────────────

class App:
    def __init__(self, root, missing_optional):
        self.root = root
        self.missing_optional = missing_optional
        root.title("Scanning Wizard 2026")
        root.geometry("1120x780")
        root.minsize(940, 640)
        root.configure(bg=C["bg"])
        root.report_callback_exception = self.report_error
        self.store = core.SecretStore()
        self.cfg = core.load_config(self.store)
        self.events = queue.Queue()
        self.ui_calls = queue.Queue()
        self.worker = None
        self.stop = threading.Event()
        self.on_event = None
        self.screen = None
        self.style_ttk()

        top = tk.Frame(root, bg=C["bg2"], height=60)
        top.pack(fill="x")
        top.pack_propagate(False)
        label(top, "Scanning Wizard 2026", 18, "bright", "bold", bg="bg2").pack(side="left", padx=20)
        FlatButton(top, "Settings", self.open_settings, "normal").pack(side="right", padx=20, pady=12)
        self.body = tk.Frame(root, bg=C["bg"])
        self.body.pack(fill="both", expand=True)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind_all("<MouseWheel>", on_wheel)
        root.bind_all("<Button-4>", on_wheel)
        root.bind_all("<Button-5>", on_wheel)
        self.poll()
        self.show_start()

    def style_ttk(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure("TProgressbar", troughcolor=C["bg2"], background=C["accent"], bordercolor=C["bg2"],
                    lightcolor=C["accent"], darkcolor=C["accent"], thickness=14)
        s.configure("TScrollbar", background=C["card"], troughcolor=C["bg"], bordercolor=C["bg"],
                    arrowcolor=C["text"], lightcolor=C["card"], darkcolor=C["card"])
        s.configure("TNotebook", background=C["bg"], borderwidth=0)
        s.configure("TNotebook.Tab", background=C["bg2"], foreground=C["text"], padding=(16, 7),
                    font=font(11))
        s.map("TNotebook.Tab", background=[("selected", C["card"])], foreground=[("selected", C["bright"])])
        s.configure("TCombobox", fieldbackground=C["input"], background=C["card"], foreground=C["text"],
                    arrowcolor=C["text"], bordercolor=C["border"], lightcolor=C["input"], darkcolor=C["input"])
        s.map("TCombobox", fieldbackground=[("readonly", C["input"])], foreground=[("readonly", C["text"])])
        self.root.option_add("*TCombobox*Listbox.background", C["input"])
        self.root.option_add("*TCombobox*Listbox.foreground", C["text"])
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.root.unbind_class("TCombobox", seq)   # scrolling the page mustn't change a choice

    # --- threads
    def emit(self, kind, **data):           # from the worker thread
        self.events.put((kind, data))

    def call_ui(self, fn, *args):           # from any thread
        self.ui_calls.put((fn, args))

    def poll(self):
        """Hand worker messages to the window. A failure in one handler is logged
        and shown, and never stops the next messages from arriving."""
        try:
            for source in (self.events, self.ui_calls):
                while True:
                    try:
                        item = source.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        if source is self.events:
                            if self.on_event:
                                self.on_event(*item)
                        else:
                            item[0](*item[1])
                    except tk.TclError:
                        pass        # the screen it was for has closed
                    except Exception:
                        self.report_error(*sys.exc_info())
        finally:
            self.root.after(100, self.poll)

    def busy(self):
        return self.worker is not None and self.worker.is_alive()

    def run_worker(self, target, on_event=None):
        if self.busy():
            messagebox.showinfo("Scanning Wizard", "Please wait for the current job to finish.")
            return False
        self.stop.clear()
        if on_event is not None:
            self.on_event = on_event
        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()
        return True

    def report_error(self, exc, val, tb):
        core.log.error("Window error", exc_info=(exc, val, tb))
        messagebox.showerror("Scanning Wizard", f"Something went wrong: {val}\n\nDetails are in {core.LOG_PATH}")

    def on_close(self):
        if self.busy():
            if not messagebox.askyesno("Scanning Wizard", "The wizard is still working. Stop and close?\n\n"
                                                          "Everything done so far is saved; press Continue next time."):
                return
            self.stop.set()
            self.worker.join(timeout=3)
        if self.screen and hasattr(self.screen, "flush"):
            self.screen.flush()
        self.root.destroy()

    def clear(self):
        if self.screen and hasattr(self.screen, "flush"):
            self.screen.flush()
        for w in self.body.winfo_children():
            w.destroy()
        self.on_event = None
        self.screen = None

    # --- screens
    def show_start(self):
        self.clear()
        self.screen = StartScreen(self, self.body)

    def show_working(self, batch, scans):
        self.clear()
        self.screen = WorkingScreen(self, self.body, batch, scans)

    def show_review(self, batch):
        self.clear()
        self.screen = ReviewScreen(self, self.body, batch)

    def show_done(self, batch, result):
        self.clear()
        self.screen = DoneScreen(self, self.body, batch, result)

    def open_settings(self, tab=0):
        if self.busy():
            messagebox.showinfo("Scanning Wizard", "Settings can be changed once the wizard has stopped working.")
            return
        SettingsDialog(self, tab)

    def settings_saved(self):
        if isinstance(self.screen, StartScreen):
            self.show_start()


# ─── Screen 1: Start ──────────────────────────────────────────────────────────

class StartScreen:
    def __init__(self, app, parent):
        self.app = app
        cfg = app.cfg
        try:
            if Path(cfg["input_folder"]).parent.is_dir():
                Path(cfg["input_folder"]).mkdir(exist_ok=True)
        except OSError:
            pass
        self.type_var = tk.StringVar(value=cfg.get("batch_type", "SM"))
        outer = Scrollable(parent)
        outer.pack(fill="both", expand=True)
        page = tk.Frame(outer.inner, bg=C["bg"])
        page.pack(fill="both", expand=True, padx=36, pady=24)

        label(page, "New batch", 22, "bright", "bold").pack(anchor="w")
        label(page, "Put the scans in the scans folder, choose what they are, and press Start. "
                    "Everything else happens on its own; you check the titles at the end.",
              11, "dim", wraplength=820, justify="left").pack(anchor="w", pady=(4, 18))

        # 1. batch type
        label(page, "What are you scanning?", 13, "text", "bold").pack(anchor="w")
        types = tk.Frame(page, bg=C["bg"])
        types.pack(anchor="w", pady=(8, 4))
        self.type_cards = {}
        for code in core.BATCH_TYPES:
            card = tk.Label(types, text=f"{code}\n{cfg.get('title_suffix_' + code, '')}", font=font(12, "bold"),
                            width=22, height=3, cursor="hand2", justify="center")
            card.pack(side="left", padx=(0, 10))
            card.bind("<Button-1>", lambda e, c=code: self.pick_type(c))
            self.type_cards[code] = card
        self.batch_label = label(page, "", 10, "dim")
        self.batch_label.pack(anchor="w", pady=(2, 18))

        # 2. scans
        scans_card = tk.Frame(page, bg=C["bg2"], padx=18, pady=14)
        scans_card.pack(fill="x")
        label(scans_card, "Scans", 13, "text", "bold", bg="bg2").grid(row=0, column=0, sticky="w")
        self.count_label = label(scans_card, "", 16, "bright", "bold", bg="bg2")
        self.count_label.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.count_note = label(scans_card, "", 10, "dim", bg="bg2", wraplength=560, justify="left")
        self.count_note.grid(row=2, column=0, sticky="w")
        label(scans_card, cfg["input_folder"], 10, "dim", bg="bg2").grid(row=3, column=0, sticky="w", pady=(6, 0))
        btns = tk.Frame(scans_card, bg=C["bg2"])
        btns.grid(row=0, column=1, rowspan=4, sticky="e")
        scans_card.grid_columnconfigure(0, weight=1)
        FlatButton(btns, "↻ Look again", self.refresh, "quiet").pack(side="left", padx=4)
        FlatButton(btns, "Open folder", lambda: open_path(cfg["input_folder"]), "normal").pack(side="left", padx=4)
        FlatButton(btns, "Change…", lambda: app.open_settings(0), "normal").pack(side="left", padx=4)

        # 3. what happens
        plan = tk.Frame(page, bg=C["bg2"], padx=18, pady=14)
        plan.pack(fill="x", pady=(12, 0))
        label(plan, "Then, automatically", 13, "text", "bold", bg="bg2").pack(anchor="w", pady=(0, 6))
        for line, color in self.plan_lines():
            label(plan, line, 11, color, bg="bg2", wraplength=820, justify="left").pack(anchor="w", pady=1)
        FlatButton(plan, "Change settings", lambda: app.open_settings(2), "quiet", size=10).pack(anchor="w", pady=(8, 0))

        self.warn_box = tk.Frame(page, bg=C["bg"])
        self.warn_box.pack(fill="x", pady=(10, 0))

        bottom = tk.Frame(page, bg=C["bg"])
        bottom.pack(fill="x", pady=(16, 6))
        self.start_btn = FlatButton(bottom, "Start ▶", self.start, "primary", size=15, padx=34, pady=12)
        self.start_btn.pack(side="right")

        # unfinished batches
        self.unfinished_box = tk.Frame(page, bg=C["bg"])
        self.unfinished_box.pack(fill="x", pady=(16, 0))
        self.pick_type(self.type_var.get())
        self.refresh()

    def plan_lines(self):
        cfg = self.app.cfg
        lines = [("✓  Straighten, number and save a full-size copy (high res) and an "
                  f"{cfg['low_res_longest_side']} px copy (low res) of each photo", "text")]
        if cfg.get("upload_mode") == "ftp":
            lines.append((f"✓  Upload the low res pictures to {cfg.get('ftp_host')}", "text"))
        else:
            lines.append(("•  You upload the low res pictures yourself (Settings > Upload)", "dim"))
        services = []
        if cfg.get("google_vision_api_key"):
            services.append("Google Vision")
        if cfg.get("serpapi_api_key"):
            services.append("Google Lens")
        if cfg.get("aws_access_key_id") and cfg.get("aws_secret_access_key"):
            services.append("Amazon")
        writers = [core.WRITER_LABELS[w] for w in core.configured_writers(cfg)]
        if writers:
            services.append(" → ".join(writers))
        if cfg.get("film_check"):
            services.append("film database")
        low, high = core.cost_per_photo(cfg)
        cost = f" (about ${low:.3f}–${high:.3f} a photo)" if high else ""
        lines.append((f"✓  Find titles with {', '.join(services) or 'nothing yet: add a key in Settings'}{cost}",
                      "text" if writers else "yellow"))
        lines.append(("✓  Then you check the titles and press Save: the CSV is written and the scans folder "
                      "is cleared" if cfg.get("after_finish") == "recycle" else
                      "✓  Then you check the titles and press Save to write the CSV", "text"))
        return lines

    def warnings(self):
        """(text, level, settings tab): level 2 stops Start, 1 is a warning, 0 a note."""
        cfg = self.app.cfg
        out = []
        if not core.configured_writers(cfg):
            out.append(("No AI key is set up, so no titles can be written.", 2, 2))
        if cfg.get("upload_mode") == "ftp" and not cfg.get("ftp_user"):
            out.append(("The FTP username isn't set, so pictures can't be uploaded.", 1, 1))
        if not str(cfg.get("picture_url_template", "")).lower().startswith("https://"):
            out.append(("Picture links start with http://. eBay only shows pictures from https:// links.", 1, 1))
        if cfg.get("anthropic_api_key") and "anthropic" in self.app.missing_optional:
            out.append(("Claude can't be used: the 'anthropic' package didn't install.", 1, 2))
        if cfg.get("aws_access_key_id") and "boto3" in self.app.missing_optional:
            out.append(("Amazon can't be used: the 'boto3' package didn't install.", 1, 2))
        if cfg.get("straighten") and "opencv-python-headless" in self.app.missing_optional:
            out.append(("Straightening is off: OpenCV didn't install.", 0, 0))
        if not self.app.store.secure:
            out.append(("Keys are saved in a plain settings file on this PC (the keyring package isn't available).", 0, 2))
        return out

    def pick_type(self, code):
        self.type_var.set(code)
        for c, card in self.type_cards.items():
            on = c == code
            card.configure(bg=C["accent"] if on else C["card"], fg="#ffffff" if on else C["text"])
        self.batch_label.configure(text=f"This batch will be called {core.Batch.name_for(code)}. "
                                        f"Photos are numbered {core.Batch.name_for(code)}_0001, _0002 …")
        if self.app.cfg.get("batch_type") != code:
            self.app.cfg["batch_type"] = code
            core.save_config(self.app.cfg, self.app.store)

    def refresh(self):
        cfg = self.app.cfg
        self.scans, done = core.new_scans(cfg)
        n = len(self.scans)
        if n:
            self.count_label.configure(text=f"{n} new scan{'s' if n != 1 else ''} ready")
        else:
            self.count_label.configure(text="No new scans yet")
        notes = []
        if not n:
            notes.append("Put the scanned photos in the scans folder, then press Look again.")
        broken = core.broken_scans(cfg)
        if done > len(broken):
            notes.append(f"{done - len(broken)} file{'s' if done - len(broken) != 1 else ''} in the folder "
                         "already belong to a batch.")
        if broken:
            notes.append(f"Couldn't open: {', '.join(broken[:5])}{' …' if len(broken) > 5 else ''} "
                         "(not pictures, or damaged). Remove or rescan them.")
        self.count_note.configure(text=" ".join(notes))
        for w in self.warn_box.winfo_children():
            w.destroy()
        blocking = False
        for text, level, tab in self.warnings():
            color = "red" if level == 2 else "yellow" if level == 1 else "dim"
            row = tk.Frame(self.warn_box, bg=C["bg"])
            row.pack(anchor="w", pady=1)
            label(row, ("⚠  " if level else "•  ") + text, 11, color).pack(side="left")
            if level:
                fix = tk.Label(row, text="Fix in Settings", font=font(10, "underline"), fg=C["accent2"],
                               bg=C["bg"], cursor="hand2")
                fix.pack(side="left", padx=10)
                fix.bind("<Button-1>", lambda e, t=tab: self.app.open_settings(t))
            blocking = blocking or level == 2
        self.start_btn.configure(text=f"Start {n} photo{'s' if n != 1 else ''} ▶" if n else "Start ▶")
        self.start_btn.set_enabled(bool(n) and not blocking)

        for w in self.unfinished_box.winfo_children():
            w.destroy()
        pending = core.Batch.unfinished(cfg["output_folder"])
        if pending:
            label(self.unfinished_box, "Not finished yet", 13, "text", "bold").pack(anchor="w", pady=(0, 6))
            for b in pending:
                row = tk.Frame(self.unfinished_box, bg=C["bg2"], padx=14, pady=8)
                row.pack(fill="x", pady=2)
                label(row, b.name, 12, "bright", "bold", bg="bg2").pack(side="left")
                label(row, "   " + b.summary(), 11, "dim", bg="bg2").pack(side="left")
                FlatButton(row, "Continue", lambda b=b: self.cont(b), "good", size=10).pack(side="right")

    def start(self):
        cfg = self.app.cfg
        try:
            Path(cfg["output_folder"]).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Scanning Wizard", f"Can't use the output folder:\n{cfg['output_folder']}\n\n{e}")
            return
        self.refresh()
        if not self.scans:
            return
        batch = core.Batch.open_or_create(cfg["output_folder"], self.type_var.get())
        self.app.show_working(batch, self.scans)

    def cont(self, batch):
        batch = core.Batch.load(batch.state_path)
        if batch.open_items():
            self.app.show_working(batch, [])
            return
        app, cfg = self.app, self.app.cfg      # Save was interrupted: finish it

        def job():
            try:
                result = core.finish_batch(cfg, batch, lambda t, level="info": core.log.info(t))
                app.call_ui(app.show_done, batch, result)
            except Exception as e:
                core.log.exception("Finish failed")
                app.call_ui(messagebox.showerror, "Scanning Wizard", f"Couldn't finish {batch.name}:\n\n{e}")

        app.run_worker(job)


# ─── Screen 2: Working ────────────────────────────────────────────────────────

class WorkingScreen:
    ICONS = {"pending": ("○", "pending"), "active": ("◉", "accent"), "done": ("●", "green"), "error": ("✕", "red")}

    def __init__(self, app, parent, batch, scans):
        self.app, self.batch, self.scans = app, batch, scans
        self.current = None
        page = tk.Frame(parent, bg=C["bg"])
        page.pack(fill="both", expand=True, padx=36, pady=24)
        label(page, f"Working on {batch.name}", 22, "bright", "bold").pack(anchor="w")
        label(page, "You can leave this running. Everything is saved as it goes, so if it stops, "
                    "Continue picks up where it left off.", 11, "dim").pack(anchor="w", pady=(4, 16))

        steps = tk.Frame(page, bg=C["bg2"], padx=20, pady=14)
        steps.pack(fill="x")
        self.rows = {}
        for key, text in core.STAGES:
            r = tk.Frame(steps, bg=C["bg2"])
            r.pack(fill="x", pady=3)
            icon = label(r, "○", 14, "pending", bg="bg2", width=2)
            icon.pack(side="left")
            name = label(r, text, 12, "dim", bg="bg2")
            name.pack(side="left", padx=(6, 0))
            detail = label(r, "", 11, "dim", bg="bg2")
            detail.pack(side="right")
            self.rows[key] = (icon, name, detail)

        self.bar = ttk.Progressbar(page, mode="determinate", maximum=1, value=0)
        self.bar.pack(fill="x", pady=(18, 6))
        self.now = label(page, "", 11, "text")
        self.now.pack(anchor="w")

        self.message = label(page, "", 12, "text", wraplength=860, justify="left")
        self.message.pack(anchor="w", pady=(14, 6))
        self.buttons = tk.Frame(page, bg=C["bg"])
        self.buttons.pack(fill="x")
        self.details_btn = tk.Label(page, text="Show details ▾", font=font(10, "underline"), fg=C["dim"],
                                    bg=C["bg"], cursor="hand2")
        self.details_btn.pack(anchor="w", pady=(10, 0))
        self.details_btn.bind("<Button-1>", lambda e: self.toggle_details())
        self.log = scrolledtext.ScrolledText(page, font=("Courier", 10), bg=C["input"], fg=C["text"],
                                             relief="flat", height=12, state="disabled", wrap="word")
        for tag, color in (("error", C["red"]), ("warn", C["yellow"]), ("info", C["text"])):
            self.log.tag_configure(tag, foreground=color)
        self.problems = 0
        self.run()

    def toggle_details(self):
        if self.log.winfo_ismapped():
            self.log.pack_forget()
            self.details_btn.configure(text="Show details ▾")
        else:
            self.log.pack(fill="both", expand=True, pady=(6, 0))
            self.details_btn.configure(text="Hide details ▴")

    def set_buttons(self, *specs):
        for w in self.buttons.winfo_children():
            w.destroy()
        for text, cmd, kind in specs:
            FlatButton(self.buttons, text, cmd, kind).pack(side="left", padx=(0, 10))

    def run(self):
        self.message.configure(text="")
        self.set_buttons(("Stop", self.stop, "quiet"))
        for key in self.rows:
            self.mark(key, "pending")
        pipeline = core.Pipeline(self.app.cfg, self.batch, self.app.emit, self.app.stop, scans=self.scans)
        self.scans = []          # added on the first run; a retry continues the same batch
        self.app.run_worker(pipeline.run, self.on_event)

    def stop(self):
        self.app.stop.set()
        self.set_buttons()
        self.message.configure(text="Stopping after the current photo…", fg=C["yellow"])

    def mark(self, key, state):
        icon, name, detail = self.rows[key]
        sym, color = self.ICONS[state]
        icon.configure(text=sym, fg=C[color])
        name.configure(fg=C["bright"] if state == "active" else C["text"] if state == "done" else
                       C["red"] if state == "error" else C["dim"],
                       font=font(12, "bold" if state == "active" else "normal"))

    def on_event(self, kind, data):
        keys = [k for k, _ in core.STAGES]
        if kind == "stage":
            for k in keys[:keys.index(data["key"])]:
                self.mark(k, "done")
            self.mark(data["key"], "active")
            self.current = data["key"]
            self.bar.configure(maximum=1, value=0)
            self.now.configure(text="")
        elif kind == "progress":
            total = max(1, data["total"])
            self.bar.configure(maximum=total, value=data["done"])
            self.rows[data["key"]][2].configure(text=f"{data['done']} of {data['total']}")
            self.now.configure(text=data.get("text", ""))
        elif kind == "log":
            level = data.get("level", "info")
            self.problems += level in ("warn", "error")
            self.log.configure(state="normal")
            self.log.insert("end", data["text"] + "\n", level)
            self.log.see("end")
            self.log.configure(state="disabled")
        elif kind == "finished":
            for k in keys:
                self.mark(k, "done")
            self.now.configure(text="All done. Opening the review…")
            self.app.root.after(700, lambda: self.app.show_review(core.Batch.load(self.batch.state_path)))
        elif kind == "stopped":
            if self.current:
                self.mark(self.current, "pending")
            self.message.configure(text="Stopped. Everything done so far is saved.", fg=C["yellow"])
            self.set_buttons(("Continue", self.run, "good"), ("Back to start", self.app.show_start, "quiet"))
        elif kind == "error":
            if self.current:
                self.mark(self.current, "error")
            self.message.configure(text=data["message"], fg=C["red"])
            self.set_buttons(("Try again", self.run, "good"), ("Settings", self.app.open_settings, "normal"),
                             ("Back to start", self.app.show_start, "quiet"))


# ─── Screen 3: Review ─────────────────────────────────────────────────────────

class ReviewRow:
    THUMB = (110, 140)

    def __init__(self, screen, parent, item):
        self.screen, self.app, self.item = screen, screen.app, item
        batch = screen.batch
        self.frame = tk.Frame(parent, bg=C["row"], padx=0, pady=0)
        self.flag = tk.Frame(self.frame, width=5, bg=C["row"])
        self.flag.pack(side="left", fill="y")
        body = tk.Frame(self.frame, bg=C["row"], padx=12, pady=10)
        body.pack(side="left", fill="both", expand=True)

        self.thumb = tk.Label(body, bg=C["bg2"], width=14, height=8, text="…", fg=C["dim"], cursor="hand2")
        self.thumb.grid(row=0, column=0, rowspan=5, sticky="n", padx=(0, 14))
        self.thumb.bind("<Button-1>", lambda e: open_path(batch.high_path(item)))
        self.photo = None

        head = tk.Frame(body, bg=C["row"])
        head.grid(row=0, column=1, columnspan=4, sticky="w")
        label(head, batch.sku(item), 12, "bright", "bold", bg="row").pack(side="left")
        label(head, f"   from {item['source']}", 10, "dim", bg="row").pack(side="left")
        self.sources = label(head, "", 10, "green", bg="row")
        self.sources.pack(side="left", padx=(14, 0))
        self.warn = label(body, "", 10, "yellow", bg="row", wraplength=640, justify="left")
        self.warn.grid(row=1, column=1, columnspan=4, sticky="w")

        self.vars = {k: tk.StringVar(value=str(item.get(k, "") or "")) for k in ("movie", "year", "actors", "scene", "title")}
        self.loading = False
        fields = (("Movie", "movie", 34, 2, 1), ("Year", "year", 6, 2, 3), ("Actors", "actors", 34, 3, 1),
                  ("Scene", "scene", 18, 3, 3))
        self.entries = []
        for text, key, width, r, c in fields:
            label(body, text, 10, "dim", bg="row").grid(row=r, column=c, sticky="e", padx=(0, 6), pady=2)
            e = entry(body, self.vars[key], width)
            e.grid(row=r, column=c + 1, sticky="w", pady=2)
            self.entries.append(e)
        label(body, "Title", 10, "dim", bg="row").grid(row=4, column=1, sticky="e", padx=(0, 6), pady=(6, 0))
        title_row = tk.Frame(body, bg=C["row"])
        title_row.grid(row=4, column=2, columnspan=3, sticky="w", pady=(6, 0))
        self.title_entry = entry(title_row, self.vars["title"], 72)
        self.title_entry.pack(side="left")
        self.entries.append(self.title_entry)
        self.count = label(title_row, "", 10, "dim", bg="row")
        self.count.pack(side="left", padx=6)
        self.rebuild_btn = tk.Label(title_row, text="↺ rebuild", font=font(10, "underline"), fg=C["accent2"],
                                    bg=C["row"], cursor="hand2")
        self.rebuild_btn.bind("<Button-1>", lambda e: self.rebuild_title(force=True))
        body.grid_columnconfigure(4, weight=1)

        side = tk.Frame(self.frame, bg=C["row"], padx=12, pady=10)
        side.pack(side="right", fill="y")
        self.include_btn = tk.Label(side, font=font(11, "bold"), width=12, pady=6, cursor="hand2")
        self.include_btn.pack(pady=(0, 6))
        self.include_btn.bind("<Button-1>", lambda e: self.toggle_include())
        self.turn_btn = FlatButton(side, "↻ Turn", self.turn, "normal", size=10, padx=10, pady=4)
        self.turn_btn.pack(fill="x", pady=2)
        self.again_btn = FlatButton(side, "Look again", self.look_again, "normal", size=10, padx=10, pady=4)
        self.again_btn.pack(fill="x", pady=2)

        for key in ("movie", "year", "actors", "scene"):
            self.vars[key].trace_add("write", lambda *a, k=key: self.changed(k))
        self.vars["title"].trace_add("write", lambda *a: self.title_typed())
        self.refresh()

    def refresh(self):
        """Show the item's current values (after loading or Look again)."""
        item = self.item
        self.loading = True
        for k, v in self.vars.items():
            v.set(str(item.get(k, "") or ""))
        self.loading = False
        self.sources.configure(text=" · ".join(item.get("sources") or []))
        warns = list(item.get("warnings") or [])
        if not item.get("identified"):
            warns.insert(0, "No title yet: press Look again, or type one in")
        if item.get("turned_auto"):
            warns.append("Straightened automatically: check it's the right way up")
        self.warn.configure(text="⚠ " + " · ".join(warns) if warns else "")
        self.update_count()
        self.update_include()
        self.update_flag()

    def update_flag(self):
        self.flag.configure(bg=C["yellow"] if self.item.get("needs_check") or not self.item.get("identified")
                            else C["green"])

    def update_count(self):
        n, top = len(self.vars["title"].get()), int(self.app.cfg.get("max_title_chars", 80))
        self.count.configure(text=f"{n}/{top}", fg=C["red"] if n > top or n == 0 else C["dim"])
        if self.item.get("title_custom"):
            self.rebuild_btn.pack(side="left")
        else:
            self.rebuild_btn.pack_forget()

    def update_include(self):
        on = self.item.get("include", True)
        self.include_btn.configure(text="✓ Include" if on else "Left out", bg=C["green"] if on else C["pending"],
                                   fg="#0b1d10" if on else C["dim"])

    def changed(self, key):
        if self.loading:
            return
        self.item[key] = self.vars[key].get()
        if not self.item.get("title_custom"):
            self.rebuild_title()
        self.screen.dirty()

    def rebuild_title(self, force=False):
        if force:
            self.item["title_custom"] = False
        title = core.title_for(self.item, self.app.cfg, self.screen.batch.batch_type)
        self.loading = True
        self.vars["title"].set(title)
        self.loading = False
        self.item["title"] = title
        self.update_count()
        self.screen.dirty()

    def title_typed(self):
        self.update_count()
        if self.loading:
            return
        self.item["title"] = self.vars["title"].get()
        self.item["title_custom"] = True
        self.update_count()
        self.screen.dirty()

    def toggle_include(self):
        if self.screen.locked:
            return
        self.item["include"] = not self.item.get("include", True)
        self.update_include()
        self.screen.dirty()
        self.screen.update_summary()

    def set_thumb(self, pil_image):
        from PIL import ImageTk
        if not self.thumb.winfo_exists():
            return
        self.photo = ImageTk.PhotoImage(pil_image)
        self.thumb.configure(image=self.photo, text="", width=self.THUMB[0], height=self.THUMB[1])

    def busy_buttons(self, busy):
        self.turn_btn.set_enabled(not busy)
        self.again_btn.set_enabled(not busy)

    def set_locked(self, locked):
        for e in self.entries:
            e.configure(state="readonly" if locked else "normal", readonlybackground=C["input"])
        self.busy_buttons(locked)

    def turn(self):
        batch, item = self.screen.batch, self.item

        def turned():               # bookkeeping on the window thread
            item["uploaded"] = False
            item["turned_by_you"] = (item.get("turned_by_you", 0) + 90) % 360
            self.screen.flush()
            self.screen.load_thumb(self)

        def job():
            try:
                core.turn_pictures(self.app.cfg, batch, item)
                self.app.call_ui(turned)
            except Exception as e:
                core.log.exception("Turn failed")
                self.app.call_ui(messagebox.showerror, "Scanning Wizard", f"Couldn't turn the picture: {e}")
            self.app.call_ui(self.busy_buttons, False)

        if self.app.run_worker(job):
            self.busy_buttons(True)

    def look_again(self):
        batch, item, cfg = self.screen.batch, self.item, self.app.cfg
        public = bool((batch.data.get("link_check") or {}).get("ok")) and item.get("uploaded")

        def found(result):          # on the window thread, so edits and saves never overlap
            core.apply_result(item, result, cfg, batch.batch_type)
            self.screen.flush()
            self.refresh()
            self.screen.update_summary()

        def job():
            try:
                ident = core.Identifier(cfg, lambda text, level="info": core.log.info(text))
                if not ident.has_any_service():
                    raise core.StageError("No title services are set up (Settings > Identify).")
                result = ident.identify(batch.high_path(item), item.get("url") if public else None)
                self.app.call_ui(found, result)
            except Exception as e:
                core.log.exception("Look again failed")
                self.app.call_ui(messagebox.showerror, "Scanning Wizard", f"Couldn't look this one up: {e}")
            self.app.call_ui(self.busy_buttons, False)

        if self.app.run_worker(job):
            self.busy_buttons(True)
            self.warn.configure(text="Looking again…")


class ReviewScreen:
    def __init__(self, app, parent, batch):
        self.app, self.batch = app, batch
        self.only_check = False
        self.locked = False
        self._save_after = None
        page = tk.Frame(parent, bg=C["bg"])
        page.pack(fill="both", expand=True)
        top = tk.Frame(page, bg=C["bg"], padx=28, pady=14)
        top.pack(fill="x")
        label(top, f"Check the titles: {batch.name}", 20, "bright", "bold").pack(side="left")
        self.filter_btn = FlatButton(top, "", self.toggle_filter, "quiet", size=10)
        self.filter_btn.pack(side="right")
        self.summary = label(page, "", 11, "dim")
        self.summary.pack(anchor="w", padx=28)
        broken = [i for i in batch.items if i.get("error") and not i.get("saved")]
        if broken:
            label(page, "⚠  Couldn't use: " + "; ".join(f"{i['source']} ({i['error']})" for i in broken[:4]) +
                  (" …" if len(broken) > 4 else "") + ". These stay in the scans folder.", 11, "yellow",
                  wraplength=1000, justify="left").pack(anchor="w", padx=28, pady=(4, 0))
        link = batch.data.get("link_check") or {}
        if link and not link.get("ok"):
            banner = tk.Frame(page, bg="#4a1b24", padx=14, pady=8)
            banner.pack(fill="x", padx=28, pady=(8, 0))
            tk.Label(banner, text=f"⚠  {link.get('message')}  Check the picture links before listing "
                                  "(Settings > Upload has a Test button).", font=font(11), fg=C["bright"],
                     bg="#4a1b24", wraplength=900, justify="left").pack(anchor="w")

        self.list = Scrollable(page)
        self.list.pack(fill="both", expand=True, padx=20, pady=10)
        self.rows = []
        items = batch.open_items()
        if not items:
            label(self.list.inner, "Nothing to check in this batch.", 13, "dim").pack(pady=40)
        for item in items:
            if not item.get("title") and item.get("identified"):
                item["title"] = core.title_for(item, app.cfg, batch.batch_type)
            row = ReviewRow(self, self.list.inner, item)
            self.rows.append(row)
        self.layout_rows()

        bottom = tk.Frame(page, bg=C["bg2"], padx=24, pady=12)
        bottom.pack(fill="x", side="bottom")
        FlatButton(bottom, "Back to start", app.show_start, "quiet").pack(side="left")
        self.save_btn = FlatButton(bottom, "Save CSV & finish ✓", self.finish, "primary", size=13, padx=26, pady=10)
        self.save_btn.pack(side="right")
        self.count_label = label(bottom, "", 11, "text", bg="bg2")
        self.count_label.pack(side="right", padx=16)
        self.update_summary()
        threading.Thread(target=self.make_thumbs, daemon=True).start()

    # --- thumbnails (made off the window thread, shown on it)
    def make_thumbs(self):
        for row in list(self.rows):
            self.load_thumb(row, background=True)

    def load_thumb(self, row, background=False):
        from PIL import Image, ImageOps

        def make():
            path = self.batch.low_path(row.item)
            try:
                with Image.open(path) as im:
                    im.draft("RGB", (300, 300))
                    im = ImageOps.exif_transpose(im).convert("RGB")
                    im.thumbnail(ReviewRow.THUMB)
                    return im.copy()
            except Exception:
                return None

        if background:
            im = make()
            if im is not None:
                self.app.call_ui(row.set_thumb, im)
        else:
            im = make()
            if im is not None:
                row.set_thumb(im)

    # --- list
    def layout_rows(self):
        for row in self.rows:
            row.frame.pack_forget()
        shown = 0
        for row in self.rows:
            if self.only_check and not (row.item.get("needs_check") or not row.item.get("identified")):
                continue
            row.frame.pack(fill="x", pady=4)
            shown += 1
        self.list.to_top()

    def toggle_filter(self):
        self.only_check = not self.only_check
        self.layout_rows()
        self.update_summary()

    def update_summary(self):
        items = [r.item for r in self.rows]
        check = sum(1 for i in items if i.get("needs_check") or not i.get("identified"))
        included = sum(1 for i in items if i.get("include", True))
        self.summary.configure(text=f"{len(items)} photos · {check} marked yellow to check · click a picture "
                                    "to open it full size · edit any field and the title updates")
        self.filter_btn.configure(text="Show all photos" if self.only_check else f"Show only the {check} to check")
        self.filter_btn.set_enabled(bool(check) or self.only_check)
        self.count_label.configure(text=f"{included} will go in {self.batch.csv_path.name}")
        self.save_btn.set_enabled(included > 0)

    # --- saving
    def dirty(self):
        if self._save_after:
            self.app.root.after_cancel(self._save_after)
        self._save_after = self.app.root.after(800, self.flush)

    def flush(self):
        if self._save_after:
            try:
                self.app.root.after_cancel(self._save_after)
            except Exception:
                pass
            self._save_after = None
        try:
            self.batch.save()
        except Exception:
            core.log.exception("Couldn't save the batch")

    def finish(self):
        if self.app.busy():
            messagebox.showinfo("Scanning Wizard", "Please wait for the current job to finish.")
            return
        cfg = self.app.cfg
        top = int(cfg.get("max_title_chars", 80))
        included = [r for r in self.rows if r.item.get("include", True)]
        bad = [r for r in included if not r.item.get("title") or len(r.item["title"]) > top]
        if bad:
            r = bad[0]
            messagebox.showwarning("Scanning Wizard", f"{self.batch.sku(r.item)} has "
                                   f"{'no title' if not r.item.get('title') else 'a title over ' + str(top) + ' characters'}. "
                                   "Fix it or mark it Left out.")
            r.title_entry.focus_set()
            return
        left_out = len(self.rows) - len(included)
        text = f"Save {len(included)} title{'s' if len(included) != 1 else ''} to {self.batch.csv_path.name}?"
        if left_out:
            text += f"\n\n{left_out} photo{'s are' if left_out != 1 else ' is'} left out and won't go in the CSV " \
                    "(their pictures stay saved)."
        if cfg.get("after_finish") == "recycle":
            text += "\n\nThe scans for this batch will move to the Recycle Bin."
        if not messagebox.askyesno("Scanning Wizard", text):
            return
        self.flush()
        self.lock(True)      # no edits while saving, so nothing typed now can be lost
        batch = self.batch

        def job():
            try:
                result = core.finish_batch(cfg, batch, lambda t, level="info": core.log.info(t))
                self.app.call_ui(self.app.show_done, batch, result)
            except Exception as e:
                core.log.exception("Finish failed")
                self.app.call_ui(self.finish_failed, str(e))

        self.app.run_worker(job)

    def lock(self, locked):
        self.locked = locked
        for row in self.rows:
            row.set_locked(locked)
        self.save_btn.set_enabled(not locked)
        self.save_btn.configure(text="Saving…" if locked else "Save CSV & finish ✓")

    def finish_failed(self, message):
        self.lock(False)
        messagebox.showerror("Scanning Wizard", f"Couldn't finish the batch:\n\n{message}\n\n"
                                                "Nothing is lost; fix the problem and press Save again.")


# ─── Done ─────────────────────────────────────────────────────────────────────

class DoneScreen:
    def __init__(self, app, parent, batch, result):
        page = tk.Frame(parent, bg=C["bg"])
        page.pack(fill="both", expand=True, padx=36, pady=40)
        label(page, "✓", 48, "green", "bold").pack(anchor="w")
        label(page, f"{batch.name} is saved" if not result.get("missing") else f"{batch.name} is partly saved",
              22, "bright", "bold").pack(anchor="w")
        lines = [f"{result['added']} row{'s' if result['added'] != 1 else ''} added to {batch.csv_path.name}"]
        if result["left_out"]:
            lines.append(f"{result['left_out']} photo{'s' if result['left_out'] != 1 else ''} left out")
        if result["cleared"]:
            lines.append(f"{result['cleared']} scan{'s' if result['cleared'] != 1 else ''} moved out of the scans folder")
        if result.get("broken"):
            lines.append(f"Left in the scans folder because they couldn't be opened: {', '.join(result['broken'])}")
        if result.get("kept"):
            lines.append("Left in the scans folder because they changed after processing or have no high res "
                         f"copy: {', '.join(result['kept'])}")
        if result.get("missing"):
            lines.append(f"Not saved because their pictures are missing: {', '.join(result['missing'])}. "
                         "Press Continue on the start screen to make them again.")
        for line in lines:
            label(page, line, 12, "text").pack(anchor="w", pady=2)
        btns = tk.Frame(page, bg=C["bg"])
        btns.pack(anchor="w", pady=(24, 0))
        FlatButton(btns, "Open CSV", lambda: open_path(batch.csv_path), "normal").pack(side="left", padx=(0, 8))
        FlatButton(btns, "Open high res folder", lambda: open_path(batch.high_dir), "normal").pack(side="left", padx=8)
        FlatButton(btns, "Open low res folder", lambda: open_path(batch.low_dir), "normal").pack(side="left", padx=8)
        FlatButton(page, "Start a new batch ▶", app.show_start, "primary", size=14, padx=28, pady=11).pack(anchor="w", pady=(30, 0))


# ─── Settings ─────────────────────────────────────────────────────────────────

MODEL_CHOICES = {
    "gemini_model": ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"],
    "openai_model": ["gpt-6.1-terra", "gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"],
    "anthropic_model": ["claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5"],
}
ORDERS = [",".join(p) for p in (("gemini", "openai", "claude"), ("gemini", "claude", "openai"),
                                ("openai", "gemini", "claude"), ("openai", "claude", "gemini"),
                                ("claude", "gemini", "openai"), ("claude", "openai", "gemini"))]
SECURITY = [("auto", "Encrypted when the server allows it"), ("tls", "Encrypted only"),
            ("plain", "Not encrypted")]


class SettingsDialog(tk.Toplevel):
    def __init__(self, app, tab=0):
        super().__init__(app.root)
        self.app = app
        self.title("Settings")
        self.configure(bg=C["bg"])
        self.geometry("820x680")
        self.minsize(720, 560)
        self.transient(app.root)
        cfg = app.cfg
        self.vars = {}
        for k, v in cfg.items():
            if isinstance(v, bool):
                self.vars[k] = tk.BooleanVar(value=v)
            elif not k.startswith("description_"):
                self.vars[k] = tk.StringVar(value=str(v))
        self.texts = {}

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=12, pady=(12, 0))
        for title, build in (("Folders", self.tab_folders), ("Upload", self.tab_upload),
                             ("Identify", self.tab_identify), ("Titles", self.tab_titles)):
            holder = Scrollable(nb)
            nb.add(holder, text=title)
            frame = tk.Frame(holder.inner, bg=C["bg"], padx=18, pady=14)
            frame.pack(fill="both", expand=True)
            build(frame)
        nb.select(tab)

        bottom = tk.Frame(self, bg=C["bg"], padx=12, pady=10)
        bottom.pack(fill="x")
        where = "Windows Credential Manager" if sys.platform == "win32" else "the system keychain"
        label(bottom, (f"Keys are stored in {where}." if app.store.secure else
                       "Keys are stored in the settings file on this PC.") +
              f"  Log: {core.LOG_PATH}", 9, "dim").pack(side="left")
        FlatButton(bottom, "Save", self.save, "good", bold=True).pack(side="right")
        FlatButton(bottom, "Cancel", self.destroy, "quiet").pack(side="right", padx=8)
        try:
            self.grab_set()
        except tk.TclError:
            pass

    # --- helpers
    def section(self, parent, text, note=""):
        label(parent, text, 13, "accent", "bold").pack(anchor="w", pady=(14, 2))
        tk.Frame(parent, bg=C["border"], height=1).pack(fill="x", pady=(0, 6))
        if note:
            label(parent, note, 10, "dim", wraplength=700, justify="left").pack(anchor="w", pady=(0, 6))

    def field(self, parent, key, text, width=40, show="", browse=False, choices=None, note=""):
        row = tk.Frame(parent, bg=C["bg"])
        row.pack(fill="x", pady=3)
        label(row, text, 11, width=22, anchor="w").pack(side="left")
        if choices:
            w = ttk.Combobox(row, textvariable=self.vars[key], values=choices, width=width - 2, font=font(11))
        else:
            w = entry(row, self.vars[key], width, show)
        w.pack(side="left")
        if browse:
            FlatButton(row, "Browse", lambda: self.browse(key), "normal", size=10, padx=10, pady=3).pack(side="left", padx=6)
        if note:
            label(parent, note, 9, "dim", wraplength=680, justify="left").pack(anchor="w", padx=(170, 0))
        return row

    def check(self, parent, key, text):
        tk.Checkbutton(parent, text=text, variable=self.vars[key], font=font(11), bg=C["bg"], fg=C["text"],
                       selectcolor=C["card"], activebackground=C["bg"], activeforeground=C["text"]).pack(anchor="w", pady=3)

    def radios(self, parent, key, options):
        for value, text in options:
            tk.Radiobutton(parent, text=text, value=value, variable=self.vars[key], font=font(11), bg=C["bg"],
                           fg=C["text"], selectcolor=C["card"], activebackground=C["bg"],
                           activeforeground=C["text"]).pack(anchor="w", pady=1)

    def test_button(self, parent, name, text="Test"):
        row = tk.Frame(parent, bg=C["bg"])
        row.pack(fill="x", pady=(2, 6), padx=(170, 0))
        result = label(row, "", 10, "dim", wraplength=480, justify="left")
        FlatButton(row, text, lambda: self.run_test(name, result), "normal", size=10, padx=10, pady=3).pack(side="left")
        result.pack(side="left", padx=10)

    def browse(self, key):
        p = filedialog.askdirectory(parent=self, initialdir=self.vars[key].get() or str(Path.home()))
        if p:
            self.vars[key].set(p)

    def collect(self):
        cfg = dict(self.app.cfg)
        for k, v in self.vars.items():
            cfg[k] = v.get()
        for k, t in self.texts.items():
            cfg[k] = t.get("1.0", "end").strip()
        cfg["ftp_security"] = next((v for v, t in SECURITY if t == cfg["ftp_security"]), cfg["ftp_security"])
        if " → " in str(cfg.get("ai_order")):
            names = {v: k for k, v in core.WRITER_LABELS.items()}
            cfg["ai_order"] = ",".join(names.get(x.strip(), x.strip()) for x in cfg["ai_order"].split("→"))
        for key in ("low_res_longest_side", "max_title_chars"):
            try:
                cfg[key] = int(str(cfg[key]).strip())
            except ValueError:
                raise ValueError(f"{key.replace('_', ' ')} must be a number")
        for key in core.SECRET_KEYS + ("ftp_user", "ftp_host", "picture_url_template"):
            cfg[key] = str(cfg.get(key, "")).strip()
        return cfg

    def run_test(self, name, result_label):
        try:
            cfg = self.collect()
        except ValueError as e:
            result_label.configure(text=str(e), fg=C["red"])
            return
        result_label.configure(text="Checking…", fg=C["dim"])

        def job():
            ok, message = core.test_service(name, cfg)
            self.app.call_ui(lambda: result_label.winfo_exists() and
                             result_label.configure(text=("✓ " if ok else "✕ ") + message,
                                                    fg=C["green"] if ok else C["red"]))

        threading.Thread(target=job, daemon=True).start()

    def save(self):
        try:
            cfg = self.collect()
        except ValueError as e:
            messagebox.showerror("Settings", str(e), parent=self)
            return
        if not 30 <= cfg["max_title_chars"] <= 80:
            messagebox.showerror("Settings", "The longest title should be between 30 and 80 characters "
                                             "(eBay allows 80).", parent=self)
            return
        if not 300 <= cfg["low_res_longest_side"] <= 4000:
            messagebox.showerror("Settings", "The low res size should be between 300 and 4000 pixels.", parent=self)
            return
        try:
            core.save_config(cfg, self.app.store)
        except Exception as e:
            messagebox.showerror("Settings", f"Couldn't save the settings: {e}", parent=self)
            return
        self.app.cfg = cfg
        self.destroy()
        self.app.settings_saved()

    # --- tabs
    def tab_folders(self, f):
        self.section(f, "Folders")
        self.field(f, "input_folder", "Scans folder", 50, browse=True)
        self.field(f, "output_folder", "Finished folder", 50, browse=True,
                   note="High res and low res copies, the CSV files and the wizard's own notes go here.")
        self.section(f, "Pictures")
        self.field(f, "low_res_longest_side", "Low res size (px)", 8,
                   note="Longest side of the uploaded pictures. eBay needs at least 500; buyers can zoom from about 1600.")
        self.check(f, "straighten", "Turn sideways and upside-down photos the right way up automatically")
        self.section(f, "When a batch is saved")
        self.radios(f, "after_finish", (("recycle", "Move its scans to the Recycle Bin (the high res copies are kept)"),
                                        ("keep", "Leave the scans in the scans folder")))

    def tab_upload(self, f):
        self.section(f, "Uploading the low res pictures")
        self.radios(f, "upload_mode", (("ftp", "Upload them automatically by FTP"),
                                       ("manual", "I upload the pictures myself")))
        self.section(f, "FTP")
        self.field(f, "ftp_host", "Server", 36)
        self.field(f, "ftp_user", "Username", 36)
        self.field(f, "ftp_pass", "Password", 36, show="•")
        self.field(f, "ftp_remote_dir", "Folder on the server", 36,
                   note="Each batch goes in its own folder inside this one, e.g. /SM110926/")
        self.vars["ftp_security"].set(dict(SECURITY).get(self.vars["ftp_security"].get(), SECURITY[0][1]))
        self.field(f, "ftp_security", "Encryption", 36, choices=[t for _, t in SECURITY])
        self.test_button(f, "ftp", "Test FTP")
        self.section(f, "Picture links", "The link eBay uses for each picture. {batch} becomes the batch name "
                                         "and {file} the file name. eBay only accepts https:// links.")
        self.field(f, "picture_url_template", "Link pattern", 52)
        self.test_button(f, "link", "Test link")

    def tab_identify(self, f):
        self.section(f, "How hard to look")
        self.radios(f, "identify_mode", (("thorough", "Thorough: up to 5 AI tries a photo, until two sources agree"),
                                         ("fast", "Fast: up to 3 tries, stops at the first full answer")))
        labels = {o: " → ".join(core.WRITER_LABELS[w] for w in o.split(",")) for o in ORDERS}
        current = ",".join(core.writer_order(self.app.cfg))
        self.vars["ai_order"].set(labels.get(current, labels[ORDERS[0]]))
        self.field(f, "ai_order", "Try the AI in this order", 30, choices=list(labels.values()))

        self.section(f, "AI title writers", "Each writer sees the photo plus what the services below found. "
                                            "You need at least one key.")
        self.field(f, "gemini_api_key", "Gemini key", 46, show="•")
        self.field(f, "gemini_model", "Gemini model", 30, choices=MODEL_CHOICES["gemini_model"],
                   note="Free tier available at aistudio.google.com (free-tier photos may be used by Google).")
        self.test_button(f, "gemini")
        self.field(f, "openai_api_key", "GPT key", 46, show="•")
        self.field(f, "openai_model", "GPT model", 30, choices=MODEL_CHOICES["openai_model"],
                   note="gpt-6.1-terra is good with pictures; gpt-6-luna is the cheapest.")
        self.test_button(f, "openai")
        self.field(f, "anthropic_api_key", "Claude key", 46, show="•")
        self.field(f, "anthropic_model", "Claude model", 30, choices=MODEL_CHOICES["anthropic_model"],
                   note="Claude won't name people from their faces. It reads captions, knows films from "
                        "sets and costumes, and checks the names Amazon and Google found.")
        self.test_button(f, "claude")

        self.section(f, "Finding the film and the actors", "Optional, but they make titles much more reliable.")
        self.field(f, "google_vision_api_key", "Google Vision key", 46, show="•",
                   note="Finds web pages that show the same photo (film sites name the movie). "
                        "1,000 free a month, then about $3.50 per 1,000.")
        self.test_button(f, "vision")
        self.field(f, "aws_access_key_id", "Amazon key ID", 46, show="•")
        self.field(f, "aws_secret_access_key", "Amazon secret key", 46, show="•")
        self.field(f, "aws_region", "Amazon region", 16,
                   note="Amazon Rekognition matches famous faces. Needs an AWS account and an access key "
                        "with Rekognition permission.")
        self.test_button(f, "amazon")
        self.field(f, "serpapi_api_key", "Google Lens key (SerpApi)", 46, show="•",
                   note="Finds similar pictures online. Needs working https picture links. "
                        "250 free searches a month, paid plans from $25 for 1,000.")
        self.test_button(f, "lens")
        self.check(f, "film_check", "Check each film in the free film database (Wikidata): fixes years, confirms the cast")
        self.field(f, "contact_email", "Contact email (optional)", 36,
                   note="Wikidata asks programs that use it to include a contact, and may slow down ones that "
                        "don't. It's only sent to Wikidata.")

    def tab_titles(self, f):
        self.section(f, "Titles")
        self.field(f, "max_title_chars", "Longest title", 8, note="eBay allows 80 characters.")
        for code in core.BATCH_TYPES:
            self.section(f, f"{code} batches")
            self.field(f, f"title_suffix_{code}", "Title ending", 40)
            label(f, "Description", 11).pack(anchor="w", pady=(6, 2))
            t = tk.Text(f, height=7, wrap="word", font=font(10), bg=C["input"], fg=C["text"],
                        insertbackground=C["text"], relief="flat", highlightthickness=1,
                        highlightbackground=C["border"])
            t.insert("1.0", self.app.cfg.get(f"description_{code}", ""))
            t.pack(fill="x")
            self.texts[f"description_{code}"] = t


# ─── Start ────────────────────────────────────────────────────────────────────

core = None


def main():
    global core, FAMILY
    root = tk.Tk()
    root.withdraw()
    log_path = Path(os.environ.get("SCANNING_WIZARD_CONFIG") or (Path.home() / ".scanning_wizard_config.json")).with_name(".scanning_wizard.log")
    handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger("scanning_wizard")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    missing_optional = install_missing(root)
    import wizard_core
    core = wizard_core
    family = tkfont.nametofont("TkDefaultFont").actual("family")
    FAMILY = "Segoe UI" if sys.platform == "win32" else family
    App(root, missing_optional)
    root.deiconify()
    root.mainloop()


if __name__ == "__main__":
    main()
