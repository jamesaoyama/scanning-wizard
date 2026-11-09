"""Scanning Wizard: everything except the window.

Settings, title rules, image handling, the online services and the batch
pipeline live here so they can be tested without a screen. The window is in
scanning_wizard.py.
"""
from __future__ import annotations

import base64
import collections
import csv
import io
import json
import logging
import os
import posixpath
import re
import shutil
import ssl
import sys
import threading
import time
import unicodedata
from datetime import datetime
from ftplib import FTP, FTP_TLS, all_errors as FTP_ERRORS, error_perm
from pathlib import Path

from PIL import Image, ImageOps

try:
    import requests
except ImportError:  # the launcher installs it; this keeps the module importable
    requests = None

log = logging.getLogger("scanning_wizard")

APP_NAME = "Scanning Wizard"
APP_VERSION = "4.0"
USER_AGENT = f"ScanningWizard/{APP_VERSION} (photo listing tool)"
Image.MAX_IMAGE_PIXELS = None  # big scans are our own files, not untrusted uploads


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


YUNET_MODEL = app_dir() / "models" / "face_detection_yunet_2023mar.onnx"
CONFIG_PATH = Path(os.environ.get("SCANNING_WIZARD_CONFIG")
                   or (Path.home() / ".scanning_wizard_config.json"))
LOG_PATH = CONFIG_PATH.with_name(".scanning_wizard.log")
DATA_DIR = "wizard data"

# ─── Listing text ─────────────────────────────────────────────────────────────
# The fixed descriptions, word for word as before. The only change: the SM text
# said "100%25", a web-encoded "%", which showed up literally in listings.

DESC_SO = (
    "This is an 8x10 (20x25cm) original black and white photograph. It was produced by the film "
    "studio during the year the actual movie or television series was in production. This photograph "
    "has been stored in archive filing cabinets since production. The photograph is in excellent vintage "
    "condition. Photographs were produced by the film studios and television networks to publicize their "
    "movies and TV shows and were often distributed to the press and newspapers and magazines "
    "internationally. We ship in special heavy duty photo mailers and you can choose from our low price "
    "USPS First Class Mail delivery or our special low rate 2 Day FedEx service. A unique historical piece "
    "of movie memorabilia from VIP Images, we guarantee your satisfaction.  This is a unique piece of "
    "cinematic history."
)

DESC_SM = (
    "This is a collectible vintage 8x10 inch (20x25cms) real photograph with a glossy finish and printed "
    "on 100% real photographic paper. This vintage collectible is in mint condition and comes from our "
    "photo archive of movie studio, press agency, newspaper and photographer candid photographs. This "
    "photograph was professionally printed in a photo lab in the 1970's-2000's. Photos made in the 1970's "
    "and 1980's typically have a small white border whilst photos from the 1990's-2000's are borderless "
    "(i.e. no white border) which is typical of that era. Movie studios and Television station "
    "broadcasters typically used these photographs for publicity purposes back in the day and they were "
    "distributed to the press and magazines and newspapers world wide. This is a collectible piece of "
    "cinema or television history. We ship in special heavy duty photo mailers and you can choose from "
    "our low price USPS First Class Mail delivery or our special low rate 2 Day FedEx service. A unique "
    "historical piece of movie memorabilia."
)

BATCH_TYPES = ("SM", "SO", "MM")
CSV_FIELDS = ["SKU", "eBay Title", "Title", "PicURL", "Description"]

# ─── Settings ─────────────────────────────────────────────────────────────────

MODEL_DEFAULTS = {
    "gemini_model": "gemini-3.8-flash",
    "openai_model": "gpt-6.1-terra",
    "anthropic_model": "claude-sonnet-5-5",
}

# Saved model names that are retired or about to be, swapped for the defaults on load.
OLD_MODELS = {
    "gemini_model": re.compile(r"^gemini-(1|2)\."),
    "openai_model": re.compile(r"^(gpt-3|gpt-4)"),
    "anthropic_model": re.compile(
        r"^claude-(2|3|instant|sonnet-4-5|sonnet-4-2025|sonnet-4$|opus-4-(0|1|2025)|opus-4$|haiku-3)"),
}

WRITERS = ("gemini", "openai", "claude")
WRITER_LABELS = {"gemini": "Gemini", "openai": "GPT", "claude": "Claude"}

SECRET_KEYS = (
    "ftp_pass", "google_vision_api_key", "aws_access_key_id", "aws_secret_access_key",
    "serpapi_api_key", "gemini_api_key", "openai_api_key", "anthropic_api_key",
)

# Settings from older versions that no longer do anything.
REMOVED_KEYS = (
    "ps_action_set", "ps_action_resize", "ps_action_exif", "ai_provider",
    "deepseek_api_key", "deepseek_model",
)


