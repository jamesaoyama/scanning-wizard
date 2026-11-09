#!/usr/bin/env python3
"""
Scanning Wizard 2026 — v3
"""

import os, sys, json, shutil, glob, re, csv, subprocess, threading, base64, io
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext
from datetime import datetime
from pathlib import Path
from ftplib import FTP

# Auto-install
def ensure_pkg(pkg, name=None):
    try: __import__(name or pkg); return True
    except ImportError:
        try: subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "--quiet"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); return True
        except: return False

ensure_pkg("Pillow", "PIL")
ensure_pkg("requests")
ensure_pkg("boto3")

from PIL import Image, ExifTags
import requests
import boto3

# ─── Config ───────────────────────────────────────────────────────────────────

CURRENT_YEAR = datetime.now().strftime("%Y")
CONFIG_PATH = os.path.expanduser("~/.scanning_wizard_config.json")
DESKTOP = os.path.join(os.path.expanduser("~"), "Desktop")

DEFAULT_CONFIG = {
    "input_folder": os.path.join(DESKTOP, "Scanned Images For Wizard"),
    "output_folder": os.path.join(DESKTOP, "Finished Images Wizard"),
    "ftp_host": "images.moviestore.com",
    "ftp_user": "",
    "ftp_pass": "",
    "ftp_remote_dir": "/",
    "ps_action_set": "Retoucher",
    "ps_action_resize": "Resize Low Res-V",
    "ps_action_exif": "EXIF METADATA",
    "low_res_longest_side": 800,
    "ai_provider": "cascade",
    "google_vision_api_key": "",
    "aws_access_key_id": "",
    "aws_secret_access_key": "",
    "aws_region": "us-east-1",
    "gemini_api_key": "",
    "openai_api_key": "",
    "deepseek_api_key": "",
    "anthropic_api_key": "",
    "gemini_model": "gemini-2.5-flash",
    "openai_model": "gpt-4o",
    "deepseek_model": "deepseek-chat",
    "anthropic_model": "claude-sonnet-4-5-20250929",
    "max_title_chars": 80,
}

# Fixed descriptions — exact text, do not modify
DESC_SO = "This is an 8x10 (20x25cm) original black and white photograph. It was produced by the film studio during the year the actual movie or television series was in production. This photograph has been stored in archive filing cabinets since production. The photograph is in excellent vintage condition. Photographs were produced by the film studios and television networks to publicize their movies and TV shows and were often distributed to the press and newspapers and magazines internationally. We ship in special heavy duty photo mailers and you can choose from our low price USPS First Class Mail delivery or our special low rate 2 Day FedEx service. A unique historical piece of movie memorabilia from VIP Images, we guarantee your satisfaction.  This is a unique piece of cinematic history."

DESC_SM = "This is a collectible vintage 8x10 inch (20x25cms) real photograph with a glossy finish and printed on 100%25 real photographic paper. This vintage collectible is in mint condition and comes from our photo archive of movie studio, press agency, newspaper and photographer candid photographs. This photograph was professionally printed in a photo lab in the 1970's-2000's. Photos made in the 1970's and 1980's typically have a small white border whilst photos from the 1990's-2000's are borderless (i.e. no white border) which is typical of that era. Movie studios and Television station broadcasters typically used these photographs for publicity purposes back in the day and they were distributed to the press and magazines and newspapers world wide. This is a collectible piece of cinema or television history. We ship in special heavy duty photo mailers and you can choose from our low price USPS First Class Mail delivery or our special low rate 2 Day FedEx service. A unique historical piece of movie memorabilia."

# Title suffixes
TITLE_PREFIX = {
    "SM": "Vintage 8x10 Inch Photo ",
    "SO": "Original 8x10 Inch Photo ",
    "MM": "Vintage Photo ",
}

DESCRIPTIONS = {
    "SM": DESC_SM,
    "SO": DESC_SO,
    "MM": DESC_SO,  # fallback
}


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                saved = json.load(f)
            merged = {**DEFAULT_CONFIG, **saved}
            if merged.get("gemini_model", "").startswith("gemini-2.0"):
                merged["gemini_model"] = "gemini-2.5-flash"
            return merged
        except: pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# ─── Theme ────────────────────────────────────────────────────────────────────

C = {
    "bg": "#1a1a2e", "bg2": "#16213e", "card": "#0f3460",
    "accent": "#e94560", "accent2": "#ff6b81", "green": "#2ed573",
    "yellow": "#ffa502", "red": "#ff4757", "text": "#eaeaea",
    "dim": "#8892a4", "bright": "#ffffff", "input": "#0d1b2a",
    "border": "#2a2a4a", "done": "#2ed573", "active": "#e94560",
    "pending": "#3a3a5c",
}

STEP_NAMES = [
    "1. Setup", "2. Rotation", "3. Rename", "4. Move Files",
    "5. Photoshop", "6. FTP Upload", "7. AI Listings",
]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_images(folder):
    exts = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.bmp', '.webp'}
    if not os.path.isdir(folder): return []
    return [os.path.join(folder, f) for f in sorted(os.listdir(folder))
            if os.path.splitext(f)[1].lower() in exts and not f.startswith('.')]


def get_today_str():
    """MMDDYY format"""
    return datetime.now().strftime("%m%d%y")


def get_batch_prefix(batch_type):
    """e.g. SM030526"""
    return f"{batch_type}{get_today_str()}"


def find_last_number_in_folder(folder, prefix):
    """Find the highest number N in files like SM030526_0001.jpg"""
    max_n = 0
    if not os.path.isdir(folder):
        return 0
    for f in os.listdir(folder):
        match = re.match(rf'{re.escape(prefix)}[_-](\d+)', f, re.IGNORECASE)
        if match:
            max_n = max(max_n, int(match.group(1)))
    return max_n


def find_last_number_in_csv(csv_path, prefix):
    """Find the highest number in the SKU column of existing CSV."""
    max_n = 0
    if not os.path.isfile(csv_path):
        return 0
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                sku = row.get("SKU", "")
                # SKU format: SM030526/SM030526_0004
                match = re.search(r'_(\d+)$', sku)
                if match:
                    max_n = max(max_n, int(match.group(1)))
    except: pass
    return max_n


def resize_for_api(image_path, max_px=1024):
    try:
        img = Image.open(image_path)
        w, h = img.size
        if max(w, h) > max_px:
            ratio = max_px / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=80)
        return buf.getvalue()
    except: return None


