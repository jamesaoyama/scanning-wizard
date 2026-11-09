"""
Checking for and installing new versions.

New versions are GitHub releases on the public jamesaoyama/scanning-wizard
repo. The program asks GitHub for the latest release, compares version
numbers, downloads ScanningWizardSetup.exe, checks its size and SHA-256
digest, then runs it silently. The installer closes this copy, updates it
and opens it again.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("scanning_wizard")

REPO = "jamesaoyama/scanning-wizard"
# GitHub's permanent ID for the repo: the check keeps working if the repo or
# its owner is renamed or moved. (Not if it's made private: private releases
# need a GitHub login.)
REPO_ID = 1451976104
API_URL = (f"https://api.github.com/repositories/{REPO_ID}/releases/latest" if REPO_ID
           else f"https://api.github.com/repos/{REPO}/releases/latest")
DOWNLOAD_PAGE = f"https://github.com/{REPO}/releases/latest"
ASSET_NAME = "ScanningWizardSetup.exe"
CHECK_EVERY_HOURS = 6


class UpdateError(Exception):
    pass


def api_url() -> str:
    return os.environ.get("SCANNING_WIZARD_UPDATE_URL", API_URL)


def checks_enabled() -> bool:
    return api_url().lower() != "off"


def parse_version(text: str) -> tuple:
    parts = re.findall(r"\d+", text or "")
    return tuple(int(p) for p in parts[:3]) or (0,)


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


@dataclass
class Release:
    version: str
    url: str
    size: int
    sha256: str | None
    notes: str
    page: str


def _agent(version: str) -> dict:
    return {"User-Agent": f"ScanningWizard/{version}"}


def latest_release(version: str, session=None) -> Release:
    import requests
    http = session or requests
    try:
        r = http.get(api_url(), timeout=15,
                     headers={"Accept": "application/vnd.github+json", **_agent(version)})
    except requests.RequestException:
        raise UpdateError("Couldn't check for updates (no internet connection?).")
    if r.status_code == 404:
        raise UpdateError("No versions have been published yet.")
    if r.status_code != 200:
        raise UpdateError(f"Couldn't check for updates (HTTP {r.status_code}).")
    data = r.json()
    asset = next((a for a in data.get("assets", []) if a.get("name") == ASSET_NAME), None)
    if not asset:
        raise UpdateError("The latest version has no installer attached yet.")
    digest = asset.get("digest") or ""
    return Release(
        version=(data.get("tag_name") or "").lstrip("vV"),
        url=asset.get("browser_download_url", ""),
        size=int(asset.get("size") or 0),
        sha256=digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else None,
        notes=data.get("body") or "",
        page=data.get("html_url") or DOWNLOAD_PAGE,
    )


def download(release: Release, dest: str, version: str, progress=None, cancelled=None, session=None) -> str:
    """Download the installer to `dest`, checking size and digest. Returns `dest`."""
    import requests
    http = session or requests
    part = dest + ".part"
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    digest = hashlib.sha256()
    got = 0
    try:
        with http.get(release.url, stream=True, timeout=(15, 120), headers=_agent(version)) as r:
            if r.status_code != 200:
                raise UpdateError(f"Download failed (HTTP {r.status_code}).")
            with open(part, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    if cancelled and cancelled():
                        raise UpdateError("Download cancelled.")
                    if chunk:
                        f.write(chunk)
                        digest.update(chunk)
                        got += len(chunk)
                        if progress:
                            progress(got, release.size)
    except requests.RequestException:
        if os.path.exists(part):
            os.remove(part)
        raise UpdateError("The download was interrupted. Please try again.")
    except UpdateError:
        if os.path.exists(part):
            os.remove(part)
        raise
    if release.size and got != release.size:
        os.remove(part)
        raise UpdateError("The download was incomplete. Please try again.")
    if release.sha256 and digest.hexdigest() != release.sha256:
        os.remove(part)
        raise UpdateError("The downloaded installer didn't match. Please try again.")
    os.replace(part, dest)
    return dest


def can_install_itself() -> bool:
    """Only the installed program (the built .exe on Windows) can update itself.
    Run from the .py files, it opens the download page instead."""
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def install_command(installer: str) -> list[str]:
    # /RELAUNCH=1 makes the installer open Scanning Wizard again afterwards (installer/ScanningWizard.iss)
    return [installer, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/RELAUNCH=1"]


# --- how often to check

def check_due(state_path: Path, hours: float = CHECK_EVERY_HOURS, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    try:
        last = float(json.loads(Path(state_path).read_text(encoding="utf-8")).get("last_check", 0))
    except Exception:
        return True
    return now - last >= hours * 3600


def record_check(state_path: Path, now: float | None = None) -> None:
    Path(state_path).parent.mkdir(parents=True, exist_ok=True)
    Path(state_path).write_text(json.dumps({"last_check": time.time() if now is None else now}),
                                encoding="utf-8")