def desktop_dir() -> Path:
    """The real Desktop, including one that OneDrive has moved."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

            desktop = GUID(0xB4BFCC3A, 0xDB2C, 0x424C,
                           (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41))
            out = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(desktop), 0, None,
                                                          ctypes.byref(out)) == 0:
                try:
                    return Path(out.value)
                finally:
                    ctypes.windll.ole32.CoTaskMemFree(out)
        except Exception:
            log.exception("Couldn't ask Windows where the Desktop is")
    return Path.home() / "Desktop"


def default_config() -> dict:
    desk = desktop_dir()
    return {
        "input_folder": str(desk / "Scanned Images For Wizard"),
        "output_folder": str(desk / "Finished Images Wizard"),
        "batch_type": "SM",
        "low_res_longest_side": 800,
        "straighten": True,
        "after_finish": "recycle",          # recycle | keep
        "upload_mode": "ftp",               # ftp | manual
        "ftp_host": "images.moviestore.com",
        "ftp_user": "",
        "ftp_pass": "",
        "ftp_remote_dir": "/",
        "ftp_security": "auto",             # auto | tls | plain
        "picture_url_template": "https://images.moviestore.com/{batch}/{file}",
        "identify_mode": "thorough",        # thorough | fast
        "ai_order": "gemini,openai,claude",
        "google_vision_api_key": "",
        "aws_access_key_id": "",
        "aws_secret_access_key": "",
        "aws_region": "us-east-1",
        "serpapi_api_key": "",
        "film_check": True,
        "contact_email": "",
        "gemini_api_key": "",
        "openai_api_key": "",
        "anthropic_api_key": "",
        **MODEL_DEFAULTS,
        "max_title_chars": 80,
        "title_suffix_SM": "Vintage 8x10 Inch Photo",
        "title_suffix_SO": "Original 8x10 Inch Photo",
        "title_suffix_MM": "Vintage Photo",
        "description_SM": DESC_SM,
        "description_SO": DESC_SO,
        "description_MM": DESC_SO,
    }


class SecretStore:
    """Keeps keys and passwords in Windows Credential Manager (or the Mac
    Keychain) when the keyring package works, else in the settings file."""

    SERVICE = APP_NAME

    def __init__(self, backend=None):
        self.backend = backend
        if backend is None:
            try:
                import keyring
                keyring.get_password(self.SERVICE, "__check__")
                self.backend = keyring
            except Exception as e:
                log.info("Keys will be kept in the settings file (%s)", e)

    @property
    def secure(self) -> bool:
        return bool(self.backend)

    def get(self, name: str) -> str:
        if not self.backend:
            return ""
        try:
            return self.backend.get_password(self.SERVICE, name) or ""
        except Exception:
            log.exception("Couldn't read %s from the credential store", name)
            return ""

    def set(self, name: str, value: str) -> bool:
        """True if the credential store now holds the value."""
        if not self.backend:
            return False
        if not value:
            try:
                self.backend.delete_password(self.SERVICE, name)
            except Exception:
                pass
            return True
        try:
            self.backend.set_password(self.SERVICE, name, value)
            return True
        except Exception:
            log.exception("Couldn't store %s in the credential store; keeping it in the settings file", name)
            return False


def load_config(store: SecretStore, path: Path | None = None) -> dict:
    path = Path(path or CONFIG_PATH)
    cfg = default_config()
    saved = {}
    if path.is_file():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("Settings file unreadable; using defaults (copy kept as .bak)")
            try:
                shutil.copy2(path, path.with_name(path.name + ".bak"))
            except OSError:
                pass
    if saved and "picture_url_template" not in saved and saved.get("ftp_host"):
        # older versions built links as http://@host/batch/file; eBay needs https
        saved["picture_url_template"] = "https://%s/{batch}/{file}" % saved["ftp_host"].strip()
    for key in REMOVED_KEYS:
        saved.pop(key, None)
    cfg.update({k: v for k, v in saved.items() if k in cfg})
    for key, pattern in OLD_MODELS.items():
        if not cfg.get(key) or pattern.search(str(cfg[key])):
            cfg[key] = MODEL_DEFAULTS[key]
    for key in ("low_res_longest_side", "max_title_chars"):
        try:
            cfg[key] = int(cfg[key])
        except (TypeError, ValueError):
            cfg[key] = default_config()[key]
    moved = False
    for key in SECRET_KEYS:
        in_file = str(saved.get(key) or "")
        if in_file:
            cfg[key] = in_file
            if store.secure and store.set(key, in_file):    # move keys out of the plain file
                moved = True
        else:
            cfg[key] = store.get(key) if store.secure else ""
    if moved:
        save_config(cfg, store, path)
    return cfg


def save_config(cfg: dict, store: SecretStore, path: Path | None = None) -> None:
    path = Path(path or CONFIG_PATH)
    out = dict(cfg)
    for key in SECRET_KEYS:
        if store.secure and store.set(key, str(cfg.get(key) or "")):
            out.pop(key, None)
    write_json(path, out)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def writer_order(cfg: dict) -> list[str]:
    order = [w.strip() for w in str(cfg.get("ai_order", "")).split(",") if w.strip() in WRITERS]
    return order + [w for w in WRITERS if w not in order]


def configured_writers(cfg: dict) -> list[str]:
    keys = {"gemini": "gemini_api_key", "openai": "openai_api_key", "claude": "anthropic_api_key"}
    return [w for w in writer_order(cfg) if cfg.get(keys[w])]


# ─── Titles ───────────────────────────────────────────────────────────────────

_PUNCT = re.compile(r"[,.:;()\[\]\"'!?‘’“”]")
_DASHES = re.compile(r"[-‐-―]")
_SCENE_WORDS = re.compile(
    r"\b(Person|People|Man|Men|Woman|Women|Boy|Boys|Girl|Girls|Lady|Ladies|Gentleman|"
    r"Blonde|Blond|Brunette|Redhead|Two People)\b", re.IGNORECASE)


def clean_text(text) -> str:
    """No punctuation, as before. Dashes become spaces, so "Spider-Man" stays two words."""
    text = str(text or "").strip()
    if text.upper() == "UNKNOWN":
        return ""
    text = _DASHES.sub(" ", text)
    text = _PUNCT.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


_SCENE_EDGES = re.compile(r"^(?:(?:and|with|&|a|an|the|of|two|three|four|young|old)\s+)+|"
                          r"(?:\s+(?:and|with|&|a|an|the|of|two|three|four|young|old))+$", re.IGNORECASE)


def clean_scene(text) -> str:
    """Scene only: drop people and looks words, and the words they leave
    dangling ("boy and girl at dinner" -> "at dinner"). Older versions also ran
    this on movie titles, which turned "Iron Man" into "Iron"."""
    text = re.sub(r"\s+", " ", _SCENE_WORDS.sub("", clean_text(text))).strip()
    if not text:
        return ""
    previous = None
    while previous != text:
        previous = text
        text = _SCENE_EDGES.sub("", text).strip()
        if text.lower() in ("and", "with", "&", "a", "an", "the", "of", "two", "three", "four", "young", "old"):
            text = ""
    return text


def smart_case(text: str) -> str:
    """Capitalise each word but keep McCabe, DiCaprio, ET and MASH as written.
    A whole phrase in capitals (CASABLANCA) is calmed down first."""
    letters = [c for c in text if c.isalpha()]
    shouting = len(letters) >= 4 and all(c.isupper() for c in letters)
    words = []
    for w in text.split():
        if shouting and len(w) > 2 and w.isalnum():    # JAWS -> Jaws; keeps ET and M*A*S*H
            w = w.lower()
        if any(c.isupper() for c in w[1:]):
            words.append(w)
        else:
            words.append(w[:1].upper() + w[1:])
    return " ".join(words)


def clean_year(year) -> str:
    """A single plausible year, else empty (ranges like 1997-2001 are dropped)."""
    year = str(year or "").strip()
    if not year or year.upper() == "UNKNOWN":
        return ""
    if re.search(r"\d{4}\s*[-–]\s*\d{4}", year) or re.fullmatch(r"\d{8}", year):
        return ""
    m = re.search(r"(\d{4})", year)
    if m and 1880 <= int(m.group(1)) <= datetime.now().year + 1:
        return m.group(1)
    return ""


def split_names(actors) -> list[str]:
    if isinstance(actors, (list, tuple)):
        parts = [str(a) for a in actors]
    else:
        parts = re.split(r",|;|\s+&\s+|\s+and\s+", str(actors or ""), flags=re.IGNORECASE)
    out, seen = [], set()
    for p in parts:
        p = re.sub(r"\s+", " ", p).strip()
        if p and p.upper() != "UNKNOWN" and norm_key(p) not in seen:
            seen.add(norm_key(p))
            out.append(p)
    return out


def build_title(suffix: str, movie="", year="", actors=(), scene="", max_chars=80) -> str:
    """[Movie] [Year] [Actors] [Scene] [Suffix], at most max_chars.
    Too long: drop the year, then the scene (keeping the year), then both,
    then all but the first two actors, then all but one, then cut at a word."""
    suffix = (suffix or "").strip()
    max_chars = max(30, min(int(max_chars or 80), 200))
    movie_t = smart_case(clean_text(movie))
    year_t = clean_year(year)
    names = [smart_case(clean_text(n)) for n in split_names(actors)]
    names = [n for n in names if n]
    scene_t = smart_case(clean_scene(scene))

    def make(use_year=True, use_scene=True, n_actors=None):
        parts = [movie_t]
        if use_year:
            parts.append(year_t)
        parts.extend(names if n_actors is None else names[:n_actors])
        if use_scene:
            parts.append(scene_t)
        content = " ".join(p for p in parts if p)
        return f"{content} {suffix}".strip() if content else suffix

    tries = ({}, {"use_year": False}, {"use_scene": False}, {"use_year": False, "use_scene": False},
             {"use_year": False, "use_scene": False, "n_actors": 2},
             {"use_year": False, "use_scene": False, "n_actors": 1})
    title = ""
    for opts in tries:
        title = make(**opts)
        if len(title) <= max_chars:
            return title
    cut = title[:max_chars - 3]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "..."


def norm_key(text) -> str:
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    text = re.sub(r"^(the|a|an)\s+", "", text.lower().strip())
    return re.sub(r"[^a-z0-9]", "", text)


# ─── Images ───────────────────────────────────────────────────────────────────

SCAN_EXTENSIONS = {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".tif", ".tiff", ".bmp", ".webp"}
TURNS = {90: Image.Transpose.ROTATE_90, 180: Image.Transpose.ROTATE_180,
         270: Image.Transpose.ROTATE_270}   # degrees counter-clockwise


def natural_key(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def list_scans(folder) -> list[Path]:
    folder = Path(folder)
    if not folder.is_dir():
        return []
    files = [p for p in folder.iterdir()
             if p.is_file() and p.suffix.lower() in SCAN_EXTENSIONS
             and not p.name.startswith((".", "~$", "__tmp_"))]
    return sorted(files, key=lambda p: natural_key(p.name))


def flatten_for_jpeg(im: Image.Image) -> Image.Image:
    """Any scan mode to something JPEG can hold (RGB, or L for black and white)."""
    if im.mode in ("RGB", "L"):
        return im
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.getchannel("A"))
        return bg
    if im.mode in ("I;16", "I;16B", "I;16L", "I;16N", "I", "F"):
        try:
            import numpy as np
            arr = np.asarray(im).astype("float64")
            top = 65535.0 if im.mode.startswith("I;16") or arr.max() > 255 else 255.0
            return Image.fromarray((arr * (255.0 / top)).clip(0, 255).astype("uint8"), "L")
        except ImportError:
            return im.point(lambda v: v / 256).convert("L")
    if im.mode == "1":
        return im.convert("L")
    return im.convert("RGB")


def jpeg_extras(im: Image.Image, keep_icc=True, keep_exif=True) -> dict:
    """Colour profile, DPI and EXIF to carry into a re-saved JPEG."""
    extras = {}
    icc = im.info.get("icc_profile")
    if icc and keep_icc:
        extras["icc_profile"] = icc
    dpi = im.info.get("dpi")
    if dpi:
        try:
            extras["dpi"] = (int(round(float(dpi[0]))), int(round(float(dpi[1]))))
        except Exception:
            pass
    if keep_exif:
        try:
            exif = im.getexif()
            if exif:
                exif[0x0112] = 1        # orientation is now baked into the pixels
                extras["exif"] = exif.tobytes()
        except Exception:
            pass
    return extras


def write_jpeg(im: Image.Image, dst: Path, quality: int, extras: dict | None = None,
               subsampling=None) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.stem + ".partial.jpg")
    kwargs = dict(extras or {})
    kwargs["quality"] = quality
    if subsampling is not None:
        kwargs["subsampling"] = subsampling
    im.save(tmp, "JPEG", **kwargs)
    os.replace(tmp, dst)


def exif_orientation(im: Image.Image) -> int:
    try:
        return int(im.getexif().get(0x0112, 1) or 1)
    except Exception:
        return 1


def save_high_res(src: Path, dst: Path, fmt: str, turn: int) -> str:
    """A full-size JPEG of the scan, the right way up. A JPEG that needs no
    turning is copied byte for byte (no quality loss). Returns what was done."""
    with Image.open(src) as im:
        orientation = exif_orientation(im)
        if fmt == "JPEG" and not turn and orientation == 1:
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.stem + ".partial.jpg")
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
            return "copied"
        im.load()
        out = ImageOps.exif_transpose(im) if orientation != 1 else im
        if turn:
            out = out.transpose(TURNS[turn])
        # TIFF tags aren't JPEG EXIF, so only JPEG scans keep their EXIF
        extras = jpeg_extras(im, keep_icc=im.mode != "CMYK", keep_exif=fmt == "JPEG")
        write_jpeg(flatten_for_jpeg(out), dst, quality=95, extras=extras, subsampling=0)
        return "converted" if fmt != "JPEG" else "turned"


def save_low_res(src: Path, dst: Path, longest: int) -> tuple[int, int]:
    with Image.open(src) as im:
        im.load()
        icc = im.info.get("icc_profile")
        small = flatten_for_jpeg(im)
        if max(small.size) > longest:
            small = small.copy()
            small.thumbnail((longest, longest), Image.Resampling.LANCZOS)
        write_jpeg(small, dst, quality=90, extras={"icc_profile": icc} if icc else None)
        return small.size


def turn_file(path: Path, degrees_ccw: int) -> None:
    with Image.open(path) as im:
        im.load()
        extras = jpeg_extras(im)
        out = flatten_for_jpeg(im.transpose(TURNS[degrees_ccw]))
    write_jpeg(out, path, quality=95, extras=extras, subsampling=0)


def image_for_api(path: Path, max_px=1024) -> bytes:
    with Image.open(path) as im:
        im.draft("RGB", (max_px, max_px))
        im = ImageOps.exif_transpose(im)
        im = flatten_for_jpeg(im).convert("RGB")
        im.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return buf.getvalue()


class FaceFinder:
    """Works out which way up a photo goes by looking for upright faces at each
    quarter turn. Uses OpenCV's YuNet face model (bundled in models/), or the
    older Haar detector if the model file is missing. Runs on this PC, no API."""

    def __init__(self, model_path: Path | None = None):
        self.kind = None
        try:
            import cv2
            import numpy as np
        except ImportError:
            return
        self.cv2, self.np = cv2, np
        model = Path(model_path or YUNET_MODEL)
        if model.is_file() and hasattr(cv2, "FaceDetectorYN"):
            try:
                self.detector = cv2.FaceDetectorYN.create(str(model), "", (320, 320), 0.6, 0.3, 50)
                self.kind = "yunet"
                return
            except Exception:
                log.exception("YuNet face model wouldn't load")
        try:
            cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
            if not cascade.empty():
                self.detector = cascade
                self.kind = "haar"
        except Exception:
            log.exception("No face detector available")

    def score(self, im: Image.Image) -> float:
        arr = self.np.ascontiguousarray(self.np.asarray(im.convert("RGB"))[:, :, ::-1])
        h, w = arr.shape[:2]
        if self.kind == "yunet":
            self.detector.setInputSize((w, h))
            _, faces = self.detector.detect(arr)
            if faces is None:
                return 0.0
            # YuNet half-sees upside-down faces (about 0.65 against 0.95 upright),
            # so count only how far each face is above the detection threshold.
            small = 0.04 * min(w, h)
            return float(sum(f[14] - 0.6 for f in faces if f[14] >= 0.6 and max(f[2], f[3]) >= small))
        gray = self.cv2.cvtColor(arr, self.cv2.COLOR_BGR2GRAY)
        faces = self.detector.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(30, 30))
        return float(len(faces))

    def best_turn(self, path: Path) -> int:
        """Degrees counter-clockwise that make the faces upright (0 if unsure)."""
        if not self.kind:
            return 0
        with Image.open(path) as im:
            im.draft("RGB", (800, 800))
            im = ImageOps.exif_transpose(im)
            im = flatten_for_jpeg(im).convert("RGB")
            im.thumbnail((640, 640))
        scores = {0: self.score(im)}
        for turn, how in TURNS.items():
            scores[turn] = self.score(im.transpose(how))
        best = max(scores, key=scores.get)
        if not best:
            return 0
        if self.kind == "yunet":
            # a clear face (0.8+) that way up, at least twice as clear as as-scanned
            sure = scores[best] >= 0.2 and scores[best] >= 2 * scores[0] + 0.05
        else:
            sure = scores[best] >= 1 and scores[best] >= scores[0] + 1
        return best if sure else 0


# ─── Batches ──────────────────────────────────────────────────────────────────

class StageError(Exception):
    """A problem that stops the run, with a message for the person at the PC."""


class Batch:
    """One batch (e.g. SM110926): its photos and how far each has got.
    Saved after every step to "wizard data/<batch>.json" in the output folder,
    so closing the window never loses work."""

    def __init__(self, output: Path, data: dict):
        self.output = Path(output)
        self.data = data
        self._lock = threading.RLock()

    # --- where things go
    @property
    def name(self) -> str:
        return self.data["name"]

    @property
    def batch_type(self) -> str:
        return self.data["type"]

    @property
    def items(self) -> list[dict]:
        return self.data["items"]

    @property
    def high_dir(self) -> Path:
        return self.output / f"high res {self.data['year']}" / self.name

    @property
    def low_dir(self) -> Path:
        return self.output / f"low res {self.data['year']}" / self.name

    @property
    def csv_path(self) -> Path:
        return self.output / f"{self.name}.csv"

    @property
    def state_path(self) -> Path:
        return self.output / DATA_DIR / f"{self.name}.json"

    def sku(self, item: dict) -> str:
        if not item.get("number"):
            return item.get("source", "?")
        return f"{self.name}_{item['number']:04d}"

    def high_path(self, item: dict) -> Path:
        return self.high_dir / f"{self.sku(item)}.jpg"

    def low_path(self, item: dict) -> Path:
        return self.low_dir / f"{self.sku(item)}.jpg"

    def source_path(self, item: dict) -> Path:
        return Path(item["source_folder"]) / item["source"]

    # --- loading and saving
    @staticmethod
    def name_for(batch_type: str, when: datetime | None = None) -> str:
        return f"{batch_type}{(when or datetime.now()).strftime('%m%d%y')}"

    @classmethod
    def load(cls, path: Path) -> "Batch":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(Path(path).parent.parent, data)

    @classmethod
    def open_or_create(cls, output, batch_type: str, when: datetime | None = None) -> "Batch":
        when = when or datetime.now()
        name = cls.name_for(batch_type, when)
        path = Path(output) / DATA_DIR / f"{name}.json"
        if path.is_file():
            batch = cls.load(path)
            batch.data["finished"] = False
            return batch
        return cls(Path(output), {"version": 1, "name": name, "type": batch_type,
                                  "year": when.strftime("%Y"), "created": when.isoformat(timespec="seconds"),
                                  "finished": False, "link_check": None, "items": []})

    @classmethod
    def all_batches(cls, output) -> list["Batch"]:
        folder = Path(output) / DATA_DIR
        out = []
        if folder.is_dir():
            for p in sorted(folder.glob("*.json")):
                try:
                    out.append(cls.load(p))
                except Exception:
                    log.exception("Couldn't read %s", p)
        return out

    @classmethod
    def unfinished(cls, output) -> list["Batch"]:
        """Batches with photos still to check, or whose Save was interrupted."""
        return [b for b in cls.all_batches(output)
                if not b.data.get("finished") and (b.open_items() or b.data.get("finish_started"))]

    def save(self) -> None:
        with self._lock:
            write_json(self.state_path, self.data)

    # --- counts
    def open_items(self) -> list[dict]:
        """Photos not yet written to the CSV (and not unreadable)."""
        return [i for i in self.items if not i.get("saved") and not i.get("error")]

    def summary(self) -> str:
        items = self.open_items()
        if not items and self.data.get("finish_started"):
            return "saved, but clearing the scans folder didn't finish"
        check = sum(1 for i in items if i.get("needs_check"))
        waiting = sum(1 for i in items if not i.get("identified"))
        parts = [f"{len(items)} photo{'s' if len(items) != 1 else ''}"]
        if waiting:
            parts.append(f"{waiting} not finished")
        elif check:
            parts.append(f"{check} to check")
        return ", ".join(parts)


def find_last_number_in_folder(folder: Path, prefix: str) -> int:
    best = 0
    if Path(folder).is_dir():
        for f in os.listdir(folder):
            m = re.match(rf"{re.escape(prefix)}[_-](\d+)", f, re.IGNORECASE)
            if m:
                best = max(best, int(m.group(1)))
    return best


def find_last_number_in_csv(csv_path: Path, prefix: str) -> int:
    best = 0
    if Path(csv_path).is_file():
        try:
            with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    m = re.search(rf"{re.escape(prefix)}_(\d+)$", row.get("SKU", ""))
                    if m:
                        best = max(best, int(m.group(1)))
        except Exception:
            log.exception("Couldn't read %s", csv_path)
    return best


def next_number(batch: Batch) -> int:
    in_state = max((i["number"] for i in batch.items if i.get("number")), default=0)
    return max(in_state, find_last_number_in_folder(batch.high_dir, batch.name),
               find_last_number_in_csv(batch.csv_path, batch.name)) + 1


def known_scans(output) -> set:
    """(folder, file name, size) of every scan any batch already has, unless it
    was moved out of the scans folder afterwards."""
    seen = set()
    for b in Batch.all_batches(output):
        for i in b.items:
            if not i.get("recycled"):
                seen.add((os.path.normcase(i["source_folder"]), i["source"], i.get("source_size")))
    return seen


def new_scans(cfg: dict) -> tuple[list[Path], int]:
    """Scans in the scans folder that no batch has yet, and how many it already has."""
    folder = Path(cfg["input_folder"])
    seen = known_scans(cfg["output_folder"])
    fresh, done = [], 0
    for p in list_scans(folder):
        try:
            key = (os.path.normcase(str(folder)), p.name, p.stat().st_size)
        except OSError:
            continue
        if key in seen:
            done += 1
        else:
            fresh.append(p)
    return fresh, done


def broken_scans(cfg: dict) -> list[str]:
    """Files still in the scans folder that a batch couldn't open."""
    folder = os.path.normcase(str(Path(cfg["input_folder"])))
    out = []
    for b in Batch.all_batches(cfg["output_folder"]):
        for i in b.items:
            if i.get("error") and os.path.normcase(i["source_folder"]) == folder \
                    and (Path(i["source_folder"]) / i["source"]).is_file():
                out.append(i["source"])
    return sorted(set(out), key=natural_key)