def strip_punctuation(text):
    """Remove all punctuation and unwanted words from title text."""
    text = re.sub(r'[-,.:;()\[\]"\'!?]', '', text).strip()
    # Remove unwanted words
    unwanted = r'\b(Person|People|Man|Woman|Boy|Girl|Lady|Blonde|Brunette|Redhead|Two People)\b'
    text = re.sub(unwanted, '', text, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', text).strip()


def clean_year(year):
    """Fix year ranges and bad values. Returns clean year or empty string."""
    year = str(year or "")
    if not year or year.upper() == "UNKNOWN":
        return ""
    # Detect ranges like "1997-2001"
    if re.search(r'\d{4}\s*[-–]\s*\d{4}', year):
        return ""
    # Detect merged years like "19972001"
    if re.match(r'^\d{8}$', year.strip()):
        return ""
    # Extract just a 4-digit year
    m = re.search(r'(\d{4})', year)
    if m:
        y = int(m.group(1))
        if 1900 <= y <= 2030:
            return str(y)
    return ""


def build_title(batch_type, movie_title, year, actors, scene, max_chars=80):
    """Build eBay title with suffix at end. Title Case. No punctuation."""
    suffix = TITLE_PREFIX.get(batch_type, "Vintage Photo ").strip()

    # Safety: cast everything to string
    movie_title = str(movie_title or "")
    year = str(year or "")
    actors = str(actors or "")
    scene = str(scene or "")

    has_movie = movie_title and movie_title.upper() != "UNKNOWN"
    cleaned_year = clean_year(year)
    has_year = bool(cleaned_year)
    has_actors = actors and actors.upper() != "UNKNOWN"
    has_scene = scene and scene.upper() != "UNKNOWN"

    # Build: [Movie] [Year] [Actors] [Scene] [Suffix]
    parts = []
    if has_movie:
        parts.append(strip_punctuation(movie_title))
    if has_year:
        parts.append(cleaned_year)
    if has_actors:
        parts.append(strip_punctuation(actors))
    if has_scene:
        parts.append(strip_punctuation(scene))

    # Title case the content, then append suffix as-is
    content = re.sub(r'\s+', ' ', " ".join(parts).title()).strip()
    title = f"{content} {suffix}".strip() if content else suffix

    # If over limit, drop year first
    if len(title) > max_chars and has_year:
        parts = []
        if has_movie: parts.append(strip_punctuation(movie_title))
        if has_actors: parts.append(strip_punctuation(actors))
        if has_scene: parts.append(strip_punctuation(scene))
        content = re.sub(r'\s+', ' ', " ".join(parts).title()).strip()
        title = f"{content} {suffix}".strip() if content else suffix

    # If still over, drop scene
    if len(title) > max_chars and has_scene:
        parts = []
        if has_movie: parts.append(strip_punctuation(movie_title))
        if has_year: parts.append(cleaned_year)
        if has_actors: parts.append(strip_punctuation(actors))
        content = re.sub(r'\s+', ' ', " ".join(parts).title()).strip()
        title = f"{content} {suffix}".strip() if content else suffix

    # If STILL over, drop both year and scene
    if len(title) > max_chars:
        parts = []
        if has_movie: parts.append(strip_punctuation(movie_title))
        if has_actors: parts.append(strip_punctuation(actors))
        content = re.sub(r'\s+', ' ', " ".join(parts).title()).strip()
        title = f"{content} {suffix}".strip() if content else suffix

    # Hard truncate as last resort
    if len(title) > max_chars:
        trunc = title[:max_chars - 3]
        if " " in trunc:
            trunc = trunc.rsplit(" ", 1)[0]
        title = trunc + "..."

    return title


# ─── Google Vision + Amazon Rekognition ───────────────────────────────────────

def google_vision_web_detect(config, image_b64):
    """Reverse image search via Google Cloud Vision. Returns dict with entities and pages."""
    key = config.get("google_vision_api_key", "")
    if not key:
        return None
    try:
        url = f"https://vision.googleapis.com/v1/images:annotate?key={key}"
        payload = {
            "requests": [{
                "image": {"content": image_b64},
                "features": [{"type": "WEB_DETECTION", "maxResults": 10}]
            }]
        }
        resp = requests.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        web = data.get("responses", [{}])[0].get("webDetection", {})

        # Extract best entity matches
        entities = []
        for e in web.get("webEntities", []):
            desc = e.get("description", "")
            score = e.get("score", 0)
            if desc:
                entities.append({"name": desc, "score": score})

        # Extract page URLs where image was found
        pages = []
        for p in web.get("pagesWithMatchingImages", []):
            title = p.get("pageTitle", "")
            url = p.get("url", "")
            if title or url:
                pages.append({"title": title, "url": url})

        return {"entities": entities, "pages": pages}
    except Exception as e:
        return {"error": str(e), "entities": [], "pages": []}


def parse_google_vision_results(vision_data):
    """Extract movie, actor, year from Google Vision web detection results.
    ONLY trusts movie titles from IMDB/movie database URLs.
    Web entities are used for actor names only."""
    if not vision_data or "error" in vision_data:
        return None, None, None, 0.0

    entities = vision_data.get("entities", [])
    pages = vision_data.get("pages", [])

    movie = None
    actor = None
    year = None
    best_score = 0.0

    # Generic labels that are NOT movie titles or actor names
    skip_words = {
        "photograph", "photo", "photography", "image", "picture", "film", "movie",
        "still", "scene", "vintage", "black and white", "portrait", "monochrome",
        "actor", "actress", "celebrity", "stock photography", "stock photo",
        "sitting", "standing", "posing", "smiling", "looking", "walking",
        "photo shoot", "photoshoot", "fashion", "model", "modeling",
        "vintage clothing", "clothing", "dress", "suit", "costume",
        "blond", "blonde", "brunette", "redhead", "hair",
        "man", "woman", "girl", "boy", "lady", "gentleman", "person", "people",
        "beauty", "glamour", "glamor", "sexy", "beautiful", "handsome",
        "studio", "indoor", "outdoor", "room", "chair", "sofa",
        "entertainment", "performing arts", "visual arts", "art",
        "retro", "classic", "old", "antique", "nostalgia",
        "hollywood", "cinema", "television", "tv", "screen",
        "publicity", "press", "headshot", "head shot",
        "snapshot", "candid", "paparazzi",
    }

    # ── Extract movie ONLY from IMDB / movie database page titles ──
    movie_domains = ["imdb.com", "themoviedb.org", "rottentomatoes.com",
                     "letterboxd.com", "moviestillsdb.com", "allmovie.com"]
    for page in pages:
        title = page.get("title", "")
        url = page.get("url", "")
        url_lower = url.lower()

        is_movie_site = any(domain in url_lower for domain in movie_domains)
        if is_movie_site and title:
            # IMDB format: "Movie Name (Year) - IMDb"
            imdb_match = re.search(r'(.+?)\s*\((\d{4})\)', title)
            if imdb_match and not movie:
                candidate = imdb_match.group(1).strip()
                # Clean up common suffixes
                candidate = re.sub(r'\s*[-–|]\s*(IMDb|TMDB|Photo|Photos|Gallery).*$', '', candidate, flags=re.IGNORECASE).strip()
                if candidate and candidate.lower() not in skip_words:
                    movie = candidate
                    if not year:
                        year = imdb_match.group(2)

    # ── Extract actor names from web entities (person names only) ──
    for e in entities:
        name = e.get("name", "")
        score = e.get("score", 0)
        name_lower = name.lower().strip()

        if name_lower in skip_words or len(name) < 3:
            continue

        # Skip years
        if re.match(r'^(19|20)\d{2}$', name.strip()):
            if not year:
                year = name.strip()
            continue

        if score > best_score:
            best_score = score

        # Only accept as actor if it looks like a person name (2+ capitalized words)
        words = name.split()
        is_person = (len(words) >= 2
                     and all(w[0].isupper() for w in words if w)
                     and not any(c.isdigit() for c in name)
                     and name_lower not in skip_words)

        if is_person and not actor:
            actor = name

    # Do NOT return generic labels as movie titles
    return movie, actor, year, best_score


def amazon_rekognition_celebrity(config, image_bytes):
    """Identify celebrity faces via Amazon Rekognition. Returns list of matches."""
    key_id = config.get("aws_access_key_id", "")
    secret = config.get("aws_secret_access_key", "")
    region = config.get("aws_region", "us-east-1")
    if not key_id or not secret:
        return None
    try:
        client = boto3.client(
            'rekognition',
            aws_access_key_id=key_id,
            aws_secret_access_key=secret,
            region_name=region
        )
        response = client.recognize_celebrities(
            Image={'Bytes': image_bytes}
        )
        celebrities = []
        for celeb in response.get('CelebrityFaces', []):
            name = celeb.get('Name', '')
            confidence = celeb.get('MatchConfidence', 0)
            urls = celeb.get('Urls', [])
            if name:
                celebrities.append({
                    "name": name,
                    "confidence": confidence,
                    "urls": urls
                })
        return celebrities
    except Exception as e:
        return [{"error": str(e)}]


# ─── AI API ───────────────────────────────────────────────────────────────────

def ai_call_gemini(config, image_b64, media_type, prompt, temperature=0.1):
    key = config["gemini_api_key"]
    model = config.get("gemini_model", "gemini-2.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    resp = requests.post(url, json={
        "contents": [{"parts": [
            {"inlineData": {"mimeType": media_type, "data": image_b64}},
            {"text": prompt}
        ]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": 1024},
    }, timeout=90)
    resp.raise_for_status()
    data = resp.json()
    try: return data["candidates"][0]["content"]["parts"][0]["text"]
    except: raise ValueError(f"Bad response: {json.dumps(data)[:300]}")


def ai_call_openai(config, image_b64, media_type, prompt, temperature=0.1):
    key = config["openai_api_key"]
    model = config.get("openai_model", "gpt-4o")
    resp = requests.post("https://api.openai.com/v1/chat/completions", json={
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{image_b64}", "detail": "low"}},
            {"type": "text", "text": prompt}
        ]}], "max_tokens": 1024, "temperature": temperature,
    }, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, timeout=90)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def ai_call_deepseek(config, image_b64, media_type, prompt, temperature=0.1):
    key = config["deepseek_api_key"]
    model = config.get("deepseek_model", "deepseek-chat")
    resp = requests.post("https://api.deepseek.com/v1/chat/completions", json={
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{image_b64}", "detail": "low"}},
            {"type": "text", "text": prompt}
        ]}], "max_tokens": 1024, "temperature": temperature,
    }, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, timeout=90)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def ai_call_claude(config, image_b64, media_type, prompt, temperature=0.1):
    key = config["anthropic_api_key"]
    model = config.get("anthropic_model", "claude-sonnet-4-5-20250929")
    resp = requests.post("https://api.anthropic.com/v1/messages", json={
        "model": model,
        "max_tokens": 1024,
        "temperature": temperature,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
            {"type": "text", "text": prompt}
        ]}],
    }, headers={
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json"
    }, timeout=90)
    resp.raise_for_status()
    data = resp.json()
    try: return data["content"][0]["text"]
    except: raise ValueError(f"Bad Claude response: {json.dumps(data)[:300]}")


