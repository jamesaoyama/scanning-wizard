"""
Self-test for a built or installed copy:  "Scanning Wizard.exe" --selftest report.txt

Checks that every package loaded in the built program, runs a whole batch on
made-up pictures (no internet, no paid services, nothing outside a temporary
folder) and opens the window hidden. Writes a report and exits 0 when
everything passed. The Windows build uses it to prove the installer works
before a version is published.
"""
import os
import sys
import tempfile
import threading
import traceback
from pathlib import Path


def run(report_path=None, window_module=None) -> int:
    watchdog = threading.Timer(180, lambda: os._exit(3))     # never hang a build
    watchdog.daemon = True
    watchdog.start()
    base = Path(tempfile.mkdtemp(prefix="sw-selftest-"))
    os.environ["SCANNING_WIZARD_CONFIG"] = str(base / "settings.json")
    os.environ["SCANNING_WIZARD_NO_KEYRING"] = "1"
    os.environ["SCANNING_WIZARD_UPDATE_URL"] = "off"
    import logging
    logging.getLogger("scanning_wizard").addHandler(logging.NullHandler())
    import wizard_core as core          # after the settings path is set
    lines, failed = [], []

    def check(name, fn):
        try:
            detail = fn()
            lines.append(f"PASS  {name}{': ' + detail if detail else ''}")
        except Exception:
            failed.append(name)
            lines.append(f"FAIL  {name}\n{traceback.format_exc()}")

    def packages():
        import PIL, requests, numpy, cv2, boto3, anthropic, keyring, send2trash  # noqa: F401
        return (f"Pillow {PIL.__version__}, OpenCV {cv2.__version__}, anthropic {anthropic.__version__}, "
                f"boto3 {boto3.__version__}")
    check("Packages", packages)

    def credential_store():
        import keyring
        name = type(keyring.get_keyring()).__name__
        if sys.platform == "win32" and "Win" not in name:
            raise RuntimeError(f"Windows Credential Manager not available (got {name})")
        return name      # looked up only; nothing is read or written
    check("Credential store", credential_store)

    def amazon():
        import boto3
        client = boto3.client("rekognition", region_name="us-east-1", aws_access_key_id="x",
                              aws_secret_access_key="y")
        if not hasattr(client, "recognize_celebrities"):
            raise RuntimeError("Rekognition client is missing recognize_celebrities")
        return "Rekognition client builds"
    check("Amazon client", amazon)

    def claude():
        import inspect
        import anthropic
        from anthropic.resources.beta.messages import Messages
        anthropic.Anthropic(api_key="x")
        params = inspect.signature(Messages.create).parameters
        if "fallbacks" not in params or "output_config" not in params:
            raise RuntimeError("this anthropic package is too old")
        return "SDK loads"
    check("Claude SDK", claude)

    def faces():
        finder = core.FaceFinder()
        if finder.kind != "yunet":
            raise RuntimeError(f"YuNet face model didn't load (got {finder.kind})")
        from PIL import Image
        p = base / "blank.jpg"
        Image.new("RGB", (640, 800), (120, 110, 100)).save(p)
        return f"model runs, blank photo -> turn {finder.best_turn(p)}"
    check("Straightening", faces)

    state = {}

    def batch():
        from PIL import Image, ImageDraw

        class Fake:
            def has_any_service(self):
                return True

            def identify(self, path, url=None):
                return {"movie": "Rain Man", "year": "1988", "actors": ["Tom Cruise"], "scene": "casino",
                        "confidence": "high", "sources": ["self-test"], "warnings": [], "clues": ""}

        cfg = core.default_config()
        cfg.update(input_folder=str(base / "scans"), output_folder=str(base / "finished"), upload_mode="manual",
                   film_check=False, after_finish="keep", gemini_api_key="x")
        scans = Path(cfg["input_folder"])
        scans.mkdir(parents=True)
        for n, (fmt, ext) in enumerate((("JPEG", ".jpg"), ("PNG", ".png"), ("TIFF", ".tif"))):
            im = Image.new("RGB", (900, 1200), (60 + n * 40, 80, 90))
            ImageDraw.Draw(im).ellipse([200, 300, 700, 900], fill=(220, 210, 190))
            im.save(scans / f"scan {n + 1}{ext}", fmt)
        (scans / "notes.jpg").write_bytes(b"not a picture")
        b = core.Batch.open_or_create(cfg["output_folder"], "SM")
        events = []
        core.Pipeline(cfg, b, lambda kind, **d: events.append((kind, d)), threading.Event(),
                      scans=core.new_scans(cfg)[0], identifier=Fake()).run()
        if events[-1][0] != "finished":
            raise RuntimeError(f"run ended with {events[-1]}")
        result = core.finish_batch(cfg, b, lambda *a, **k: None)
        rows = b.csv_path.read_text(encoding="utf-8").splitlines()
        if result["added"] != 3 or len(rows) != 4 or result["broken"] != ["notes.jpg"]:
            raise RuntimeError(f"unexpected result {result}, {len(rows)} CSV lines")
        state["cfg"] = cfg
        return f"3 photos saved, CSV written, 1 unreadable file named"
    check("A whole batch", batch)

    def window():
        if window_module is None:
            return "skipped (no window module)"
        import tkinter as tk
        import wizard_updates
        window_module.core, window_module.updates = core, wizard_updates
        root = tk.Tk()
        root.withdraw()
        try:
            app = window_module.App(root, [])
            if type(app.screen).__name__ != "StartScreen":
                raise RuntimeError("the start screen didn't open")
            app.open_settings(2)
            root.update()
            for w in root.winfo_children():
                if type(w).__name__ == "SettingsDialog":
                    w.destroy()
            root.update()
        finally:
            root.destroy()
        return "start screen and settings open"
    check("Window", window)

    lines.insert(0, f"Scanning Wizard {core.APP_VERSION} self-test: {'PASSED' if not failed else 'FAILED'}")
    text = "\n".join(lines) + "\n"
    if report_path:
        Path(report_path).write_text(text, encoding="utf-8")
    elif sys.stdout:
        sys.stdout.write(text)
    return 0 if not failed else 1