def add_scans(batch: Batch, scans: list[Path]) -> int:
    """Give each new scan the next number. Files that aren't pictures are
    recorded (so they're named on screen) but don't use up a number."""
    n = next_number(batch)
    for p in scans:
        st = p.stat()
        item = {"number": None, "source": p.name, "source_folder": str(p.parent),
                "source_size": st.st_size, "source_mtime": st.st_mtime, "include": True}
        try:
            with Image.open(p):
                pass
            item["number"] = n
            n += 1
        except Exception as e:
            item["error"] = f"Can't open this file ({e.__class__.__name__})"
        batch.items.append(item)
    batch.save()
    return len(scans)


def scan_unchanged(item: dict, path: Path) -> bool:
    """Is the file in the scans folder still the one that was processed?"""
    try:
        st = path.stat()
    except OSError:
        return False
    if st.st_size != item.get("source_size"):
        return False
    mtime = item.get("source_mtime")
    return mtime is None or abs(st.st_mtime - mtime) < 2


# ─── Picture links and FTP ────────────────────────────────────────────────────

def make_url(cfg: dict, batch_name: str, file_name: str) -> str:
    template = cfg.get("picture_url_template") or "https://{host}/{batch}/{file}"
    return (template.replace("{host}", cfg.get("ftp_host", "").strip())
            .replace("{batch}", batch_name).replace("{file}", file_name))


def http(contact: str = "") -> "requests.Session":
    s = requests.Session()
    s.headers["User-Agent"] = f"{USER_AGENT[:-1]}; {contact})" if contact else USER_AGENT
    return s


def check_link(url: str, session=None) -> tuple[bool, str]:
    """Does this picture link open over https? eBay refuses http links."""
    if not url.lower().startswith("https://"):
        return False, "Picture links start with http://. eBay only shows pictures from https:// links."
    s = session or http()
    try:
        r = s.head(url, timeout=15, allow_redirects=True)
        if r.status_code in (403, 405, 501):
            r = s.get(url, timeout=15, stream=True)
        kind = r.headers.get("Content-Type", "")
        if r.status_code == 200 and kind.startswith("image/"):
            return True, "Picture links work."
        if r.status_code == 200:
            return False, f"The link opens, but not as a picture ({kind or 'unknown type'})."
        return False, f"The picture link didn't open (error {r.status_code})."
    except requests.exceptions.SSLError:
        return False, "The picture server doesn't have working https. eBay won't show these pictures."
    except Exception as e:
        return False, f"Couldn't reach the picture link: {e}"