AI_PROVIDERS = {
    "gemini": ("gemini_api_key", ai_call_gemini),
    "openai": ("openai_api_key", ai_call_openai),
    "deepseek": ("deepseek_api_key", ai_call_deepseek),
    "claude": ("anthropic_api_key", ai_call_claude),
}

# Cascade order: Gemini (free) → Claude (smart) → GPT-4o (last resort)
CASCADE_ORDER = ["gemini", "claude", "openai"]


def parse_ai_json(text):
    text = text.strip()
    # Strip markdown fences
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"): lines = lines[1:]
        if lines and lines[-1].strip() == "```": lines = lines[:-1]
        text = "\n".join(lines).strip()

    # Try direct parse
    try: return json.loads(text)
    except: pass

    # Extract outermost braces
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e > s:
        chunk = text[s:e+1]
        try: return json.loads(chunk)
        except: pass

        # Fix common issue: newlines inside JSON string values
        # Replace newlines between quotes with spaces
        fixed = re.sub(r'(?<=": ")(.*?)(?=")', lambda m: m.group(0).replace('\n', ' '), chunk, flags=re.DOTALL)
        try: return json.loads(fixed)
        except: pass

    # Last resort: regex extract individual fields
    try:
        movie = re.search(r'"movie_title"\s*:\s*"([^"]*)"', text)
        year = re.search(r'"year"\s*:\s*"([^"]*)"', text)
        actors = re.search(r'"actors"\s*:\s*"([^"]*)"', text)
        scene = re.search(r'"scene_description"\s*:\s*"([^"]*)"', text)
        return {
            "movie_title": movie.group(1) if movie else "UNKNOWN",
            "year": year.group(1) if year else "UNKNOWN",
            "actors": actors.group(1) if actors else "",
            "scene_description": scene.group(1) if scene else "",
        }
    except: pass

    return None


# ─── Main App ─────────────────────────────────────────────────────────────────

