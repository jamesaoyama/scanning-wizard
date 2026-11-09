"""The update check and download (GitHub is faked), and the version numbers."""
import hashlib
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import wizard_core as core  # noqa: E402
import wizard_updates as up  # noqa: E402

PAYLOAD = b"installer bytes " * 5000


class Resp:
    def __init__(self, status, data=None, body=b""):
        self.status_code, self._data, self.body = status, data, body

    def json(self):
        return self._data

    def iter_content(self, size):
        for i in range(0, len(self.body), size):
            yield self.body[i:i + size]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        return self.responses.pop(0)


def release_json(tag="v4.1.0", digest=None, size=len(PAYLOAD)):
    return {"tag_name": tag, "html_url": "https://github.com/x/releases/tag/" + tag, "body": "notes",
            "assets": [{"name": "Other.zip"}, {
                "name": "ScanningWizardSetup.exe", "size": size,
                "browser_download_url": "https://github.com/x/releases/download/v/ScanningWizardSetup.exe",
                "digest": digest or "sha256:" + hashlib.sha256(PAYLOAD).hexdigest()}]}


def test_versions_match_and_compare():
    iss = (ROOT / "installer" / "ScanningWizard.iss").read_text(encoding="utf-8")
    assert re.search(r'#define AppVersion "([^"]+)"', iss).group(1) == core.APP_VERSION
    assert up.is_newer("4.0.1", "4.0.0") and up.is_newer("v4.10.0", "4.9.9")
    assert not up.is_newer("4.0.0", "4.0.0") and not up.is_newer("3.9", "4.0.0")


def test_latest_release_reads_the_installer_asset():
    s = Session(Resp(200, release_json()))
    r = up.latest_release("4.0.0", s)
    assert (r.version, r.size) == ("4.1.0", len(PAYLOAD))
    assert r.sha256 == hashlib.sha256(PAYLOAD).hexdigest() and r.url.endswith("ScanningWizardSetup.exe")
    assert s.calls[0][1]["headers"]["User-Agent"] == "ScanningWizard/4.0.0"


@pytest.mark.parametrize("resp, message", [
    (Resp(404), "No versions"), (Resp(500), "HTTP 500"),
    (Resp(200, {"tag_name": "v4.1.0", "assets": []}), "no installer"),
])
def test_latest_release_problems(resp, message):
    with pytest.raises(up.UpdateError, match=message):
        up.latest_release("4.0.0", Session(resp))


def test_download_checks_size_and_digest(tmp_path):
    rel = up.latest_release("4.0.0", Session(Resp(200, release_json())))
    dest = tmp_path / "Setup.exe"
    seen = []
    up.download(rel, str(dest), "4.0.0", progress=lambda got, total: seen.append(got),
                session=Session(Resp(200, body=PAYLOAD)))
    assert dest.read_bytes() == PAYLOAD and seen[-1] == len(PAYLOAD)

    bad = up.latest_release("4.0.0", Session(Resp(200, release_json(digest="sha256:" + "0" * 64))))
    with pytest.raises(up.UpdateError, match="didn't match"):
        up.download(bad, str(tmp_path / "b.exe"), "4.0.0", session=Session(Resp(200, body=PAYLOAD)))
    short = up.latest_release("4.0.0", Session(Resp(200, release_json(size=len(PAYLOAD) + 1))))
    with pytest.raises(up.UpdateError, match="incomplete"):
        up.download(short, str(tmp_path / "c.exe"), "4.0.0", session=Session(Resp(200, body=PAYLOAD)))
    with pytest.raises(up.UpdateError, match="cancelled"):
        up.download(rel, str(tmp_path / "d.exe"), "4.0.0", cancelled=lambda: True,
                    session=Session(Resp(200, body=PAYLOAD)))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Setup.exe"]     # no half files left


def test_how_often_to_check(tmp_path, monkeypatch):
    state = tmp_path / "update.json"
    assert up.check_due(state)
    up.record_check(state, now=1000)
    assert not up.check_due(state, now=1000 + 5 * 3600) and up.check_due(state, now=1000 + 6 * 3600)
    monkeypatch.setenv("SCANNING_WIZARD_UPDATE_URL", "off")
    assert not up.checks_enabled()
    assert up.install_command("S.exe")[-1] == "/RELAUNCH=1"