def _host_port(host: str) -> tuple[str, int]:
    host = host.strip()
    if host.count(":") == 1:
        h, p = host.split(":")
        if p.isdigit():
            return h, int(p)
    return host, 21


def ftp_open(cfg: dict) -> tuple[FTP, bool]:
    """Log in, using encrypted FTP when the server supports it. Returns
    (connection, secure). In "auto", servers without encryption still work."""
    host, port = _host_port(cfg.get("ftp_host", ""))
    user, pw = cfg.get("ftp_user", ""), cfg.get("ftp_pass", "")
    if not host or not user:
        raise StageError("Uploading isn't set up: add the FTP username in Settings > Upload, "
                         "or choose \"I upload the pictures myself\".")
    security = cfg.get("ftp_security", "auto")
    if security in ("auto", "tls"):
        ftps = FTP_TLS(timeout=30)
        try:
            ftps.connect(host, port)
            ftps.auth()
            secure = True
        except (error_perm, ssl.SSLError, OSError, EOFError) as e:
            try:
                ftps.close()
            except Exception:
                pass
            if security == "tls":
                raise StageError(f"{host} doesn't accept secure FTP: {e}")
            secure = False
        if secure:
            try:
                ftps.login(user, pw)
            except error_perm as e:
                ftps.close()
                raise StageError(f"Couldn't log in to {host}: {e}")
            try:
                ftps.prot_p()          # encrypt the files too, where the server allows it
            except error_perm:
                pass
            return ftps, True
    ftp = FTP(timeout=30)
    try:
        ftp.connect(host, port)
        ftp.login(user, pw)
    except error_perm as e:
        raise StageError(f"Couldn't log in to {host}: {e}")
    except FTP_ERRORS as e:
        raise StageError(f"Couldn't connect to {host}: {e}")
    return ftp, False


def ftp_enter(ftp: FTP, base: str, batch_name: str) -> None:
    """Go to <base>/<batch> on the server, creating any folder that's missing."""
    base = (base or "/").replace("\\", "/")
    path = posixpath.join("/" + base.strip("/"), batch_name)
    try:
        ftp.cwd(path)
        return
    except error_perm:
        pass
    ftp.cwd("/")
    for part in [p for p in path.split("/") if p]:
        try:
            ftp.cwd(part)
        except error_perm:
            try:
                ftp.mkd(part)
                ftp.cwd(part)
            except error_perm as e:
                raise StageError(f"Couldn't make the folder {path} on the server: {e}")


# ─── Online services ──────────────────────────────────────────────────────────

class ProviderError(Exception):
    def __init__(self, message, fatal=False):
        super().__init__(message)
        self.fatal = fatal       # bad key or model: don't try this service again this run


def _post_json(session, url, body, headers, timeout=120, retries=2):
    """POST with a couple of retries for busy servers."""
    for attempt in range(retries + 1):
        try:
            r = session.post(url, json=body, headers=headers, timeout=timeout)
        except requests.exceptions.RequestException as e:
            if attempt < retries:
                time.sleep(2 + 3 * attempt)
                continue
            raise ProviderError(f"network problem: {e}")
        if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            time.sleep(3 + 5 * attempt)
            continue
        return r
    return r


def _error_text(r) -> str:
    try:
        err = r.json().get("error", {})
        if isinstance(err, dict):
            return err.get("message") or json.dumps(err)[:300]
        return str(err)[:300]
    except Exception:
        return r.text[:300]


def _raise_for(r, service):
    msg = _error_text(r)
    fatal = r.status_code in (401, 403, 404) or "api key" in msg.lower()
    raise ProviderError(f"{service} error {r.status_code}: {msg}", fatal=fatal)


def google_vision_web_detect(key: str, image_b64: str, session=None) -> dict:
    s = session or http()
    r = _post_json(s, "https://vision.googleapis.com/v1/images:annotate",
                   {"requests": [{"image": {"content": image_b64},
                                  "features": [{"type": "WEB_DETECTION", "maxResults": 10}]}]},
                   {"x-goog-api-key": key}, timeout=60)
    if r.status_code != 200:
        _raise_for(r, "Google Vision")
    web = r.json().get("responses", [{}])[0].get("webDetection", {})
    entities = [{"name": e.get("description", ""), "score": e.get("score", 0)}
                for e in web.get("webEntities", []) if e.get("description")]
    pages = [{"title": p.get("pageTitle", ""), "url": p.get("url", "")}
             for p in web.get("pagesWithMatchingImages", []) if p.get("pageTitle") or p.get("url")]
    return {"entities": entities, "pages": pages}


_GENERIC = {
    "photograph", "photo", "photography", "image", "picture", "film", "movie", "still", "scene",
    "vintage", "black and white", "portrait", "monochrome", "actor", "actress", "celebrity",
    "stock photography", "stock photo", "sitting", "standing", "posing", "smiling", "looking",
    "walking", "photo shoot", "photoshoot", "fashion", "model", "modeling", "vintage clothing",
    "clothing", "dress", "suit", "costume", "blond", "blonde", "brunette", "redhead", "hair",
    "man", "woman", "girl", "boy", "lady", "gentleman", "person", "people", "beauty", "glamour",
    "glamor", "sexy", "beautiful", "handsome", "studio", "indoor", "outdoor", "room", "chair",
    "sofa", "entertainment", "performing arts", "visual arts", "art", "retro", "classic", "old",
    "antique", "nostalgia", "hollywood", "cinema", "television", "tv", "screen", "publicity",
    "press", "headshot", "head shot", "snapshot", "candid", "paparazzi",
}
_MOVIE_SITES = ("imdb.com", "themoviedb.org", "rottentomatoes.com", "letterboxd.com",
                "moviestillsdb.com", "allmovie.com")


def parse_vision(vision: dict) -> dict:
    """Movie and year only from film-site page titles ("Name (1988) - IMDb");
    a person's name from web entities that look like names."""
    out = {"movie": "", "year": "", "actor": "", "score": 0.0, "page_titles": []}
    for page in vision.get("pages", []):
        title, url = page.get("title", ""), page.get("url", "").lower()
        if title:
            out["page_titles"].append(re.sub(r"<[^>]+>", "", title)[:120])
        if not out["movie"] and title and any(d in url for d in _MOVIE_SITES):
            m = re.search(r"(.+?)\s*\((?:TV Series |TV Movie )?(\d{4})", re.sub(r"<[^>]+>", "", title))
            if m:
                cand = re.sub(r"\s*[-–|]\s*(IMDb|TMDB|Photo|Photos|Gallery).*$", "",
                              m.group(1), flags=re.IGNORECASE).strip()
                if cand and cand.lower() not in _GENERIC:
                    out["movie"], out["year"] = cand, m.group(2)
    for e in vision.get("entities", []):
        name, score = e.get("name", "").strip(), float(e.get("score", 0) or 0)
        if name.lower() in _GENERIC or len(name) < 3:
            continue
        if re.fullmatch(r"(19|20)\d{2}", name):
            out["year"] = out["year"] or name
            continue
        out["score"] = max(out["score"], score)
        words = name.split()
        if (not out["actor"] and len(words) >= 2 and all(w[:1].isupper() for w in words)
                and not any(c.isdigit() for c in name)):
            out["actor"] = name
    return out


def rekognition_celebrities(key_id, secret, region, image_bytes, client=None) -> list[dict]:
    if client is None:
        try:
            import boto3
        except ImportError:
            raise ProviderError("Amazon needs the boto3 package", fatal=True)
        client = boto3.client("rekognition", aws_access_key_id=key_id,
                              aws_secret_access_key=secret, region_name=region or "us-east-1")
    try:
        resp = client.recognize_celebrities(Image={"Bytes": image_bytes})
    except Exception as e:
        text = str(e)
        fatal = any(w in text for w in ("UnrecognizedClient", "InvalidSignature", "AccessDenied",
                                        "SignatureDoesNotMatch", "security token"))
        raise ProviderError(f"Amazon error: {text[:200]}", fatal=fatal)
    return [{"name": c.get("Name", ""), "confidence": float(c.get("MatchConfidence", 0))}
            for c in resp.get("CelebrityFaces", []) if c.get("Name")]


def lens_matches(api_key: str, image_url: str, session=None) -> list[str]:
    """Titles of pages showing the same or a similar picture (Google Lens via SerpApi).
    Needs the picture at a public link, so it runs after uploading."""
    s = session or http()
    try:
        r = s.get("https://serpapi.com/search",
                  params={"engine": "google_lens", "url": image_url, "type": "visual_matches",
                          "api_key": api_key, "hl": "en"}, timeout=60)
    except requests.exceptions.RequestException as e:
        raise ProviderError(f"Google Lens: network problem: {e}")
    if r.status_code != 200:
        _raise_for(r, "Google Lens")
    data = r.json()
    if data.get("error"):
        raise ProviderError(f"Google Lens: {data['error']}", fatal="api key" in data["error"].lower())
    return [m.get("title", "")[:120] for m in data.get("visual_matches", [])[:8] if m.get("title")]


# --- AI title writers

LISTING_SCHEMA = {
    "type": "object",
    "properties": {
        "movie_title": {"type": "string"},
        "year": {"type": "string"},
        "actors": {"type": "array", "items": {"type": "string"}},
        "scene_description": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "clues": {"type": "string"},
    },
    "required": ["movie_title", "year", "actors", "scene_description", "confidence", "clues"],
    "additionalProperties": False,
}

PROMPT_RULES = """Answer with these fields:
- movie_title: the film or TV show title, or "" if you can't tell.
- year: the single year the film came out or the show aired, as 4 digits, or "" if unsure. Never a range.
- actors: full names of the people shown, most prominent first. Use [] if you don't know. Don't guess names.
- scene_description: the setting or action only, under 25 characters. No names, no gender words (man, woman, boy, girl), no looks (blonde, brunette).
- confidence: "high" only if text on the photo or at least two of the clues agree with your answer, otherwise "medium" or "low".
- clues: a few words on what you relied on."""


def make_prompt(clues: list[str], guesses: list[str]) -> str:
    lines = ["You are an expert at identifying vintage movie and TV photographs that are being listed for sale.",
             "Work out which film or TV show this photograph is from and who is in it. Look at costumes, "
             "sets, lighting, era, faces, and any printed captions, stamps or text.", ""]
    if clues:
        lines += ["Clues from other services (they can be wrong; use them only where the photo agrees):"]
        lines += [f"- {c}" for c in clues] + [""]
    if guesses:
        lines += ["Earlier answers for this photo (check them, and correct anything wrong):"]
        lines += [f"- {g}" for g in guesses] + [""]
    lines.append(PROMPT_RULES)
    return "\n".join(lines)


