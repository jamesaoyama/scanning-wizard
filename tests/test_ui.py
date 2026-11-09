"""Drives the real window through a whole batch: Start, Working, Review,
Settings, Save and Done. AI services are faked; files and folders are real."""
import csv
import json
import sys
import time
from pathlib import Path

import pytest

tk = pytest.importorskip("tkinter")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wizard_core as core  # noqa: E402
import scanning_wizard as sw  # noqa: E402
from test_core import MemKeyring, photo  # noqa: E402


class FakeIdentifier:
    calls = 0

    def __init__(self, cfg, log_fn=None, stop=None, **kw):
        pass

    def has_any_service(self):
        return True

    def identify(self, path, url=None):
        FakeIdentifier.calls += 1
        if FakeIdentifier.calls == 2:
            return {"movie": "", "year": "", "actors": ["Julia Roberts"], "scene": "hotel", "confidence": "low",
                    "sources": ["Gemini"], "warnings": ["No film or show found"], "clues": ""}
        return {"movie": "Rain Man", "year": "1988", "actors": ["Tom Cruise", "Dustin Hoffman"],
                "scene": "casino", "confidence": "high", "sources": ["Google", "Amazon", "Gemini", "Film check"],
                "warnings": [], "clues": ""}


def pump(root, until, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        root.update()
        if until():
            return
        time.sleep(0.02)
    raise AssertionError("timed out waiting")


@pytest.fixture
def app(tmp_path, monkeypatch):
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps({
        "input_folder": str(tmp_path / "scans"), "output_folder": str(tmp_path / "finished"),
        "upload_mode": "manual", "film_check": False, "gemini_api_key": "g", "batch_type": "SM",
        "picture_url_template": "https://images.example.com/{batch}/{file}"}))
    monkeypatch.setattr(core, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(core.SecretStore, "__init__", lambda self, backend=None: setattr(self, "backend", MemKeyring()))
    monkeypatch.setattr(core, "check_link", lambda url, session=None: (True, "Picture links work."))
    monkeypatch.setattr(core, "Identifier", FakeIdentifier)
    monkeypatch.setattr(core, "recycle_scan", lambda path, archive: path.unlink() or "archive")
    FakeIdentifier.calls = 0
    shown = {"errors": [], "asked": [], "info": []}
    monkeypatch.setattr(sw.messagebox, "askyesno", lambda *a, **k: shown["asked"].append(a) or True)
    monkeypatch.setattr(sw.messagebox, "showerror", lambda *a, **k: shown["errors"].append(a))
    monkeypatch.setattr(sw.messagebox, "showwarning", lambda *a, **k: shown["errors"].append(a))
    monkeypatch.setattr(sw.messagebox, "showinfo", lambda *a, **k: shown["info"].append(a))
    scans = tmp_path / "scans"
    photo(scans / "a.jpg", (900, 1200))
    photo(scans / "b.jpg", (1200, 900))
    (scans / "broken.jpg").write_bytes(b"not a picture")
    sw.core = core
    sw.FAMILY = "Helvetica"
    root = tk.Tk()
    root.withdraw()
    a = sw.App(root, [])
    a.shown = shown
    yield a
    root.destroy()


def texts(widget):
    out = []
    for w in widget.winfo_children():
        try:
            out.append(str(w.cget("text")))
        except tk.TclError:
            pass
        out += texts(w)
    return out


def test_a_whole_batch_through_the_window(app, tmp_path):
    root = app.root
    start = app.screen
    assert isinstance(start, sw.StartScreen)
    assert start.count_label.cget("text") == "3 new scans ready"
    assert start.start_btn.enabled and start.start_btn.cget("text") == "Start 3 photos ▶"
    assert any("Gemini" in t for t in texts(app.body))

    start.pick_type("SO")
    assert json.loads(core.CONFIG_PATH.read_text())["batch_type"] == "SO"
    start.start()
    assert isinstance(app.screen, sw.WorkingScreen)
    pump(root, lambda: isinstance(app.screen, sw.ReviewScreen))

    review = app.screen
    assert len(review.rows) == 2
    shown = texts(app.body)
    assert any("Couldn't use: broken.jpg" in t for t in shown)
    first, second = review.rows
    assert first.vars["title"].get() == "Rain Man 1988 Tom Cruise Dustin Hoffman Casino Original 8x10 Inch Photo"
    assert first.flag.cget("bg") == sw.C["green"] and second.flag.cget("bg") == sw.C["yellow"]
    assert "Show only the 1 to check" == review.filter_btn.cget("text")

    # editing a field rebuilds the title; typing a title keeps it
    second.vars["movie"].set("Pretty Woman")
    second.vars["year"].set("1990")
    assert second.vars["title"].get() == "Pretty Woman 1990 Julia Roberts Hotel Original 8x10 Inch Photo"
    second.vars["title"].set("Pretty Woman 1990 Julia Roberts Original 8x10 Inch Photo")
    assert second.item["title_custom"] and second.rebuild_btn.winfo_manager() == "pack"
    second.vars["scene"].set("lobby")
    assert second.vars["title"].get() == "Pretty Woman 1990 Julia Roberts Original 8x10 Inch Photo"
    second.rebuild_title(force=True)
    assert second.vars["title"].get() == "Pretty Woman 1990 Julia Roberts Lobby Original 8x10 Inch Photo"

    # turning a picture
    high = review.batch.high_path(first.item)
    from PIL import Image
    with Image.open(high) as im:
        before = im.size
    first.turn()
    pump(root, lambda: not app.busy() and first.turn_btn.enabled)
    with Image.open(high) as im:
        assert im.size == before[::-1]
    pump(root, lambda: first.photo is not None)

    # look again
    second.look_again()
    pump(root, lambda: not app.busy() and second.again_btn.enabled)
    assert second.vars["movie"].get() == "Rain Man"

    # filter and include
    review.toggle_filter()
    review.toggle_filter()
    first.toggle_include()
    assert review.count_label.cget("text").startswith("1 will go in")
    first.toggle_include()

    # settings: change a model, test a key, save
    app.open_settings(2)
    dialog = [w for w in root.winfo_children() if isinstance(w, sw.SettingsDialog)][0]
    dialog.vars["gemini_model"].set("gemini-3.5-flash-lite")
    result = tk.Label(dialog)
    core_test = core.test_service
    try:
        core.test_service = lambda name, cfg: (True, f"{name} ok with {cfg['gemini_model']}")
        dialog.run_test("gemini", result)
        pump(root, lambda: "ok" in result.cget("text"))
    finally:
        core.test_service = core_test
    assert result.cget("text") == "✓ gemini ok with gemini-3.5-flash-lite"
    dialog.save()
    assert app.cfg["gemini_model"] == "gemini-3.5-flash-lite"
    assert app.cfg["ftp_security"] == "auto" and app.cfg["ai_order"] == "gemini,openai,claude"
    saved = json.loads(core.CONFIG_PATH.read_text())
    assert "gemini_api_key" not in saved and saved["gemini_model"] == "gemini-3.5-flash-lite"

    # save and finish
    review.finish()
    pump(root, lambda: isinstance(app.screen, sw.DoneScreen))
    done = texts(app.body)
    assert any("2 rows added to" in t for t in done)
    assert any("couldn't be opened: broken.jpg" in t for t in done)
    batch = review.batch
    with open(batch.csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [r["Title"] for r in rows] == [
        "Rain Man 1988 Tom Cruise Dustin Hoffman Casino Original 8x10 Inch Photo",
        "Rain Man 1988 Tom Cruise Dustin Hoffman Casino Original 8x10 Inch Photo"]
    assert all(r["Description"] == core.DESC_SO for r in rows)
    assert sorted(p.name for p in (tmp_path / "scans").iterdir()) == ["broken.jpg"]

    # back to the start: nothing new, and the broken file is named
    app.show_start()
    assert app.screen.count_label.cget("text") == "No new scans yet"
    assert "Couldn't open: broken.jpg" in app.screen.count_note.cget("text")
    assert not app.screen.start_btn.enabled
    assert not app.shown["errors"], app.shown["errors"]


def test_stop_and_continue(app):
    root = app.root
    app.screen.start()
    working = app.screen
    pump(root, lambda: working.current in ("high", "low", "upload", "identify"))
    working.stop()
    pump(root, lambda: not app.busy() and (isinstance(app.screen, sw.ReviewScreen)
                                            or "Stopped" in working.message.cget("text")))
    if isinstance(app.screen, sw.WorkingScreen):
        assert "Stopped" in working.message.cget("text")
        app.show_start()
        unfinished = core.Batch.unfinished(app.cfg["output_folder"])
        assert len(unfinished) == 1
        app.screen.cont(unfinished[0])
        pump(root, lambda: isinstance(app.screen, sw.ReviewScreen))
    assert len(app.screen.rows) == 2
    assert not app.shown["errors"], app.shown["errors"]


def test_missing_ai_key_blocks_start(app):
    app.cfg["gemini_api_key"] = ""
    app.show_start()
    assert not app.screen.start_btn.enabled
    assert any("No AI key" in t for t in texts(app.body))


def test_one_failing_handler_does_not_freeze_the_window(app):
    root = app.root
    seen = []

    def bad(kind, data):
        raise KeyError("boom")

    app.on_event = bad
    app.emit("log", text="x")
    pump(root, lambda: app.shown["errors"])
    app.on_event = lambda kind, data: seen.append(kind)
    app.emit("log", text="y")
    app.call_ui(seen.append, "ui call")
    pump(root, lambda: seen == ["log", "ui call"])


def test_rows_are_locked_while_saving_and_an_interrupted_save_can_be_finished(app, monkeypatch):
    root = app.root
    app.screen.start()
    pump(root, lambda: isinstance(app.screen, sw.ReviewScreen))
    review = app.screen
    review.lock(True)
    row = review.rows[0]
    assert all(e.cget("state") == "readonly" for e in row.entries)
    before = row.item["include"]
    row.toggle_include()
    assert row.item["include"] == before and not review.save_btn.enabled
    review.lock(False)
    assert all(e.cget("state") == "normal" for e in row.entries)

    calls = {"n": 0}

    def flaky(path, archive):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("network drive went away")
        path.unlink()
        return "archive"

    monkeypatch.setattr(core, "recycle_scan", flaky)
    review.finish()
    pump(root, lambda: not app.busy() and app.shown["errors"])
    assert "network drive went away" in str(app.shown["errors"][-1]) and not review.locked
    app.shown["errors"].clear()
    app.show_start()
    pending = core.Batch.unfinished(app.cfg["output_folder"])
    assert len(pending) == 1 and "didn't finish" in pending[0].summary()
    app.screen.cont(pending[0])
    pump(root, lambda: isinstance(app.screen, sw.DoneScreen))
    assert core.Batch.unfinished(app.cfg["output_folder"]) == []