class ScanningWizard:
    def __init__(self, root):
        self.root = root
        self.root.title("Scanning Wizard 2026")
        self.root.geometry("1100x750")
        self.root.minsize(900, 600)
        self.root.configure(bg=C["bg"])

        self.config = load_config()
        self.current_step = 0
        self.step_status = ["pending"] * 7
        self._snapshot = {}

        # State
        self.batch_type = tk.StringVar(value="SM")
        self.batch_prefix = tk.StringVar()  # e.g. SM030526
        self.input_folder = tk.StringVar(value=self.config.get("input_folder", ""))
        self.output_folder = tk.StringVar(value=self.config.get("output_folder", ""))
        self.highres_path = tk.StringVar()
        self.lowres_path = tk.StringVar()
        self.batch_subfolder = tk.StringVar()  # full path to batch subfolder in highres
        self.file_count = tk.IntVar(value=0)
        self.next_number = tk.IntVar(value=1)
        self.ftp_urls = tk.StringVar()

        self._build_ui()
        self._show_step(0)

    def _build_ui(self):
        top = tk.Frame(self.root, bg=C["bg2"], height=60)
        top.pack(fill="x"); top.pack_propagate(False)
        tk.Label(top, text="Scanning Wizard 2026", font=("Helvetica", 18, "bold"),
                 bg=C["bg2"], fg=C["bright"]).pack(side="left", padx=20, pady=15)
        tk.Button(top, text="Settings", font=("Helvetica", 11), bg=C["card"],
                  fg=C["text"], bd=0, cursor="hand2", command=self._open_settings
                  ).pack(side="right", padx=20, pady=15)

        main = tk.Frame(self.root, bg=C["bg"])
        main.pack(fill="both", expand=True)

        self.sidebar = tk.Frame(main, bg=C["bg2"], width=200)
        self.sidebar.pack(side="left", fill="y"); self.sidebar.pack_propagate(False)
        tk.Label(self.sidebar, text="WORKFLOW", font=("Helvetica", 10, "bold"),
                 bg=C["bg2"], fg=C["dim"]).pack(pady=(20, 10), padx=15, anchor="w")

        self.step_labels = []
        for i, name in enumerate(STEP_NAMES):
            frame = tk.Frame(self.sidebar, bg=C["bg2"], cursor="hand2")
            frame.pack(fill="x", padx=10, pady=2)
            ind = tk.Label(frame, text="○", font=("Helvetica", 14), bg=C["bg2"],
                           fg=C["pending"], width=2)
            ind.pack(side="left", padx=(5, 5))
            lbl = tk.Label(frame, text=name, font=("Helvetica", 11), bg=C["bg2"],
                           fg=C["dim"], anchor="w")
            lbl.pack(side="left", fill="x")
            self.step_labels.append((frame, ind, lbl))
            for w in (frame, ind, lbl):
                w.bind("<Button-1>", lambda e, idx=i: self._show_step(idx))

        self.batch_info = tk.Label(self.sidebar, text="", font=("Helvetica", 10),
                                   bg=C["bg2"], fg=C["dim"], wraplength=170, justify="left")
        self.batch_info.pack(side="bottom", fill="x", padx=15, pady=15)

        self.content = tk.Frame(main, bg=C["bg"])
        self.content.pack(side="right", fill="both", expand=True)
        self.step_frame = tk.Frame(self.content, bg=C["bg"])
        self.step_frame.pack(fill="both", expand=True, padx=30, pady=20)

        nav = tk.Frame(self.content, bg=C["bg"], height=60)
        nav.pack(fill="x", padx=30, pady=(0, 15))
        self.prev_btn = tk.Button(nav, text="← Previous", font=("Helvetica", 12),
                                  bg=C["card"], fg=C["text"], bd=0, cursor="hand2",
                                  command=self._prev, padx=20, pady=8)
        self.prev_btn.pack(side="left")
        self.next_btn = tk.Button(nav, text="Next →", font=("Helvetica", 12, "bold"),
                                  bg=C["accent"], fg="#fff", bd=0, cursor="hand2",
                                  command=self._next, padx=20, pady=8)
        self.next_btn.pack(side="right")
        self.run_btn = tk.Button(nav, text="▶ Run Step", font=("Helvetica", 12, "bold"),
                                 bg=C["green"], fg="#fff", bd=0, cursor="hand2",
                                 command=self._run_step, padx=20, pady=8)
        self.run_btn.pack(side="right", padx=(0, 10))

    def _update_sidebar(self):
        for i, (f, ind, lbl) in enumerate(self.step_labels):
            if self.step_status[i] == "done":
                ind.configure(fg=C["done"], text="●"); lbl.configure(fg=C["done"], font=("Helvetica", 11))
            elif i == self.current_step:
                ind.configure(fg=C["active"], text="◉"); lbl.configure(fg=C["bright"], font=("Helvetica", 11, "bold"))
            elif self.step_status[i] == "error":
                ind.configure(fg=C["red"], text="✕"); lbl.configure(fg=C["red"], font=("Helvetica", 11))
            else:
                ind.configure(fg=C["pending"], text="○"); lbl.configure(fg=C["dim"], font=("Helvetica", 11))
        bp = self.batch_prefix.get()
        if bp:
            self.batch_info.configure(
                text=f"Batch: {bp}\nType: {self.batch_type.get()}\nFiles: {self.file_count.get()}\nNext #: {self.next_number.get()}",
                fg=C["text"])

    def _show_step(self, idx):
        self.current_step = idx; self._update_sidebar()
        for w in self.step_frame.winfo_children(): w.destroy()
        [self._build_setup, self._build_rotation, self._build_rename,
         self._build_move, self._build_photoshop, self._build_ftp,
         self._build_ai][idx]()
        self.prev_btn.configure(state="normal" if idx > 0 else "disabled")
        self.next_btn.configure(text="Next →" if idx < 6 else "Finish ✓")

    def _prev(self):
        if self.current_step > 0: self._show_step(self.current_step - 1)

    def _next(self):
        if self.current_step < 6: self._show_step(self.current_step + 1)

    def _run_step(self):
        runners = [self._run_setup, self._run_rotation, self._run_rename,
                   self._run_move, self._run_photoshop, self._run_ftp, self._run_ai]
        self.step_status[self.current_step] = "active"; self._update_sidebar()
        self._snapshot = {}
        try:
            if hasattr(self, 'ftp_mode'): self._snapshot['ftp_mode'] = self.ftp_mode.get()
            if hasattr(self, 'url_text') and self.url_text.winfo_exists():
                self._snapshot['url_text'] = self.url_text.get("1.0", "end").strip()
            if hasattr(self, 'ai_mode_var'): self._snapshot['ai_mode'] = self.ai_mode_var.get()
            if hasattr(self, 'input_entry_var'): self._snapshot['input_folder'] = self.input_entry_var.get()
        except: pass

        def _run():
            try:
                runners[self.current_step]()
                self.step_status[self.current_step] = "done"
            except Exception as ex:
                self.step_status[self.current_step] = "error"
                self._log(f"ERROR: {ex}", error=True)
            self.root.after(0, self._update_sidebar)
        threading.Thread(target=_run, daemon=True).start()

    # ─── UI helpers ───────────────────────────────────────────────────────

    def _header(self, text, desc=""):
        tk.Label(self.step_frame, text=text, font=("Helvetica", 20, "bold"),
                 bg=C["bg"], fg=C["bright"]).pack(anchor="w", pady=(0, 5))
        if desc:
            tk.Label(self.step_frame, text=desc, font=("Helvetica", 11), bg=C["bg"],
                     fg=C["dim"], wraplength=700, justify="left").pack(anchor="w", pady=(0, 15))

    def _make_log(self):
        self.log_text = scrolledtext.ScrolledText(
            self.step_frame, font=("Courier", 10), bg=C["input"], fg=C["text"],
            insertbackground=C["text"], relief="flat", height=12, state="disabled")
        self.log_text.pack(fill="both", expand=True, pady=(10, 0))
        for tag, color in [("error", C["red"]), ("success", C["green"]),
                           ("warn", C["yellow"]), ("title", C["accent2"])]:
            self.log_text.tag_configure(tag, foreground=color)

    def _log(self, msg, error=False, success=False, warn=False, title=False):
        tag = "error" if error else "success" if success else "warn" if warn else "title" if title else None
        def _do():
            if not hasattr(self, 'log_text') or not self.log_text.winfo_exists(): return
            self.log_text.configure(state="normal")
            self.log_text.insert("end", msg + "\n", tag)
            self.log_text.see("end"); self.log_text.configure(state="disabled")
        self.root.after(0, _do)

    # ─── Step 1: Setup ────────────────────────────────────────────────────

    def _build_setup(self):
        self._header("Step 1: Setup", "Select batch type. Input/output folders are auto-configured.")

        form = tk.Frame(self.step_frame, bg=C["bg"])
        form.pack(fill="x", pady=10)

        # Batch type
        tk.Label(form, text="Batch Type:", font=("Helvetica", 11, "bold"),
                 bg=C["bg"], fg=C["text"]).grid(row=0, column=0, sticky="w", pady=8)
        tf = tk.Frame(form, bg=C["bg"])
        tf.grid(row=0, column=1, sticky="w", padx=10)
        for bt in ["SM", "SO", "MM"]:
            tk.Radiobutton(tf, text=bt, variable=self.batch_type, value=bt,
                           font=("Helvetica", 12, "bold"), bg=C["bg"], fg=C["text"],
                           selectcolor=C["card"]).pack(side="left", padx=10)

        # Input folder
        tk.Label(form, text="Input Folder:", font=("Helvetica", 11, "bold"),
                 bg=C["bg"], fg=C["text"]).grid(row=1, column=0, sticky="w", pady=8)
        inf = tk.Frame(form, bg=C["bg"])
        inf.grid(row=1, column=1, sticky="w", padx=10)
        self.input_entry_var = tk.StringVar(value=self.config.get("input_folder", ""))
        tk.Entry(inf, textvariable=self.input_entry_var, font=("Helvetica", 11),
                 bg=C["input"], fg=C["text"], insertbackground=C["text"],
                 relief="flat", bd=5, width=50).pack(side="left")
        tk.Button(inf, text="Browse", font=("Helvetica", 10), bg=C["card"],
                  fg=C["text"], bd=0, padx=12, pady=4, cursor="hand2",
                  command=self._browse_input).pack(side="left", padx=5)

        # Output folder
        tk.Label(form, text="Output Folder:", font=("Helvetica", 11, "bold"),
                 bg=C["bg"], fg=C["text"]).grid(row=2, column=0, sticky="w", pady=8)
        outf = tk.Frame(form, bg=C["bg"])
        outf.grid(row=2, column=1, sticky="w", padx=10)
        self.output_entry_var = tk.StringVar(value=self.config.get("output_folder", ""))
        tk.Entry(outf, textvariable=self.output_entry_var, font=("Helvetica", 11),
                 bg=C["input"], fg=C["text"], insertbackground=C["text"],
                 relief="flat", bd=5, width=50).pack(side="left")
        tk.Button(outf, text="Browse", font=("Helvetica", 10), bg=C["card"],
                  fg=C["text"], bd=0, padx=12, pady=4, cursor="hand2",
                  command=self._browse_output).pack(side="left", padx=5)

        # Preview
        preview = tk.LabelFrame(self.step_frame, text=" Folder Structure ",
                                font=("Helvetica", 11, "bold"), bg=C["bg2"],
                                fg=C["dim"], bd=1, relief="groove", padx=15, pady=10)
        preview.pack(fill="x", pady=15)
        self.preview_label = tk.Label(preview, text="", font=("Courier", 10),
                                      bg=C["bg2"], fg=C["text"], justify="left", anchor="w")
        self.preview_label.pack(anchor="w")

        def update_preview(*_):
            bt = self.batch_type.get()
            bp = get_batch_prefix(bt)
            inp = self.input_entry_var.get()
            out = self.output_entry_var.get()
            self.preview_label.configure(text=(
                f"Input:    {inp}\n"
                f"High Res: {out}\\high res {CURRENT_YEAR}\\{bp}\\\n"
                f"Low Res:  {out}\\low res {CURRENT_YEAR}\\{bp}\\\n"
                f"Batch:    {bp}\n"
                f"Files:    {bp}_0001.jpg, {bp}_0002.jpg, ..."
            ))
        self.batch_type.trace_add("write", update_preview)
        update_preview()
        self._make_log()

    def _browse_input(self):
        p = filedialog.askdirectory()
        if p:
            self.input_entry_var.set(p)
            self.config["input_folder"] = p; save_config(self.config)

    def _browse_output(self):
        p = filedialog.askdirectory()
        if p:
            self.output_entry_var.set(p)
            self.config["output_folder"] = p; save_config(self.config)

    def _run_setup(self):
        bt = self.batch_type.get()
        bp = get_batch_prefix(bt)
        self.batch_prefix.set(bp)

        # Sync from UI
        inp = self._snapshot.get('input_folder', self.config.get("input_folder", ""))
        if inp:
            self.config["input_folder"] = inp; save_config(self.config)
        out = self.config.get("output_folder", "")

        # Create input folder
        os.makedirs(inp, exist_ok=True)
        self.input_folder.set(inp)
        self._log(f"Input: {inp}", success=True)

        # Create output structure
        hr = os.path.join(out, f"high res {CURRENT_YEAR}", bp)
        lr = os.path.join(out, f"low res {CURRENT_YEAR}", bp)
        os.makedirs(hr, exist_ok=True)
        os.makedirs(lr, exist_ok=True)
        self.highres_path.set(hr)
        self.lowres_path.set(lr)
        self.batch_subfolder.set(hr)
        self._log(f"High Res: {hr}", success=True)
        self._log(f"Low Res:  {lr}", success=True)

        # Count images in input and find next number
        images = get_images(inp)
        self.file_count.set(len(images))

        # Check existing files in highres to find next number
        last_in_folder = find_last_number_in_folder(hr, bp)
        # Also check CSV
        csv_path = os.path.join(out, f"{bp}.csv")
        last_in_csv = find_last_number_in_csv(csv_path, bp)
        next_n = max(last_in_folder, last_in_csv) + 1
        self.next_number.set(next_n)

        self._log(f"\nBatch: {bp}")
        self._log(f"Images in input: {len(images)}")
        self._log(f"Next file number: {next_n}")
        self._log("Ready.", success=True)

    # ─── Step 2: Rotation ─────────────────────────────────────────────────

    def _build_rotation(self):
        self._header("Step 2: Auto-Rotate Images",
                      "Uses local face detection (free, no API) to fix upside-down images. "
                      "Only rotates 180° if a face is detected upside down.")
        self._make_log()

    def _run_rotation(self):
        folder = self.config.get("input_folder", "")
        images = get_images(folder)
        if not images:
            self._log("No images in input folder.", error=True); raise ValueError("No images")

        # Auto-install opencv
        try:
            import cv2
        except ImportError:
            self._log("Installing OpenCV (one time)...", warn=True)
            subprocess.check_call([sys.executable, "-m", "pip", "install", "opencv-python-headless", "--quiet"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            import cv2

        # Load face detector
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        face_cascade = cv2.CascadeClassifier(cascade_path)

        self._log(f"Checking {len(images)} images for upside-down faces...")
        rotated = 0

        for p in images:
            fn = os.path.basename(p)
            try:
                img_cv = cv2.imread(p)
                if img_cv is None:
                    self._log(f"  ? {fn} (could not read)", warn=True)
                    continue

                # Resize for faster detection
                h, w = img_cv.shape[:2]
                scale = min(1.0, 800 / max(h, w))
                small = cv2.resize(img_cv, (int(w * scale), int(h * scale)))
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

                # Try detecting face normally
                faces_normal = face_cascade.detectMultiScale(gray, scaleFactor=1.1,
                    minNeighbors=5, minSize=(30, 30))

                if len(faces_normal) > 0:
                    self._log(f"  ✓ {fn} (face OK)")
                    continue

                # No face found — try rotated 180°
                gray_flipped = cv2.rotate(gray, cv2.ROTATE_180)
                faces_flipped = face_cascade.detectMultiScale(gray_flipped, scaleFactor=1.1,
                    minNeighbors=5, minSize=(30, 30))

                if len(faces_flipped) > 0:
                    # Face found when flipped = image is upside down
                    img_pil = Image.open(p)
                    img_pil = img_pil.rotate(180, expand=True)
                    img_pil.save(p, quality=95)
                    img_pil.close()
                    self._log(f"  ↻ {fn} — rotated 180° (was upside down)", success=True)
                    rotated += 1
                else:
                    # No face either way — leave it alone
                    self._log(f"  ✓ {fn} (no face detected)")

            except Exception as e:
                self._log(f"  ✕ {fn}: {e}", error=True)

        self._log(f"\n{rotated} images rotated.", success=True)

    # ─── Step 3: Rename ───────────────────────────────────────────────────

    def _build_rename(self):
        bp = self.batch_prefix.get() or get_batch_prefix(self.batch_type.get())
        self._header("Step 3: Rename",
                      f"Renames to {bp}_0001.jpg, {bp}_0002.jpg, ... continuing from last number.")
        self._make_log()

    def _run_rename(self):
        folder = self.config.get("input_folder", "")
        images = get_images(folder)
        if not images:
            self._log("No images.", error=True); raise ValueError("No images")

        bp = self.batch_prefix.get() or get_batch_prefix(self.batch_type.get())
        n = self.next_number.get()
        self._log(f"Renaming {len(images)} images starting at {bp}_{n:04d}...")

        # Temp rename to avoid collisions
        temp = []
        for i, p in enumerate(images):
            tp = os.path.join(folder, f"__tmp_{i:06d}__.jpg")
            os.rename(p, tp)
            temp.append((tp, os.path.basename(p)))

        for tp, orig in temp:
            new_name = f"{bp}_{n:04d}.jpg"
            os.rename(tp, os.path.join(folder, new_name))
            self._log(f"  {orig} → {new_name}", success=True)
            n += 1

        self.file_count.set(len(temp))
        self.next_number.set(n)
        self._log(f"Done. Next number will be {n:04d}.", success=True)

    # ─── Step 4: Move Files ───────────────────────────────────────────────

    def _build_move(self):
        self._header("Step 4: Copy to HIGH RES",
                      "Copies renamed files from input to high res output folder.")
        self._make_log()

    def _run_move(self):
        inp = self.config.get("input_folder", "")
        hr = self.highres_path.get()
        if not hr: self._log("Run Setup first.", error=True); raise ValueError("No path")
        images = get_images(inp)
        self._log(f"Copying {len(images)} files to {hr}...")
        os.makedirs(hr, exist_ok=True)
        for p in images:
            fn = os.path.basename(p)
            shutil.copy2(p, os.path.join(hr, fn))
            self._log(f"  ✓ {fn}", success=True)
        self._log("Done.", success=True)

    # ─── Step 5: Photoshop / Resize ───────────────────────────────────────

    def _build_photoshop(self):
        self._header("Step 5: Create Low Res Copies",
                      "Resizes images for eBay upload (800px longest side, quality 90).")
        self._make_log()

    def _run_photoshop(self):
        hr = self.highres_path.get()
        lr = self.lowres_path.get()
        if not hr or not lr: self._log("Run Setup first.", error=True); raise ValueError("No path")
        images = get_images(hr)
        target = int(self.config.get("low_res_longest_side", 800))
        self._log(f"Resizing {len(images)} images to {target}px...")
        os.makedirs(lr, exist_ok=True)
        for p in images:
            fn = os.path.basename(p)
            try:
                img = Image.open(p)
                w, h = img.size
                if max(w, h) > target:
                    ratio = target / max(w, h)
                    nw, nh = int(w * ratio), int(h * ratio)
                else:
                    nw, nh = w, h
                img.resize((nw, nh), Image.LANCZOS).save(os.path.join(lr, fn), "JPEG", quality=90)
                self._log(f"  ✓ {fn} ({w}x{h} → {nw}x{nh})", success=True)
            except Exception as e:
                self._log(f"  ✕ {fn}: {e}", error=True)
        self._log("Done.", success=True)

    # ─── Step 6: FTP Upload ───────────────────────────────────────────────

    def _build_ftp(self):
        self._header("Step 6: FTP Upload", "Upload low res images to FTP server.")
        self.ftp_mode = tk.StringVar(value="auto")
        mf = tk.Frame(self.step_frame, bg=C["bg"])
        mf.pack(fill="x", pady=5)
        for t, v in [("Auto FTP", "auto"), ("Manual (paste URLs)", "manual")]:
            tk.Radiobutton(mf, text=t, variable=self.ftp_mode, value=v,
                           font=("Helvetica", 11), bg=C["bg"], fg=C["text"],
                           selectcolor=C["card"]).pack(side="left", padx=10)
        tk.Label(self.step_frame, text="Paste URLs (one per line) if manual:",
                 font=("Helvetica", 11, "bold"), bg=C["bg"], fg=C["text"]
                 ).pack(anchor="w", pady=(15, 5))
        self.url_text = scrolledtext.ScrolledText(
            self.step_frame, font=("Courier", 10), bg=C["input"], fg=C["text"],
            insertbackground=C["text"], relief="flat", height=6)
        self.url_text.pack(fill="x", pady=(0, 10))
        self._make_log()

    def _run_ftp(self):
        mode = self._snapshot.get('ftp_mode', 'auto')
        lr = self.lowres_path.get()
        bp = self.batch_prefix.get()
        host = self.config.get("ftp_host", "images.moviestore.com")

        if mode == "auto":
            user = self.config.get("ftp_user", "")
            pw = self.config.get("ftp_pass", "")
            if not user:
                self._log("FTP username not set. Check Settings.", error=True)
                raise ValueError("No FTP user")

            self._log(f"Connecting to {host}...")
            ftp = FTP()
            ftp.connect(host)
            ftp.login(user, pw)
            self._log("Connected!", success=True)

            # Create remote dir matching batch prefix
            remote = f"/{bp}"
            try: ftp.mkd(remote)
            except: pass
            ftp.cwd(remote)

            urls = []
            for p in get_images(lr):
                fn = os.path.basename(p)
                with open(p, 'rb') as f:
                    ftp.storbinary(f'STOR {fn}', f)
                # URL format: http://@images.moviestore.com/SM030526/SM030526_0001.jpg
                url = f"http://@{host}/{bp}/{fn}"
                urls.append(url)
                self._log(f"  ↑ {fn}", success=True)

            ftp.quit()
            self.ftp_urls.set("\n".join(urls))
            self.root.after(0, lambda: self.url_text.insert("1.0", "\n".join(urls)))
            self._log(f"\n{len(urls)} files uploaded.", success=True)
        else:
            urls = self._snapshot.get('url_text', '')
            if not urls:
                self._log("Paste URLs above, then run again.", warn=True); return
            self.ftp_urls.set(urls)
            self._log(f"Captured {len(urls.splitlines())} URLs.", success=True)

    # ─── Step 7: AI Listings + CSV ────────────────────────────────────────

    def _build_ai(self):
        self._header("Step 7: AI Listings + CSV",
                      "Regular: Google Vision → Amazon → Gemini 3x → Claude 2x\n"
                      "Fast: Google Vision → Amazon → Gemini 2x → Claude 1x")

        mode_frame = tk.Frame(self.step_frame, bg=C["bg2"], padx=15, pady=10)
        mode_frame.pack(fill="x", pady=(0, 5))
        tk.Label(mode_frame, text="Mode:", font=("Helvetica", 11, "bold"),
                 bg=C["bg2"], fg=C["text"]).pack(anchor="w")
        self.ai_mode_var = tk.StringVar(value="regular")
        mf = tk.Frame(mode_frame, bg=C["bg2"])
        mf.pack(anchor="w", pady=3)
        tk.Radiobutton(mf, text="Regular (thorough)",
                       variable=self.ai_mode_var, value="regular",
                       font=("Helvetica", 10), bg=C["bg2"], fg=C["text"],
                       selectcolor=C["card"]).pack(side="left", padx=8)
        tk.Radiobutton(mf, text="Fast (quick)",
                       variable=self.ai_mode_var, value="fast",
                       font=("Helvetica", 10), bg=C["bg2"], fg=C["text"],
                       selectcolor=C["card"]).pack(side="left", padx=8)

        key_frame = tk.Frame(self.step_frame, bg=C["bg2"], padx=15, pady=10)
        key_frame.pack(fill="x", pady=(0, 10))
        tk.Label(key_frame, text="API Keys:", font=("Helvetica", 11, "bold"),
                 bg=C["bg2"], fg=C["text"]).pack(anchor="w")
        api_checks = [
            ("Google Vision (reverse image search)", "google_vision_api_key"),
            ("Amazon Rekognition (face ID)", "aws_access_key_id"),
            ("Gemini (free, title writing)", "gemini_api_key"),
            ("Claude (smart fallback)", "anthropic_api_key"),
        ]
        for label, key_name in api_checks:
            has_key = bool(self.config.get(key_name, ""))
            status = "\u2713" if has_key else "\u2717"
            color = C["green"] if has_key else C["red"]
            tk.Label(key_frame, text=f"  {status} {label}", font=("Helvetica", 10),
                     bg=C["bg2"], fg=color).pack(anchor="w")

        bp = self.batch_prefix.get() or get_batch_prefix(self.batch_type.get())
        bt = self.batch_type.get()
        tk.Label(self.step_frame, text=f"Images from: {self.config.get('input_folder', '')}",
                 font=("Helvetica", 10), bg=C["bg"], fg=C["dim"]).pack(anchor="w")
        tk.Label(self.step_frame, text=f"CSV: {os.path.join(self.config.get('output_folder', ''), bp + '.csv')}",
                 font=("Helvetica", 10), bg=C["bg"], fg=C["dim"]).pack(anchor="w")
        tk.Label(self.step_frame, text=f"Title suffix: \"{TITLE_PREFIX.get(bt, '')}\"",
                 font=("Helvetica", 10), bg=C["bg"], fg=C["dim"]).pack(anchor="w", pady=(0, 5))
        self._make_log()

    def _make_prompt(self, known_actors="", known_movie=""):
        context = ""
        if known_actors and known_movie:
            context = f"Previous analysis: Actor(s): {known_actors}, Movie/Show: {known_movie}. Verify and add details."
        elif known_actors:
            context = f"Previous analysis identified the person as: {known_actors}. Identify the MOVIE or TV SHOW."
        elif known_movie:
            context = f"Previous analysis identified this as from: {known_movie}. Identify the ACTORS."

        return f"""You are an expert at identifying vintage movie and TV photographs.

{context}

Analyze this photograph. Look at costumes, sets, lighting, era, faces.

Respond with ONLY a JSON object on a SINGLE LINE:
{{"movie_title": "title or UNKNOWN", "year": "single year or UNKNOWN", "actors": "names or UNKNOWN", "scene_description": "setting or action"}}

RULES:
- Try HARDEST to identify both movie/TV show AND actors.
- year: single year ONLY, never a range like 1997-2001. If unsure put UNKNOWN.
- scene_description: SETTING or ACTION only. No gender words (man woman boy girl). No appearance (blonde brunette). No actor name. Under 25 chars.
- Return ONLY valid JSON on one line. No markdown no backticks."""

    def _try_provider(self, provider, config, image_b64, mtype, prompt, temperature=0.1):
        key_name, ai_fn = AI_PROVIDERS[provider]
        if not config.get(key_name, ""):
            return None
        try:
            raw = ai_fn(config, image_b64, mtype, prompt, temperature)
            result = parse_ai_json(raw)
            if not isinstance(result, dict):
                return None
            return result
        except Exception as e:
            self._log(f"    {provider}: {e}", warn=True)
            return None

    def _extract(self, data):
        """Extract non-UNKNOWN values from AI response."""
        if not data:
            return None, None, None, None
        movie = str(data.get("movie_title", "") or "")
        year = str(data.get("year", "") or "")
        actors = str(data.get("actors", "") or "")
        scene = str(data.get("scene_description", "") or "")
        if movie.upper() == "UNKNOWN": movie = ""
        if year.upper() == "UNKNOWN": year = ""
        if actors.upper() == "UNKNOWN": actors = ""
        if scene.upper() == "UNKNOWN": scene = ""
        return movie or None, year or None, actors or None, scene or None

    def _phase0_identify(self, config, image_b64, image_bytes):
        """Phase 0: Google Vision + Amazon Rekognition for factual identification."""
        found = {"movie": None, "year": None, "actors": None, "scene": None,
                 "gv_score": 0.0, "ar_confidence": 0.0}

        # ── Google Vision Web Detection ──
        gv_key = config.get("google_vision_api_key", "")
        if gv_key:
            self._log("    [Google Vision] searching web...", warn=True)
            vision_data = google_vision_web_detect(config, image_b64)
            if vision_data and "error" not in vision_data:
                movie, actor, year, score = parse_google_vision_results(vision_data)
                found["gv_score"] = score
                if movie: found["movie"] = movie
                if actor: found["actors"] = actor
                if year: found["year"] = year

                # Log what we found
                parts = []
                if movie: parts.append(f"movie={movie}")
                if actor: parts.append(f"actor={actor}")
                if year: parts.append(f"year={year}")
                if parts:
                    self._log(f"    [Google Vision] found: {', '.join(parts)} (score={score:.2f})", success=True)
                else:
                    self._log(f"    [Google Vision] no useful matches", warn=True)

                # Log matching pages
                for page in (vision_data.get("pages", []) or [])[:3]:
                    url = page.get("url", "")
                    if url:
                        self._log(f"      → {url[:80]}")
            elif vision_data and "error" in vision_data:
                self._log(f"    [Google Vision] error: {vision_data['error']}", warn=True)

        # ── Amazon Rekognition Celebrity Recognition ──
        aws_key = config.get("aws_access_key_id", "")
        if aws_key:
            # Only call Amazon if Google didn't find actor, or Google score was low
            need_amazon = not found["actors"] or found["gv_score"] < 0.5
            if need_amazon:
                self._log("    [Amazon Rekognition] identifying faces...", warn=True)
                celebs = amazon_rekognition_celebrity(config, image_bytes)
                if celebs and not any("error" in c for c in celebs):
                    for celeb in celebs:
                        name = celeb.get("name", "")
                        conf = celeb.get("confidence", 0)
                        self._log(f"    [Amazon] {name} ({conf:.0f}%)", success=True)
                        if conf >= 80 and not found["actors"]:
                            found["actors"] = name
                            found["ar_confidence"] = conf
                elif celebs and any("error" in c for c in celebs):
                    for c in celebs:
                        if "error" in c:
                            self._log(f"    [Amazon] error: {c['error']}", warn=True)
                else:
                    self._log(f"    [Amazon] no celebrity match", warn=True)
            else:
                self._log(f"    [Amazon] skipped (Google found actor)", success=True)

        return found

    def _cascade_ai_call(self, config, image_b64, mtype):
        """Regular: Google Vision → Amazon → Gemini 3x → Claude 2x (blind first)."""
        best = {"movie": None, "year": None, "actors": None, "scene": None}
        movie_votes = {}
        actor_votes = {}

        def update(movie, year, actors, scene):
            if movie:
                movie_votes[movie.lower()] = movie_votes.get(movie.lower(), 0) + 1
                if not best["movie"]: best["movie"] = movie
            if year and not best["year"]: best["year"] = year
            if actors:
                actor_votes[actors.lower()] = actor_votes.get(actors.lower(), 0) + 1
                if not best["actors"]: best["actors"] = actors
            if scene and not best["scene"]: best["scene"] = scene

        def confident_actor():
            if not actor_votes: return ""
            top = max(actor_votes, key=actor_votes.get)
            return top.title() if actor_votes[top] >= 2 else ""

        def confident_movie():
            if not movie_votes: return ""
            top = max(movie_votes, key=movie_votes.get)
            return top.title() if movie_votes[top] >= 2 else ""

        def has_both():
            return best["movie"] and best["actors"]

        def result():
            return {"movie_title": best["movie"] or "UNKNOWN",
                    "year": best["year"] or "UNKNOWN",
                    "actors": best["actors"] or "",
                    "scene_description": best["scene"] or ""}

        # ── Phase 0: Google Vision + Amazon ──
        image_bytes = base64.b64decode(image_b64)
        p0 = self._phase0_identify(config, image_b64, image_bytes)
        if p0["movie"]: update(p0["movie"], p0["year"], p0["actors"], None)
        if p0["actors"] and not best["actors"]:
            best["actors"] = p0["actors"]
            actor_votes[p0["actors"].lower()] = actor_votes.get(p0["actors"].lower(), 0) + 2  # trusted source
        if p0["movie"] and not best["movie"]:
            best["movie"] = p0["movie"]
            movie_votes[p0["movie"].lower()] = movie_votes.get(p0["movie"].lower(), 0) + 2  # trusted
        if p0["year"]: best["year"] = p0["year"]

        # If Phase 0 got both, still go to Gemini for scene description + verification
        phase0_had_both = has_both()

        # ── Phase 1: Gemini 3x ──
        gemini_temps = [0.1, 0.1, 0.5]
        if config.get("gemini_api_key", ""):
            for i in range(3):
                t = gemini_temps[i]
                prompt = self._make_prompt(confident_actor() or best["actors"] or "",
                                           confident_movie() or best["movie"] or "")
                data = self._try_provider("gemini", config, image_b64, mtype, prompt, t)
                if not data:
                    self._log(f"    gemini {i+1}/3 (t={t}): parse failed", warn=True)
                    continue
                m, y, a, s = self._extract(data)

                # If Phase 0 found actor/movie, don't let Gemini override with different answer
                if p0["actors"] and a and a.lower() != p0["actors"].lower():
                    a = None  # trust Phase 0 over Gemini
                if p0["movie"] and m and m.lower() != p0["movie"].lower():
                    m = None  # trust Phase 0

                update(m, y, a, s)

                if has_both() and (confident_movie() or phase0_had_both) and (confident_actor() or phase0_had_both):
                    self._log(f"    gemini {i+1}/3 (t={t}): confirmed", success=True)
                    return result(), "gemini"

                if m and a:
                    self._log(f"    gemini {i+1}/3 (t={t}): got both, confirming...", warn=True)
                else:
                    miss = []
                    if not best["movie"]: miss.append("movie")
                    if not best["actors"]: miss.append("actor")
                    self._log(f"    gemini {i+1}/3 (t={t}): missing {', '.join(miss) if miss else 'confirming'}", warn=True)

            if has_both() and (confident_movie() or phase0_had_both) and (confident_actor() or phase0_had_both):
                return result(), "gemini"

        # ── Phase 2: Claude 2x (blind first, then with context) ──
        claude_temps = [0.1, 0.3]
        if config.get("anthropic_api_key", ""):
            for i in range(2):
                t = claude_temps[i]
                if i == 0:
                    prompt = self._make_prompt("", "")  # blind
                else:
                    prompt = self._make_prompt(best["actors"] or "", best["movie"] or "")
                data = self._try_provider("claude", config, image_b64, mtype, prompt, t)
                if not data:
                    self._log(f"    claude {i+1}/2 (t={t}): parse failed", warn=True)
                    continue
                m, y, a, s = self._extract(data)
                if p0["actors"] and a and a.lower() != p0["actors"].lower():
                    a = None
                if p0["movie"] and m and m.lower() != p0["movie"].lower():
                    m = None
                update(m, y, a, s)

                if has_both():
                    self._log(f"    claude {i+1}/2 (t={t}): got it", success=True)
                    return result(), "claude"

                miss = []
                if not best["movie"]: miss.append("movie")
                if not best["actors"]: miss.append("actor")
                self._log(f"    claude {i+1}/2 (t={t}): still missing {', '.join(miss) if miss else 'none'}", warn=True)

        return result(), "merged"

    def _cascade_fast(self, config, image_b64, mtype):
        """Fast: Google Vision → Amazon → Gemini 2x → Claude 1x. Trusts first answers."""
        best = {"movie": None, "year": None, "actors": None, "scene": None}

        def update(movie, year, actors, scene):
            if movie and not best["movie"]: best["movie"] = movie
            if year and not best["year"]: best["year"] = year
            if actors and not best["actors"]: best["actors"] = actors
            if scene and not best["scene"]: best["scene"] = scene

        def has_both():
            return best["movie"] and best["actors"]

        def result():
            return {"movie_title": best["movie"] or "UNKNOWN",
                    "year": best["year"] or "UNKNOWN",
                    "actors": best["actors"] or "",
                    "scene_description": best["scene"] or ""}

        # ── Phase 0: Google Vision + Amazon ──
        image_bytes = base64.b64decode(image_b64)
        p0 = self._phase0_identify(config, image_b64, image_bytes)
        update(p0["movie"], p0["year"], p0["actors"], None)

        if has_both():
            # Still need scene description from Gemini
            if config.get("gemini_api_key", ""):
                prompt = self._make_prompt(best["actors"], best["movie"])
                data = self._try_provider("gemini", config, image_b64, mtype, prompt, 0.1)
                if data:
                    _, _, _, s = self._extract(data)
                    if s: best["scene"] = s
                    self._log(f"    gemini: wrote description", success=True)
            return result(), "vision"

        # Gemini 2x
        if config.get("gemini_api_key", ""):
            for i, t in enumerate([0.1, 0.5]):
                prompt = self._make_prompt(best["actors"] or "", best["movie"] or "")
                data = self._try_provider("gemini", config, image_b64, mtype, prompt, t)
                if data:
                    m, y, a, s = self._extract(data)
                    update(m, y, a, s)
                    if has_both():
                        self._log(f"    gemini {i+1}/2 (t={t}): got both", success=True)
                        return result(), "gemini"
                    self._log(f"    gemini {i+1}/2 (t={t}): incomplete", warn=True)
                else:
                    self._log(f"    gemini {i+1}/2 (t={t}): parse failed", warn=True)

        # Claude 1x
        if config.get("anthropic_api_key", ""):
            prompt = self._make_prompt(best["actors"] or "", best["movie"] or "")
            data = self._try_provider("claude", config, image_b64, mtype, prompt, 0.8)
            if data:
                m, y, a, s = self._extract(data)
                update(m, y, a, s)
                if has_both():
                    self._log(f"    claude 1/1: got it", success=True)
                    return result(), "claude"
                self._log(f"    claude 1/1: incomplete", warn=True)
            else:
                self._log(f"    claude 1/1: parse failed", warn=True)

        return result(), "fast"

    def _run_ai(self):
        mode = self._snapshot.get('ai_mode', 'regular')
        has_any_key = any(self.config.get(AI_PROVIDERS[p][0], "") for p in CASCADE_ORDER)
        if not has_any_key:
            self._log("No API keys configured. Set at least one in Settings.", error=True)
            raise ValueError("No API keys")

        folder = self.config.get("input_folder", "")
        images = get_images(folder)
        if not images:
            self._log("No images in input folder.", error=True); raise ValueError("No images")

        bt = self.batch_type.get()
        bp = self.batch_prefix.get() or get_batch_prefix(bt)
        out = self.config.get("output_folder", "")
        host = self.config.get("ftp_host", "images.moviestore.com")
        max_chars = int(self.config.get("max_title_chars", 80))
        description = DESCRIPTIONS.get(bt, DESC_SO)

        csv_path = os.path.join(out, f"{bp}.csv")
        fields = ["SKU", "eBay Title", "Title", "PicURL", "Description"]

        last_n = find_last_number_in_csv(csv_path, bp)
        hr = self.highres_path.get() or os.path.join(out, f"high res {CURRENT_YEAR}", bp)
        last_folder = find_last_number_in_folder(hr, bp)

        file_numbers = []
        for p in images:
            fn = os.path.basename(p)
            match = re.match(rf'{re.escape(bp)}[_-](\d+)', fn)
            if match:
                file_numbers.append((int(match.group(1)), p, fn))
            else:
                self._log(f"  ⚠ {fn} not in expected format, skipping", warn=True)

        new_files = [(n, p, fn) for n, p, fn in sorted(file_numbers) if n > last_n]
        if not new_files:
            self._log("No new images to process (all already in CSV).", warn=True); return

        available = [p for p in CASCADE_ORDER if self.config.get(AI_PROVIDERS[p][0], "")]
        self._log(f"Mode: {mode.upper()}")
        self._log(f"Processing {len(new_files)} new images")
        gv = "✓" if self.config.get("google_vision_api_key", "") else "✕"
        ar = "✓" if self.config.get("aws_access_key_id", "") else "✕"
        self._log(f"Google Vision: {gv} | Amazon Rekognition: {ar}")
        if mode == "fast":
            self._log(f"Cascade: Vision → Amazon → Gemini x2 → Claude x1")
        else:
            self._log(f"Cascade: Vision → Amazon → Gemini x3 → Claude x2")
        self._log(f"Title suffix: \"{TITLE_PREFIX.get(bt, '')}\"")
        self._log(f"CSV: {csv_path}\n")

        new_rows = []
        errors = 0
        provider_stats = {}

        for i, (num, img_path, fname) in enumerate(new_files):
            self._log(f"[{i+1}/{len(new_files)}] {fname}")
            try:
                resized = resize_for_api(img_path)
                if resized:
                    image_b64 = base64.b64encode(resized).decode("utf-8")
                    mtype = "image/jpeg"
                else:
                    with open(img_path, "rb") as f:
                        image_b64 = base64.b64encode(f.read()).decode("utf-8")
                    ext = os.path.splitext(img_path)[1].lower()
                    mtype = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                             ".tif": "image/tiff"}.get(ext, "image/jpeg")

                if mode == "fast":
                    data, used = self._cascade_fast(self.config, image_b64, mtype)
                else:
                    data, used = self._cascade_ai_call(self.config, image_b64, mtype)
                provider_stats[used] = provider_stats.get(used, 0) + 1

                movie = str(data.get("movie_title", "UNKNOWN") or "UNKNOWN")
                year = str(data.get("year", "UNKNOWN") or "UNKNOWN")
                actors = str(data.get("actors", "") or "")
                scene = str(data.get("scene_description", "") or "")
                title = build_title(bt, movie, year, actors, scene, max_chars)

                sku = f"{bp}/{bp}_{num:04d}"
                pic_url = f"http://@{host}/{bp}/{bp}_{num:04d}.jpg"
                row = {"SKU": sku, "eBay Title": title, "Title": title,
                       "PicURL": pic_url, "Description": description}
                new_rows.append(row)

                via = f" via {used.upper()}" if used != "gemini" else ""
                self._log(f"  → {movie} ({year}) {actors}{via}", success=True)
                self._log(f"  [{len(title)} chars] {title}", title=True)

            except Exception as e:
                errors += 1
                self._log(f"  ✕ All providers failed: {e}", error=True)
                sku = f"{bp}/{bp}_{num:04d}"
                title = f"{fname.split('.')[0]} {TITLE_PREFIX.get(bt, 'Vintage 8x10 Inch Photo').strip()}".title()
                new_rows.append({"SKU": sku, "eBay Title": title, "Title": title,
                    "PicURL": f"http://@{host}/{bp}/{bp}_{num:04d}.jpg",
                    "Description": description})

        file_exists = os.path.isfile(csv_path)
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            if not file_exists: w.writeheader()
            w.writerows(new_rows)

        self._log(f"\n{'─'*50}")
        self._log(f"Done! {len(new_rows)} rows added ({errors} errors).", success=True)
        for prov, count in provider_stats.items():
            self._log(f"  {prov.upper()}: {count} images", success=True)
        self._log(f"CSV: {csv_path}", success=True)

    # ─── Settings ─────────────────────────────────────────────────────────

    def _open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("Settings"); win.geometry("650x550"); win.configure(bg=C["bg"])
        win.transient(self.root)

        canvas = tk.Canvas(win, bg=C["bg"], highlightthickness=0)
        sb = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=C["bg"])
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True, padx=20, pady=10)
        sb.pack(side="right", fill="y")

        entries = {}

        def section(t):
            tk.Label(inner, text=t, font=("Helvetica", 13, "bold"), bg=C["bg"],
                     fg=C["accent"]).pack(anchor="w", pady=(15, 5))
            tk.Frame(inner, bg=C["border"], height=1).pack(fill="x", pady=(0, 8))

        def field(k, l, show=""):
            fr = tk.Frame(inner, bg=C["bg"]); fr.pack(fill="x", pady=3)
            tk.Label(fr, text=l, font=("Helvetica", 11), bg=C["bg"], fg=C["text"],
                     width=25, anchor="w").pack(side="left")
            v = tk.StringVar(value=str(self.config.get(k, "")))
            tk.Entry(fr, textvariable=v, font=("Helvetica", 11), bg=C["input"],
                     fg=C["text"], insertbackground=C["text"], relief="flat", bd=5,
                     width=40, show=show).pack(side="left", fill="x", expand=True)
            entries[k] = v

        section("Folders")
        field("input_folder", "Input Folder")
        field("output_folder", "Output Folder")

        section("FTP")
        field("ftp_host", "FTP Host")
        field("ftp_user", "FTP Username")
        field("ftp_pass", "FTP Password", show="•")

        section("Resize")
        field("low_res_longest_side", "Low Res Target (px)")

        section("Image Identification")
        field("google_vision_api_key", "Google Vision API Key", show="•")
        field("aws_access_key_id", "AWS Access Key ID", show="•")
        field("aws_secret_access_key", "AWS Secret Access Key", show="•")
        field("aws_region", "AWS Region")

        section("AI Title Generation")
        field("gemini_api_key", "Gemini API Key", show="•")
        field("gemini_model", "Gemini Model")
        field("openai_api_key", "OpenAI API Key", show="•")
        field("anthropic_api_key", "Claude API Key", show="•")
        field("anthropic_model", "Claude Model")
        field("max_title_chars", "Max Title Characters")

        def save():
            for k, v in entries.items():
                val = v.get()
                if k in ("low_res_longest_side", "max_title_chars"):
                    try: val = int(val)
                    except: pass
                self.config[k] = val
            save_config(self.config); win.destroy()

        bf = tk.Frame(inner, bg=C["bg"]); bf.pack(fill="x", pady=20)
        tk.Button(bf, text="Save", font=("Helvetica", 12, "bold"), bg=C["green"],
                  fg="#fff", bd=0, padx=24, pady=8, cursor="hand2", command=save).pack(side="right")
        tk.Button(bf, text="Cancel", font=("Helvetica", 12), bg=C["card"],
                  fg=C["text"], bd=0, padx=16, pady=6, cursor="hand2",
                  command=win.destroy).pack(side="right", padx=8)


def main():
    root = tk.Tk()
    ttk.Style().theme_use("clam")
    ScanningWizard(root)
    root.mainloop()

if __name__ == "__main__":
    main()