def parse_json_text(text: str):
    """JSON from a reply; tolerant of code fences and stray text."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e > s:
        try:
            return json.loads(text[s:e + 1])
        except Exception:
            pass
    return None


class Writer:
    name = ""
    label = ""
    key_field = ""
    model_field = ""

    def __init__(self, cfg: dict, session=None):
        self.key = cfg.get(self.key_field, "")
        self.model = cfg.get(self.model_field) or MODEL_DEFAULTS[self.model_field]
        self.session = session

    def ask(self, image_b64: str, prompt: str):
        """A dict matching LISTING_SCHEMA, None if the model declined, or ProviderError."""
        raise NotImplementedError


class GeminiWriter(Writer):
    name, label, key_field, model_field = "gemini", "Gemini", "gemini_api_key", "gemini_model"

    def __init__(self, cfg, session=None):
        super().__init__(cfg, session)
        self.level = 0   # 0: schema + low thinking, 1: schema only, 2: plain JSON

    def body(self, image_b64, prompt):
        gen = {"responseMimeType": "application/json", "maxOutputTokens": 4096}
        if self.level <= 1:
            gen["responseJsonSchema"] = LISTING_SCHEMA
        if self.level == 0:
            gen["thinkingConfig"] = {"thinkingLevel": "low"}
        return {"contents": [{"role": "user", "parts": [
            {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}}, {"text": prompt}]}],
            "generationConfig": gen}

    def ask(self, image_b64, prompt):
        s = self.session or http()
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        while True:
            r = _post_json(s, url, self.body(image_b64, prompt), {"x-goog-api-key": self.key})
            if r.status_code == 400 and self.level < 2:
                msg = _error_text(r).lower()
                if "thinking" in msg:
                    self.level = 1
                    continue
                if any(w in msg for w in ("schema", "unknown name", "invalid json payload")):
                    self.level = 2
                    continue
            if r.status_code != 200:
                _raise_for(r, "Gemini")
            data = r.json()
            cands = data.get("candidates") or []
            if not cands:
                if data.get("promptFeedback", {}).get("blockReason"):
                    return None
                raise ProviderError("Gemini sent back an empty answer")
            cand = cands[0]
            if cand.get("finishReason") in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "RECITATION"):
                return None
            text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", [])
                           if not p.get("thought"))
            if cand.get("finishReason") == "MAX_TOKENS" and not text.strip().endswith("}"):
                raise ProviderError("Gemini's answer was cut off")
            out = parse_json_text(text)
            if not isinstance(out, dict):
                raise ProviderError("Gemini's answer wasn't readable")
            return out


class OpenAIWriter(Writer):
    name, label, key_field, model_field = "openai", "GPT", "openai_api_key", "openai_model"

    def __init__(self, cfg, session=None):
        super().__init__(cfg, session)
        self.use_reasoning = True
        self.use_schema = True

    def body(self, image_b64, prompt):
        body = {"model": self.model, "max_output_tokens": 4096,
                "input": [{"role": "user", "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": f"data:image/jpeg;base64,{image_b64}",
                     "detail": "auto"}]}]}
        if self.use_schema:
            body["text"] = {"format": {"type": "json_schema", "name": "photo_listing",
                                       "schema": LISTING_SCHEMA, "strict": True}}
        if self.use_reasoning:
            body["reasoning"] = {"effort": "low"}
        return body

    def ask(self, image_b64, prompt):
        s = self.session or http()
        while True:
            r = _post_json(s, "https://api.openai.com/v1/responses", self.body(image_b64, prompt),
                           {"Authorization": f"Bearer {self.key}"})
            if r.status_code == 400:
                msg = _error_text(r).lower()
                if self.use_reasoning and "reasoning" in msg:
                    self.use_reasoning = False
                    continue
                if self.use_schema and ("json_schema" in msg or "text.format" in msg):
                    self.use_schema = False
                    continue
            if r.status_code != 200:
                _raise_for(r, "GPT")
            data = r.json()
            if data.get("status") == "incomplete":
                reason = (data.get("incomplete_details") or {}).get("reason", "")
                if reason == "content_filter":
                    return None
                raise ProviderError(f"GPT's answer was cut off ({reason})")
            for item in data.get("output", []):
                if item.get("type") != "message":
                    continue
                for part in item.get("content", []):
                    if part.get("type") == "refusal":
                        return None
                    if part.get("type") == "output_text":
                        out = parse_json_text(part.get("text", ""))
                        if isinstance(out, dict):
                            return out
            raise ProviderError("GPT's answer wasn't readable")


# Models that accept the server-side fallback for refused requests.
CLAUDE_FALLBACK_MODELS = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1")


class ClaudeWriter(Writer):
    """Note: Claude is built not to name people from their faces. It still reads
    captions and recognises films from sets and costumes, and it can confirm
    names that Amazon or the web search found."""
    name, label, key_field, model_field = "claude", "Claude", "anthropic_api_key", "anthropic_model"

    def __init__(self, cfg, session=None, client=None):
        super().__init__(cfg, session)
        self.client = client
        self.use_effort = True

    def _client(self):
        if self.client is None:
            try:
                import anthropic
            except ImportError:
                raise ProviderError("Claude needs the 'anthropic' package (Python 3.10 or newer)", fatal=True)
            self.client = anthropic.Anthropic(api_key=self.key, max_retries=2, timeout=120.0)
        return self.client

    def ask(self, image_b64, prompt):
        client = self._client()
        import anthropic
        output_config = {"format": {"type": "json_schema", "schema": LISTING_SCHEMA}}
        if self.use_effort:
            output_config["effort"] = "low"
        params = dict(model=self.model, max_tokens=4096, output_config=output_config, messages=[{
            "role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                {"type": "text", "text": prompt}]}])
        try:
            if self.model in CLAUDE_FALLBACK_MODELS:
                # If a safety check declines the request, the API retries it on
                # another Claude model instead of returning nothing.
                resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"],
                                                   fallbacks="default", **params)
            else:
                resp = client.messages.create(**params)
        except anthropic.BadRequestError as e:
            if self.use_effort and "effort" in str(e).lower():
                self.use_effort = False
                return self.ask(image_b64, prompt)
            raise ProviderError(f"Claude: {e.message}")
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise ProviderError(f"Claude key problem: {e.message}", fatal=True)
        except anthropic.NotFoundError as e:
            raise ProviderError(f"Claude model '{self.model}' not found: {e.message}", fatal=True)
        except anthropic.APIStatusError as e:
            raise ProviderError(f"Claude error {e.status_code}: {e.message}")
        except anthropic.APIConnectionError as e:
            raise ProviderError(f"Claude: network problem: {e}")
        if resp.stop_reason == "refusal":
            return None
        if resp.stop_reason == "max_tokens":
            raise ProviderError("Claude's answer was cut off")
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
        out = parse_json_text(text)
        if not isinstance(out, dict):
            raise ProviderError("Claude's answer wasn't readable")
        return out


WRITER_CLASSES = {"gemini": GeminiWriter, "openai": OpenAIWriter, "claude": ClaudeWriter}


# --- Film database check (Wikidata: free, no key, fine for business use)

SCREEN_TYPES = {
    "Q11424",     # film
    "Q506240",    # television film
    "Q5398426",   # television series
    "Q202866",    # animated film
    "Q24862",     # short film
    "Q1259759",   # miniseries
    "Q581714",    # animated series
    "Q15416",     # television program
    "Q63952888",  # anime television series
    "Q24856",     # film series
}


def _claim_ids(entity, prop):
    out = []
    for c in entity.get("claims", {}).get(prop, []):
        v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(v, dict) and v.get("id"):
            out.append(v["id"])
    return out


def _label(entity) -> str:
    labels = entity.get("labels", {})
    return (labels.get("en") or labels.get("mul") or {}).get("value", "")


def _aliases(entity) -> list[str]:
    al = entity.get("aliases", {})
    return [a.get("value", "") for a in al.get("en", []) + al.get("mul", [])]


def _claim_years(entity, prop):
    years = []
    for c in entity.get("claims", {}).get(prop, []):
        v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(v, dict) and isinstance(v.get("time"), str):
            m = re.match(r"[+-](\d{4})", v["time"])
            if m:
                years.append(int(m.group(1)))
    return years


class FilmChecker:
    API = "https://www.wikidata.org/w/api.php"

    def __init__(self, session=None, contact=""):
        self.session = session
        self.contact = contact          # Wikidata asks programs to give a contact
        self.cache = {}
        self.last_error = None

    GAP = 0.5      # seconds between requests: Wikidata throttles quick bursts

    def _get(self, **params):
        s = self.session or http(self.contact)
        params["format"] = "json"
        for attempt in range(4):
            wait = getattr(self, "_last", 0) + self.GAP - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            r = s.get(self.API, params=params, timeout=20)
            if r.status_code in (429, 503) and attempt < 3:      # busy: wait as asked, then retry
                try:
                    wait = float(r.headers.get("Retry-After", 0))
                except ValueError:
                    wait = 0
                time.sleep(min(max(wait, 2 * (attempt + 1)), 15))
                continue
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                raise RuntimeError(data["error"].get("info", "film database error"))
            return data
        r.raise_for_status()
        return r.json()

    def check(self, title: str, year: str = "", actors=()) -> dict | None:
        """The film or show in the film database: its title, year range and which
        of the given actors are in its cast. When several share the title (there
        are two films called Rain Man), the one with these actors wins, then the
        closest year, then the best known. None if not found (or offline)."""
        key = norm_key(title)
        if not key:
            return None
        try:
            if key not in self.cache:
                self.cache[key] = self._lookup(title)
            films = self.cache[key]
        except Exception as e:
            log.warning("Film check failed for %r: %s", title, e)
            self.last_error = str(e)
            return None
        self.last_error = None
        if not films:
            return None
        names = split_names(actors)
        y = int(year) if str(year).isdigit() else None

        def matched(film):
            cast = {norm_key(n) for n in film["cast"]}
            return [a for a in names if norm_key(a) in cast]

        def rank(film):
            gap = abs(film["first_year"] - y) if y and film["first_year"] else 0
            return (len(matched(film)), -gap, film["fame"])

        best = max(films, key=rank)
        return {**best, "matched": matched(best)}

    def _lookup(self, title):
        # Search films and TV shows only ("Vertigo" is also an illness); else a plain search.
        words = re.sub(r"[^\w\s']", " ", title).strip()
        types = "|".join(f"P31={q}" for q in sorted(SCREEN_TYPES))
        hits = self._get(action="query", list="search", srsearch=f"{words} haswbstatement:{types}",
                         srlimit=10).get("query", {}).get("search", [])
        ids = [h["title"] for h in hits if re.fullmatch(r"Q\d+", h.get("title", ""))]
        if not ids:
            found = self._get(action="wbsearchentities", search=title, language="en", uselang="en",
                              type="item", limit=12).get("search", [])
            ids = [f["id"] for f in found]
        if not ids:
            return []
        ents = self._get(action="wbgetentities", ids="|".join(ids), props="labels|aliases|claims|sitelinks",
                         languages="en|mul").get("entities", {})
        want = norm_key(title)
        scored = []
        for qid in ids:
            e = ents.get(qid, {})
            if not SCREEN_TYPES.intersection(_claim_ids(e, "P31")):
                continue
            label, aliases = _label(e), _aliases(e)
            score = 3 if norm_key(label) == want else 2 if any(norm_key(a) == want for a in aliases) else \
                1 if want and want in norm_key(label) else 0
            if score:
                scored.append((score, len(e.get("sitelinks", {})), qid, e, label))
        if not scored:
            return []
        top = max(s[0] for s in scored)
        picks = sorted([s for s in scored if s[0] == top], key=lambda s: -s[1])[:5]
        cast_ids = {qid: _claim_ids(e, "P161")[:60] for _, _, qid, e, _ in picks}
        people = {}
        wanted = [pid for ids_ in cast_ids.values() for pid in ids_]
        for start in range(0, len(wanted), 50):
            chunk = wanted[start:start + 50]
            people.update(self._get(action="wbgetentities", ids="|".join(chunk), props="labels|aliases",
                                    languages="en|mul").get("entities", {}))
        films = []
        for _, fame, qid, e, label in picks:
            cast = []
            for pid in cast_ids[qid]:
                p = people.get(pid, {})
                cast += [_label(p)] + _aliases(p)
            years = sorted(set(_claim_years(e, "P577") + _claim_years(e, "P580")))
            end = _claim_years(e, "P582")
            films.append({"id": qid, "title": label, "first_year": years[0] if years else None,
                          "last_year": max(end) if end else None, "cast": [c for c in cast if c],
                          "fame": fame})
        return films


# --- Putting it together

class Identifier:
    """Finds movie, year, actors and scene for one photo.

    1. Evidence: Google Vision web search (film sites name the movie), Google
       Lens similar pictures, and Amazon's celebrity face match.
    2. AI writers in your chosen order, each shown the evidence and the earlier
       answers, until two sources agree (Thorough) or both movie and actors are
       found (Fast).
    3. Film database check: fixes the year and confirms the cast.
    """

    def __init__(self, cfg: dict, log_fn=None, stop: threading.Event | None = None,
                 session=None, writers=None, film=None, rekognition_client=None):
        self.cfg = cfg
        self.log = log_fn or (lambda text, level="info": log.info(text))
        self.stop = stop or threading.Event()
        self.session = session
        self.writers = writers if writers is not None else [
            WRITER_CLASSES[w](cfg, session) for w in configured_writers(cfg)]
        self.film = film if film is not None else (
            FilmChecker(session, cfg.get("contact_email", "")) if cfg.get("film_check") else None)
        self.rek_client = rekognition_client
        self.disabled = set()     # services with a bad key or model this run

    def has_any_service(self) -> bool:
        c = self.cfg
        return bool(self.writers or c.get("google_vision_api_key")
                    or (c.get("aws_access_key_id") and c.get("aws_secret_access_key")))

    def _service(self, name, fn, *args):
        if name in self.disabled:
            return None
        try:
            return fn(*args)
        except ProviderError as e:
            if e.fatal:
                self.disabled.add(name)
                self.log(f"{e} (skipping {name} for the rest of this batch)", "error")
            else:
                self.log(str(e), "warn")
            return None

    def identify(self, image_path: Path, public_url: str | None = None) -> dict:
        cfg = self.cfg
        jpeg = image_for_api(image_path)
        b64 = base64.b64encode(jpeg).decode("ascii")
        sources, warnings, clues = [], [], []
        trusted_movie, trusted_year, face_names, web_actor = "", "", [], ""

        if cfg.get("google_vision_api_key"):
            v = self._service("Google Vision", google_vision_web_detect, cfg["google_vision_api_key"], b64,
                              self.session)
            if v:
                p = parse_vision(v)
                trusted_movie, trusted_year, web_actor = p["movie"], p["year"], p["actor"]
                if trusted_movie:
                    clues.append(f"A film website shows this photo as: {trusted_movie} ({trusted_year})")
                    sources.append("Google")
                if p["page_titles"]:
                    clues.append("Web pages with this photo: " + "; ".join(p["page_titles"][:6]))
                if web_actor:
                    clues.append(f"Web search suggests the person may be: {web_actor}")

        if cfg.get("serpapi_api_key") and public_url:
            titles = self._service("Google Lens", lens_matches, cfg["serpapi_api_key"], public_url,
                                   self.session)
            if titles:
                clues.append("Similar pictures online are titled: " + "; ".join(titles))
                sources.append("Lens")

        if cfg.get("aws_access_key_id") and cfg.get("aws_secret_access_key"):
            if self.rek_client is None and "Amazon" not in self.disabled:
                try:
                    import boto3
                    self.rek_client = boto3.client(
                        "rekognition", aws_access_key_id=cfg["aws_access_key_id"],
                        aws_secret_access_key=cfg["aws_secret_access_key"],
                        region_name=cfg.get("aws_region") or "us-east-1")
                except ImportError:
                    self.disabled.add("Amazon")
                    self.log("Amazon needs the boto3 package; skipping it", "error")
            celebs = self._service("Amazon", rekognition_celebrities, cfg["aws_access_key_id"],
                                   cfg["aws_secret_access_key"], cfg.get("aws_region"), jpeg, self.rek_client)
            if celebs:
                face_names = [c["name"] for c in celebs if c["confidence"] >= 80]
                clues.append("Face match: " + ", ".join(f"{c['name']} ({c['confidence']:.0f}%)" for c in celebs))
                if face_names:
                    sources.append("Amazon")

        # AI writers
        movie_votes, name_votes = collections.Counter(), collections.Counter()
        first_seen = {}
        if trusted_movie:
            movie_votes[norm_key(trusted_movie)] += 2
            first_seen.setdefault(norm_key(trusted_movie), trusted_movie)
        for n in face_names:
            name_votes[norm_key(n)] += 2
            first_seen.setdefault(norm_key(n), n)
        if web_actor:
            name_votes[norm_key(web_actor)] += 1
            first_seen.setdefault(norm_key(web_actor), web_actor)
        answers, guesses = [], []
        thorough = cfg.get("identify_mode", "thorough") != "fast"
        sequence = (self.writers * 3)[:(5 if thorough else 3)]

        def confirmed():
            # always ask at least one AI: it writes the scene and double-checks the clues
            if not answers or not movie_votes or not name_votes:
                return False
            if not thorough:
                return True
            return movie_votes.most_common(1)[0][1] >= 2 and name_votes.most_common(1)[0][1] >= 2

        for writer in sequence:
            if self.stop.is_set() or writer.name in self.disabled:
                continue
            if confirmed():
                break
            try:
                data = writer.ask(b64, make_prompt(clues, guesses))
            except ProviderError as e:
                if e.fatal:
                    self.disabled.add(writer.name)
                    self.log(f"{e} (skipping {writer.label} for the rest of this batch)", "error")
                else:
                    self.log(f"{writer.label}: {e}", "warn")
                continue
            if data is None:
                self.log(f"{writer.label} declined this photo", "warn")
                continue
            movie = str(data.get("movie_title") or "").strip()
            movie = "" if movie.upper() == "UNKNOWN" else movie
            names = split_names(data.get("actors") or [])
            answer = {"by": writer.label, "movie": movie, "year": clean_year(data.get("year")),
                      "actors": names, "scene": str(data.get("scene_description") or "").strip(),
                      "confidence": str(data.get("confidence") or "low"), "clues": str(data.get("clues") or "")}
            answers.append(answer)
            if movie:
                movie_votes[norm_key(movie)] += 1
                first_seen.setdefault(norm_key(movie), movie)
            for n in names:
                name_votes[norm_key(n)] += 1
                first_seen.setdefault(norm_key(n), n)
            guesses.append(f"{writer.label}: {movie or 'unknown film'}"
                           f"{' (' + answer['year'] + ')' if answer['year'] else ''}"
                           f" with {', '.join(names) or 'unknown actors'}")
            if writer.label not in sources:
                sources.append(writer.label)

        # Merge: trusted evidence first, then the answers that agree most
        best_movie_key = movie_votes.most_common(1)[0][0] if movie_votes else ""
        movie = first_seen.get(best_movie_key, "")
        agreeing = [a for a in answers if norm_key(a["movie"]) == best_movie_key] or answers
        year = trusted_year if trusted_movie and norm_key(trusted_movie) == best_movie_key else ""
        year = year or next((a["year"] for a in agreeing if a["year"]), "")
        ai_names = []
        for a in sorted(agreeing, key=lambda a: -sum(name_votes[norm_key(n)] for n in a["actors"])):
            ai_names = a["actors"]
            if ai_names:
                break
        if face_names:
            if ai_names and not any(norm_key(n) in {norm_key(f) for f in face_names} for n in ai_names):
                warnings.append(f"AI suggested {', '.join(ai_names)}; Amazon's face match says "
                                f"{', '.join(face_names)}")
                ai_names = []
            actors = face_names + [n for n in ai_names if norm_key(n) not in {norm_key(f) for f in face_names}]
        else:
            actors = ai_names or ([web_actor] if web_actor else [])
        scene = next((a["scene"] for a in agreeing if a["scene"]), "") or next(
            (a["scene"] for a in answers if a["scene"]), "")
        clue_text = "; ".join(a["clues"] for a in agreeing if a["clues"])[:300]

        movie_ok = bool(movie) and movie_votes[best_movie_key] >= 2
        actors_ok = bool(actors) and any(name_votes[norm_key(n)] >= 2 for n in actors)
        if self.film and movie and not self.stop.is_set():
            film = self.film.check(movie, year, actors)
            if film:
                sources.append("Film check")
                movie_ok = True
                if norm_key(film["title"]) == norm_key(movie) and film["title"] != movie:
                    movie = film["title"]
                first, last = film.get("first_year"), film.get("last_year")
                if first:
                    y = int(year) if year else None
                    if y is None:
                        year = str(first)
                    elif not (first - 1 <= y <= (last or first) + 1):
                        warnings.append(f"Year changed from {year} to {first} (film database)")
                        year = str(first)
                if film["matched"]:
                    actors_ok = True
                elif actors and film["cast"]:
                    warnings.append(f"{', '.join(actors)} not in the film database's cast list for {film['title']}")
            elif getattr(self.film, "last_error", None):
                warnings.append("The film database didn't answer, so the film wasn't checked")
            else:
                warnings.append(f"\"{movie}\" wasn't found in the film database")

        if not movie:
            warnings.append("No film or show found")
        if not actors:
            warnings.append("No actors found")
        confidence = "high" if movie_ok and actors_ok else "medium" if movie and actors else "low"
        return {"movie": movie, "year": clean_year(year), "actors": actors, "scene": scene,
                "confidence": confidence, "sources": sources, "warnings": warnings, "clues": clue_text}


def apply_result(item: dict, result: dict, cfg: dict, batch_type: str) -> None:
    item.update({
        "movie": result["movie"], "year": result["year"], "actors": ", ".join(result["actors"]),
        "scene": result["scene"], "confidence": result["confidence"], "sources": result["sources"],
        "warnings": result["warnings"], "clues": result.get("clues", ""), "identified": True,
        "title_custom": False,
    })
    item["needs_check"] = result["confidence"] != "high" or bool(result["warnings"])
    item["include"] = bool(result["movie"] or result["actors"])
    item["title"] = title_for(item, cfg, batch_type)


def title_for(item: dict, cfg: dict, batch_type: str) -> str:
    return build_title(cfg.get(f"title_suffix_{batch_type}", ""), item.get("movie", ""), item.get("year", ""),
                       item.get("actors", ""), item.get("scene", ""), int(cfg.get("max_title_chars", 80)))


# ─── The pipeline ─────────────────────────────────────────────────────────────

STAGES = (("check", "Checking scans"), ("straighten", "Straightening"), ("high", "Saving high res"),
          ("low", "Making low res"), ("upload", "Uploading pictures"), ("identify", "Finding titles"))


class Pipeline:
    """Runs every step for a batch. Each step skips photos it has already done,
    so a stopped or crashed run picks up where it left off."""

    def __init__(self, cfg: dict, batch: Batch, emit, stop: threading.Event,
                 scans: list[Path] | None = None, identifier: Identifier | None = None):
        self.cfg, self.batch, self.emit, self.stop = cfg, batch, emit, stop
        self.scans = scans or []
        self.identifier = identifier

    def log(self, text, level="info"):
        (log.warning if level in ("warn", "error") else log.info)("%s: %s", self.batch.name, text)
        self.emit("log", text=text, level=level)

    def progress(self, key, done, total, text=""):
        self.emit("progress", key=key, done=done, total=total, text=text)

    def ok_items(self):
        return [i for i in self.batch.items if not i.get("error") and not i.get("saved")]

    def run(self) -> None:
        try:
            for key, label in STAGES:
                if self.stop.is_set():
                    break
                self.emit("stage", key=key, label=label)
                getattr(self, "_" + key)()
            self.batch.save()
            self.emit("stopped" if self.stop.is_set() else "finished")
        except StageError as e:
            self.batch.save()
            self.log(str(e), "error")
            self.emit("error", message=str(e))
        except Exception as e:
            log.exception("Run failed")
            self.batch.save()
            self.emit("error", message=f"Something went wrong: {e}")

    def _check(self):
        if self.scans:
            added = add_scans(self.batch, self.scans)
            self.log(f"Added {added} scan{'s' if added != 1 else ''} to {self.batch.name}")
        for item in self.batch.items:
            if item.get("error") and not item.get("reported"):
                self.log(f"{item['source']}: {item['error']}. Skipped.", "warn")
                item["reported"] = True
        todo = [i for i in self.ok_items() if "format" not in i]
        if not self.ok_items():
            broken = [i["source"] for i in self.batch.items if i.get("error") and not i.get("saved")]
            self.batch.save()
            if broken:
                raise StageError("None of the new files could be opened as pictures: " + ", ".join(broken[:8]) +
                                 ". Check they're scans (JPEG, TIFF or PNG), then try again.")
            raise StageError("There are no scans to work on. Put the scans in the scans folder first.")
        for n, item in enumerate(todo, 1):
            if self.stop.is_set():
                return
            src = self.batch.source_path(item)
            try:
                with Image.open(src) as im:
                    item["format"], item["size"], item["mode"] = im.format, list(im.size), im.mode
            except FileNotFoundError:
                item["error"] = "The scan file is missing"
            except Exception as e:
                item["error"] = f"Can't open this file ({e.__class__.__name__})"
            if item.get("error"):
                self.log(f"{item['source']}: {item['error']}. Skipped.", "warn")
            self.progress("check", n, len(todo), item["source"])
        self.batch.save()

    def _straighten(self):
        todo = [i for i in self.ok_items() if i.get("turn") is None]
        if not todo:
            return
        finder = FaceFinder() if self.cfg.get("straighten", True) else None
        if finder is not None and not finder.kind:
            self.log("Straightening is off: OpenCV isn't installed.", "warn")
        for n, item in enumerate(todo, 1):
            if self.stop.is_set():
                break
            turn = 0
            if finder and finder.kind:
                try:
                    turn = finder.best_turn(self.batch.source_path(item))
                except Exception as e:
                    self.log(f"{item['source']}: couldn't check which way up ({e})", "warn")
            item["turn"] = turn
            if turn:
                item["turned_auto"] = turn
                self.log(f"{self.batch.sku(item)}: turned {('a quarter turn left', 'upside down', 'a quarter turn right')[turn // 90 - 1]}")
            self.progress("straighten", n, len(todo), self.batch.sku(item))
            if n % 10 == 0:
                self.batch.save()
        self.batch.save()

    def _high(self):
        todo = [i for i in self.ok_items()
                if not i.get("high_done") or not self.batch.high_path(i).is_file()]   # also remakes lost copies
        for n, item in enumerate(todo, 1):
            if self.stop.is_set():
                break
            try:
                how = save_high_res(self.batch.source_path(item), self.batch.high_path(item),
                                    item.get("format", ""), item.get("turn") or 0)
                item["high_done"] = True
                if how == "converted":
                    self.log(f"{self.batch.sku(item)}: {item.get('format')} scan saved as JPEG")
            except FileNotFoundError:
                item["error"] = "The scan file is missing"
                self.log(f"{item['source']}: {item['error']}", "warn")
            except Exception as e:
                item["error"] = f"Couldn't save the high res copy ({e})"
                self.log(f"{item['source']}: {item['error']}", "error")
            self.progress("high", n, len(todo), self.batch.sku(item))
            self.batch.save()

    def _low(self):
        longest = int(self.cfg.get("low_res_longest_side", 800))
        todo = [i for i in self.ok_items() if i.get("high_done")
                and (not i.get("low_done") or not self.batch.low_path(i).is_file())]
        for n, item in enumerate(todo, 1):
            if self.stop.is_set():
                break
            try:
                save_low_res(self.batch.high_path(item), self.batch.low_path(item), longest)
                item["low_done"] = True
                item["uploaded"] = False
            except Exception as e:
                item["error"] = f"Couldn't make the low res copy ({e})"
                self.log(f"{self.batch.sku(item)}: {item['error']}", "error")
            self.progress("low", n, len(todo), self.batch.sku(item))
            if n % 10 == 0:
                self.batch.save()
        self.batch.save()

    def _upload(self):
        items = [i for i in self.ok_items() if i.get("low_done")]
        for item in items:
            item["url"] = make_url(self.cfg, self.batch.name, self.batch.low_path(item).name)
        if self.cfg.get("upload_mode", "ftp") != "ftp":
            self.log("Uploading is set to \"I upload the pictures myself\": "
                     f"upload the {self.batch.low_dir} folder.")
            self.batch.data["link_check"] = None      # nothing is online yet, so nothing to check
            self.batch.save()
            return
        upload_pending(self.cfg, self.batch, items, self.log, self.progress, self.stop)
        self.batch.save()
        if items and not self.stop.is_set():
            ok, message = check_link(items[0]["url"])
            self.batch.data["link_check"] = {"ok": ok, "message": message, "url": items[0]["url"]}
            self.log(message, "info" if ok else "warn")
            self.batch.save()

    def _identify(self):
        todo = [i for i in self.ok_items() if i.get("low_done") and not i.get("identified")]
        if not todo:
            return
        ident = self.identifier or Identifier(self.cfg, self.log, self.stop)
        if not ident.has_any_service():
            raise StageError("No title services are set up. Add a Gemini, GPT or Claude key in "
                             "Settings > Identify, then press Try again.")
        public = bool((self.batch.data.get("link_check") or {}).get("ok"))
        for n, item in enumerate(todo, 1):
            if self.stop.is_set():
                break
            sku = self.batch.sku(item)
            self.progress("identify", n - 1, len(todo), sku)
            try:
                result = ident.identify(self.batch.high_path(item), item.get("url") if public else None)
            except Exception as e:
                log.exception("Identify failed for %s", sku)
                self.log(f"{sku}: couldn't find a title ({e}). You can try again on the review screen.",
                         "error")
                continue
            if self.stop.is_set():
                break
            apply_result(item, result, self.cfg, self.batch.batch_type)
            self.log(f"{sku}: {item['title']}  [{', '.join(result['sources']) or 'no sources'}]",
                     "info" if not item["needs_check"] else "warn")
            self.batch.save()
            self.progress("identify", n, len(todo), sku)


def upload_pending(cfg, batch, items, log_fn, progress=None, stop=None) -> int:
    pending = [i for i in items if not i.get("uploaded")]
    if not pending:
        return 0
    ftp, secure = ftp_open(cfg)
    if not secure:
        log_fn(f"{cfg.get('ftp_host')} doesn't support secure FTP, so the password was sent "
               "unencrypted (as before).", "warn")
    try:
        ftp_enter(ftp, cfg.get("ftp_remote_dir", "/"), batch.name)
        for n, item in enumerate(pending, 1):
            if stop is not None and stop.is_set():
                break
            path = batch.low_path(item)
            for attempt in (1, 2):
                try:
                    with open(path, "rb") as f:
                        ftp.storbinary(f"STOR {path.name}", f)
                    break
                except FTP_ERRORS as e:
                    if attempt == 2:
                        raise StageError(f"Uploading {path.name} failed: {e}")
                    time.sleep(2)
            item["uploaded"] = True
            if progress:
                progress("upload", n, len(pending), path.name)
            if n % 10 == 0:
                batch.save()
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()
    batch.save()
    return len(pending)


def csv_row(cfg: dict, batch: Batch, item: dict) -> dict:
    sku = batch.sku(item)
    return {"SKU": f"{batch.name}/{sku}", "eBay Title": item.get("title", ""), "Title": item.get("title", ""),
            "PicURL": item.get("url") or make_url(cfg, batch.name, f"{sku}.jpg"),
            "Description": cfg.get(f"description_{batch.batch_type}", "")}


def append_csv(path: Path, rows: list[dict]) -> int:
    """Add rows whose SKU isn't in the file yet. Copes with a CSV that Excel
    re-saved (other text encoding, no final line break)."""
    existing, encoding, needs_break = set(), "utf-8", False
    exists = path.is_file() and path.stat().st_size > 0
    try:
        if exists:
            raw = path.read_bytes()
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text, encoding = raw.decode("cp1252", errors="replace"), "cp1252"
            existing = {r.get("SKU", "") for r in csv.DictReader(io.StringIO(text, newline=""))}
            needs_break = not raw.endswith((b"\n", b"\r"))
        new = [r for r in rows if r["SKU"] not in existing]
        with open(path, "a", newline="", encoding=encoding, errors="replace") as f:
            if needs_break:
                f.write("\r\n")
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            if not exists:
                w.writeheader()
            w.writerows(new)
            f.flush()
            os.fsync(f.fileno())
    except PermissionError:
        raise StageError(f"{path.name} is open in another program (Excel?) or read-only. "
                         "Close it and press Save again.")
    return len(new)


def finish_batch(cfg: dict, batch: Batch, log_fn) -> dict:
    """Upload any re-turned pictures, add the included photos to the CSV, then
    clear their scans out of the scans folder. Returns counts for the screen."""
    def complete(i):
        high, low = batch.high_path(i), batch.low_path(i)
        return high.is_file() and high.stat().st_size > 0 and low.is_file() and low.stat().st_size > 0

    open_items = batch.open_items()
    missing = [i for i in open_items if i.get("include") and i.get("title") and not complete(i)]
    included = [i for i in open_items if i.get("include") and i.get("title") and i not in missing]
    left_out = [i for i in open_items if i not in included and i not in missing]
    for i in missing:
        log_fn(f"{batch.sku(i)}: its high or low res picture is missing, so it wasn't saved", "warn")
    batch.data["finish_started"] = True
    batch.save()
    if cfg.get("upload_mode", "ftp") == "ftp":
        upload_pending(cfg, batch, included, log_fn)
    added = append_csv(batch.csv_path, [csv_row(cfg, batch, i) for i in included])
    for i in included:
        i["saved"] = True
    for i in left_out:
        i["saved"] = True
        i["left_out"] = True
    batch.save()
    cleared, kept = 0, []
    if cfg.get("after_finish", "recycle") == "recycle":
        archive = Path(cfg["output_folder"]) / f"scans archive {batch.data['year']}" / batch.name
        for item in batch.items:
            if item.get("recycled") or not item.get("saved") or item.get("error"):
                continue
            src = batch.source_path(item)
            if not src.is_file():
                item["recycled"] = True
                continue
            high = batch.high_path(item)
            if not high.is_file() or high.stat().st_size == 0:
                kept.append(item["source"])
                log_fn(f"{item['source']}: kept in the scans folder (no high res copy)", "warn")
                continue
            if not scan_unchanged(item, src):
                kept.append(item["source"])
                log_fn(f"{item['source']}: kept in the scans folder (it changed after it was processed)", "warn")
                continue
            item["recycled"] = recycle_scan(src, archive)
            cleared += 1
            batch.save()
    batch.data["finished"] = not batch.open_items()
    if batch.data["finished"]:
        batch.data["finished_at"] = datetime.now().isoformat(timespec="seconds")
    batch.save()
    broken = [i["source"] for i in batch.items if i.get("error")]
    return {"added": added, "left_out": len(left_out), "cleared": cleared, "broken": broken,
            "kept": kept, "missing": [batch.sku(i) for i in missing]}


def recycle_scan(path: Path, archive: Path) -> str:
    try:
        from send2trash import send2trash
        send2trash(str(path))
        return "recycle bin"
    except Exception as e:
        log.info("Recycle Bin unavailable for %s (%s); archiving", path, e)
    archive.mkdir(parents=True, exist_ok=True)
    dest = archive / path.name
    n = 2
    while dest.exists():
        dest = archive / f"{path.stem} ({n}){path.suffix}"
        n += 1
    shutil.move(str(path), str(dest))
    return "archive"


def turn_pictures(cfg: dict, batch: Batch, item: dict) -> None:
    """A quarter turn clockwise for both copies, for photos the straightener got
    wrong. The caller marks the picture for uploading again."""
    turn_file(batch.high_path(item), 270)
    save_low_res(batch.high_path(item), batch.low_path(item), int(cfg.get("low_res_longest_side", 800)))


# ─── Rough costs (list prices, Nov 2026) ──────────────────────────────────────
# Per photo per call, assuming about 1,500 tokens in and 400 out. A guide only.

CALL_PRICES = {
    "gemini-3.8-flash": 0.0026, "gemini-3.6-flash": 0.0026, "gemini-3.5-flash-lite": 0.0015,
    "gpt-6.1-terra": 0.0035, "gpt-6-luna": 0.0004, "gpt-6.1-sol": 0.007, "gpt-6-astra": 0.035,
    "claude-sonnet-5-5": 0.007, "claude-haiku-5-5": 0.0004, "claude-opus-5-5": 0.014,
}


def cost_per_photo(cfg: dict) -> tuple[float, float]:
    base = 0.0
    if cfg.get("google_vision_api_key"):
        base += 0.0035
    if cfg.get("aws_access_key_id") and cfg.get("aws_secret_access_key"):
        base += 0.001
    if cfg.get("serpapi_api_key"):
        base += 0.025
    writers = configured_writers(cfg)
    if not writers:
        return base, base
    fields = {"gemini": "gemini_model", "openai": "openai_model", "claude": "anthropic_model"}
    prices = [CALL_PRICES.get(cfg.get(fields[w], ""), 0.005) for w in writers]
    calls = 5 if cfg.get("identify_mode", "thorough") != "fast" else 3
    sequence = (prices * 3)[:calls]
    return base + prices[0], base + sum(sequence)


# ─── Setting checks (the Test buttons) ────────────────────────────────────────

def _tiny_jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (80, 80), (200, 200, 200)).save(buf, "JPEG")
    return buf.getvalue()


def test_service(name: str, cfg: dict) -> tuple[bool, str]:
    """Quick check that a key works. Free, except Google Vision and Amazon,
    which use one picture from the monthly free allowance."""
    s = http()
    try:
        if name == "gemini":
            r = s.get(f"https://generativelanguage.googleapis.com/v1beta/models/{cfg['gemini_model']}",
                      headers={"x-goog-api-key": cfg["gemini_api_key"]}, timeout=20)
            return (True, "Gemini key works.") if r.ok else (False, f"Gemini: {_error_text(r)}")
        if name == "openai":
            r = s.get(f"https://api.openai.com/v1/models/{cfg['openai_model']}",
                      headers={"Authorization": f"Bearer {cfg['openai_api_key']}"}, timeout=20)
            return (True, "GPT key works.") if r.ok else (False, f"GPT: {_error_text(r)}")
        if name == "claude":
            import anthropic
            client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"], max_retries=1, timeout=20.0)
            client.models.retrieve(cfg["anthropic_model"])
            return True, "Claude key works."
        if name == "vision":
            google_vision_web_detect(cfg["google_vision_api_key"],
                                     base64.b64encode(_tiny_jpeg()).decode("ascii"), s)
            return True, "Google Vision key works."
        if name == "amazon":
            rekognition_celebrities(cfg["aws_access_key_id"], cfg["aws_secret_access_key"],
                                    cfg.get("aws_region"), _tiny_jpeg())
            return True, "Amazon keys work."
        if name == "lens":
            r = s.get("https://serpapi.com/account.json", params={"api_key": cfg["serpapi_api_key"]}, timeout=20)
            if not r.ok:
                return False, f"SerpApi: {_error_text(r)}"
            left = r.json().get("total_searches_left")
            return True, f"SerpApi key works ({left} searches left this month)." if left is not None \
                else "SerpApi key works."
        if name == "ftp":
            ftp, secure = ftp_open(cfg)
            try:
                ftp.quit()
            except Exception:
                ftp.close()
            return True, "FTP login works (encrypted)." if secure else \
                "FTP login works, but the server doesn't support encryption."
        if name == "link":
            host_url = make_url(cfg, "test", "test.jpg")
            if not host_url.lower().startswith("https://"):
                return False, "The link pattern starts with http://. eBay needs https://."
            base = re.match(r"https://[^/]+/", host_url)
            r = s.head(base.group(0) if base else host_url, timeout=15, allow_redirects=True)
            return True, f"The picture server answers over https (status {r.status_code})."
    except StageError as e:
        return False, str(e)
    except ProviderError as e:
        return False, str(e)
    except ImportError as e:
        return False, f"Missing package: {e.name}"
    except Exception as e:
        if requests and isinstance(e, requests.exceptions.SSLError):
            return False, "The server doesn't have working https. eBay won't show these pictures."
        return False, f"{e.__class__.__name__}: {e}"
    return False, "Unknown check"
