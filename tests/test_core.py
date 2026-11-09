"""Tests for the Scanning Wizard core. AI services are faked (no paid calls);
images, the batch files and FTP are real. Set WIZARD_FACE_PHOTO to a
one-face photo to test straightening, and WIZARD_NETWORK_TESTS=1 to check
the free film database for real."""
import base64
import csv
import io
import json
import os
import sys
import threading
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wizard_core as core  # noqa: E402

FACE = Path(os.environ.get("WIZARD_FACE_PHOTO", Path(__file__).parent / "data" / "face.jpg"))
NETWORK = os.environ.get("WIZARD_NETWORK_TESTS") == "1"


class MemKeyring:
    def __init__(self):
        self.d = {}

    def get_password(self, s, k):
        return self.d.get((s, k))

    def set_password(self, s, k, v):
        self.d[(s, k)] = v

    def delete_password(self, s, k):
        self.d.pop((s, k), None)


def photo(path, size=(1200, 1500), mode="RGB", fmt="JPEG", **save):
    im = Image.new("RGB", size, (90, 80, 70))
    d = ImageDraw.Draw(im)
    d.rectangle([size[0] // 4, size[1] // 4, size[0] // 2, size[1] // 2], fill=(230, 220, 200))
    if mode != "RGB":
        im = im.convert(mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, fmt, **save)
    return path


# ─── Settings ─────────────────────────────────────────────────────────────────

def test_old_settings_are_upgraded_and_keys_move_out_of_the_file(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "input_folder": "C:/scans", "ftp_host": "images.moviestore.com", "ftp_user": "bob", "ftp_pass": "pw",
        "gemini_api_key": "g", "gemini_model": "gemini-2.5-flash", "openai_model": "gpt-4o",
        "anthropic_model": "claude-sonnet-4-5-20250929", "ps_action_set": "Retoucher",
        "deepseek_api_key": "d", "low_res_longest_side": "900"}))
    store = core.SecretStore(MemKeyring())
    cfg = core.load_config(store, p)
    assert (cfg["gemini_model"], cfg["openai_model"], cfg["anthropic_model"]) == (
        "gemini-3.8-flash", "gpt-6.1-terra", "claude-sonnet-5-5")
    assert cfg["picture_url_template"] == "https://images.moviestore.com/{batch}/{file}"
    assert cfg["ftp_pass"] == "pw" and cfg["gemini_api_key"] == "g" and cfg["input_folder"] == "C:/scans"
    assert cfg["low_res_longest_side"] == 900
    saved = json.loads(p.read_text())
    for gone in ("ftp_pass", "gemini_api_key", "ps_action_set", "deepseek_api_key"):
        assert gone not in saved
    assert store.get("ftp_pass") == "pw"
    # and it loads the same next time
    assert core.load_config(store, p)["gemini_api_key"] == "g"


def test_keys_stay_in_the_file_when_there_is_no_keyring(tmp_path):
    p = tmp_path / "cfg.json"
    store = core.SecretStore(backend=False)
    assert not store.secure
    cfg = core.load_config(store, p)
    cfg["openai_api_key"] = "sk-x"
    core.save_config(cfg, store, p)
    assert json.loads(p.read_text())["openai_api_key"] == "sk-x"
    assert core.load_config(store, p)["openai_api_key"] == "sk-x"


def test_current_models_are_kept(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"anthropic_model": "claude-haiku-5-5", "openai_model": "gpt-6-luna",
                             "gemini_model": "gemini-3.5-flash-lite"}))
    cfg = core.load_config(core.SecretStore(backend=False), p)
    assert (cfg["anthropic_model"], cfg["openai_model"], cfg["gemini_model"]) == (
        "claude-haiku-5-5", "gpt-6-luna", "gemini-3.5-flash-lite")


# ─── Titles ───────────────────────────────────────────────────────────────────

SUFFIX = "Vintage 8x10 Inch Photo"


@pytest.mark.parametrize("movie, year, actors, scene, expected", [
    ("Iron Man", "2008", ["Robert Downey Jr."], "lab workshop",
     "Iron Man 2008 Robert Downey Jr Lab Workshop Vintage 8x10 Inch Photo"),
    ("Pretty Woman", "1990", "Julia Roberts", "", "Pretty Woman 1990 Julia Roberts Vintage 8x10 Inch Photo"),
    ("Boy Meets World", "1993", ["Ben Savage"], "", "Boy Meets World 1993 Ben Savage Vintage 8x10 Inch Photo"),
    ("E.T. the Extra-Terrestrial", "1982", ["Henry Thomas"], "",
     "ET The Extra Terrestrial 1982 Henry Thomas Vintage 8x10 Inch Photo"),
    ("McCabe & Mrs. Miller", "1971", "Warren Beatty", "", "McCabe & Mrs Miller 1971 Warren Beatty Vintage 8x10 Inch Photo"),
    ("Dr. No", "1962", "Sean Connery and Ursula Andress", "",
     "Dr No 1962 Sean Connery Ursula Andress Vintage 8x10 Inch Photo"),
    ("CASABLANCA", "1942", ["Humphrey Bogart"], "blonde woman at airport",
     "Casablanca 1942 Humphrey Bogart At Airport Vintage 8x10 Inch Photo"),
    ("UNKNOWN", "UNKNOWN", [], "", SUFFIX),
    ("M*A*S*H", "1972-1983", ["Alan Alda"], "", "M*A*S*H Alan Alda Vintage 8x10 Inch Photo"),
])
def test_titles(movie, year, actors, scene, expected):
    assert core.build_title(SUFFIX, movie, year, actors, scene) == expected


def test_long_titles_drop_year_then_scene_then_actors():
    t = core.build_title(SUFFIX, "Ferris Bueller's Day Off", "1986", ["Matthew Broderick"], "parade float")
    assert t == "Ferris Buellers Day Off Matthew Broderick Parade Float Vintage 8x10 Inch Photo"
    many = ["Burt Lancaster", "Kirk Douglas", "Rhonda Fleming", "Jo Van Fleet"]
    t = core.build_title(SUFFIX, "Gunfight at the O.K. Corral", "1957", many, "saloon")
    assert len(t) <= 80 and t.startswith("Gunfight At The OK Corral Burt Lancaster Kirk Douglas")
    assert core.build_title(SUFFIX, "X" * 100, "", [], "").endswith("...")


def test_years_and_names():
    assert core.clean_year("circa 1964") == "1964"
    assert core.clean_year("1997-2001") == "" and core.clean_year("19972001") == "" and core.clean_year("1750") == ""
    assert core.split_names("Tom Cruise & Dustin Hoffman, tom cruise") == ["Tom Cruise", "Dustin Hoffman"]
    assert core.norm_key("The Pénélope") == core.norm_key("penelope")


# ─── Images ───────────────────────────────────────────────────────────────────

def test_plain_jpeg_is_copied_exactly(tmp_path):
    src = photo(tmp_path / "scan.jpg", dpi=(600, 600))
    dst = tmp_path / "out" / "SM1.jpg"
    assert core.save_high_res(src, dst, "JPEG", 0) == "copied"
    assert dst.read_bytes() == src.read_bytes()


def test_jpeg_with_rotation_tag_is_baked_in(tmp_path):
    src = tmp_path / "scan.jpg"
    im = Image.new("RGB", (1200, 800), (50, 60, 70))
    exif = im.getexif()
    exif[0x0112] = 6
    im.save(src, "JPEG", exif=exif.tobytes(), dpi=(400, 400))
    dst = tmp_path / "SM1.jpg"
    core.save_high_res(src, dst, "JPEG", 0)
    with Image.open(dst) as out:
        assert out.size == (800, 1200)
        assert out.getexif().get(0x0112) == 1
        assert round(out.info["dpi"][0]) == 400


@pytest.mark.parametrize("mode, fmt, ext", [("RGBA", "PNG", ".png"), ("I;16", "TIFF", ".tif"),
                                            ("CMYK", "JPEG", ".jpg"), ("1", "BMP", ".bmp")])
def test_other_scan_types_become_real_jpegs(tmp_path, mode, fmt, ext):
    src = tmp_path / f"scan{ext}"
    if mode == "I;16":
        Image.new("I;16", (600, 900), 40000).save(src, fmt)
    else:
        photo(src, (600, 900), mode=mode, fmt=fmt)
    with Image.open(src) as im:
        real = im.format
    dst = tmp_path / "SM1.jpg"
    core.save_high_res(src, dst, real, 180)
    low = tmp_path / "low.jpg"
    core.save_low_res(dst, low, 300)
    with Image.open(dst) as out:
        assert out.format == "JPEG" and out.mode in ("RGB", "L") and out.size == (600, 900)
    with Image.open(low) as small:
        assert max(small.size) == 300
    if mode == "I;16":
        with Image.open(dst) as out:
            assert 140 < out.getpixel((10, 10)) < 170     # 40000/65535 of white, not clipped


def test_turning_a_picture_clockwise(tmp_path):
    p = photo(tmp_path / "a.jpg", (400, 200))
    with Image.open(p) as im:
        im.putpixel((0, 0), (255, 0, 0))
    core.turn_file(p, 270)
    with Image.open(p) as im:
        assert im.size == (200, 400)


@pytest.mark.skipif(not FACE.is_file(), reason="no face photo (set WIZARD_FACE_PHOTO)")
@pytest.mark.parametrize("applied", [0, 90, 180, 270])
def test_straightening_finds_which_way_up(tmp_path, applied):
    finder = core.FaceFinder()
    if finder.kind != "yunet":
        pytest.skip("OpenCV YuNet not available")
    with Image.open(FACE) as im:
        upright = im.convert("RGB")
    scan = upright.transpose(core.TURNS[applied]) if applied else upright
    scan = scan.resize((scan.width * 3, scan.height * 3))
    path = tmp_path / "scan.jpg"
    scan.save(path, "JPEG")
    assert finder.best_turn(path) == (360 - applied) % 360


def test_straightening_leaves_faceless_photos_alone(tmp_path):
    finder = core.FaceFinder()
    if not finder.kind:
        pytest.skip("OpenCV not available")
    assert finder.best_turn(photo(tmp_path / "x.jpg")) == 0


# ─── AI writers (fake servers) ────────────────────────────────────────────────

ANSWER = {"movie_title": "Rain Man", "year": "1988", "actors": ["Dustin Hoffman", "Tom Cruise"],
          "scene_description": "casino floor", "confidence": "high", "clues": "faces and set"}


class FakeResp:
    def __init__(self, status, data=None, headers=None):
        self.status_code, self._data = status, data
        self.headers = headers or {}
        self.ok = status < 400
        self.text = json.dumps(data) if data is not None else ""

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.posts = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append((url, json, headers))
        return self.responses.pop(0)

    def get(self, url, params=None, timeout=None, **kw):
        self.posts.append((url, params, None))
        return self.responses.pop(0)


B64 = base64.b64encode(b"jpegbytes").decode()


def test_gemini_request_and_reply():
    reply = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
        {"text": "thinking it over", "thought": True}, {"text": json.dumps(ANSWER)}]}}]}
    s = FakeSession(FakeResp(200, reply))
    w = core.GeminiWriter({"gemini_api_key": "g", "gemini_model": "gemini-3.8-flash"}, s)
    assert w.ask(B64, "prompt") == ANSWER
    url, body, headers = s.posts[0]
    assert url.endswith("/models/gemini-3.8-flash:generateContent") and headers == {"x-goog-api-key": "g"}
    gen = body["generationConfig"]
    assert gen["responseJsonSchema"] == core.LISTING_SCHEMA and gen["thinkingConfig"] == {"thinkingLevel": "low"}
    assert "temperature" not in gen and body["contents"][0]["parts"][0]["inline_data"]["data"] == B64


def test_gemini_steps_down_when_a_setting_is_refused():
    s = FakeSession(FakeResp(400, {"error": {"message": "Unknown name \"thinkingLevel\""}}),
                    FakeResp(200, {"candidates": [{"content": {"parts": [{"text": json.dumps(ANSWER)}]}}]}))
    w = core.GeminiWriter({"gemini_api_key": "g"}, s)
    assert w.ask(B64, "p") == ANSWER
    assert "thinkingConfig" not in s.posts[1][1]["generationConfig"]


def test_gemini_bad_key_is_fatal():
    s = FakeSession(FakeResp(400, {"error": {"message": "API key not valid. Please pass a valid API key."}}))
    with pytest.raises(core.ProviderError) as e:
        core.GeminiWriter({"gemini_api_key": "bad"}, s).ask(B64, "p")
    assert e.value.fatal


def test_gpt_request_reply_and_refusal():
    reply = {"status": "completed", "output": [{"type": "reasoning", "summary": []}, {
        "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": json.dumps(ANSWER)}]}]}
    refusal = {"status": "completed", "output": [{"type": "message", "content": [
        {"type": "refusal", "refusal": "I can't help with that."}]}]}
    s = FakeSession(FakeResp(200, reply), FakeResp(200, refusal))
    w = core.OpenAIWriter({"openai_api_key": "k", "openai_model": "gpt-6.1-terra"}, s)
    assert w.ask(B64, "prompt") == ANSWER
    url, body, headers = s.posts[0]
    assert url == "https://api.openai.com/v1/responses" and headers["Authorization"] == "Bearer k"
    assert body["text"]["format"]["type"] == "json_schema" and body["text"]["format"]["strict"] is True
    assert body["reasoning"] == {"effort": "low"} and "temperature" not in body
    image = body["input"][0]["content"][1]
    assert image["type"] == "input_image" and image["image_url"] == f"data:image/jpeg;base64,{B64}"
    assert w.ask(B64, "prompt") is None


def claude_with_mock(answer_text, stop_reason="end_turn", model="claude-sonnet-5-5"):
    anthropic = pytest.importorskip("anthropic")
    import httpx2
    seen = []

    def handler(request):
        seen.append(request)
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": answer_text}] if answer_text else [],
            "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 1200, "output_tokens": 80}})

    client = anthropic.Anthropic(api_key="k", http_client=anthropic.DefaultHttpxClient(
        transport=httpx2.MockTransport(handler)))
    w = core.ClaudeWriter({"anthropic_api_key": "k", "anthropic_model": model}, client=client)
    return w, seen


def test_claude_request_through_the_real_sdk():
    w, seen = claude_with_mock(json.dumps(ANSWER))
    assert w.ask(B64, "prompt") == ANSWER
    req = seen[0]
    body = json.loads(req.content)
    assert req.url.path == "/v1/messages" and "server-side-fallback-2026-07-01" in req.headers["anthropic-beta"]
    assert body["model"] == "claude-sonnet-5-5" and body["fallbacks"] == "default"
    assert body["output_config"] == {"format": {"type": "json_schema", "schema": core.LISTING_SCHEMA},
                                     "effort": "low"}
    assert "temperature" not in body and "thinking" not in body
    assert body["messages"][0]["content"][0]["source"] == {"type": "base64", "media_type": "image/jpeg", "data": B64}


def test_claude_haiku_has_no_fallback_and_refusals_return_none():
    w, seen = claude_with_mock("", stop_reason="refusal", model="claude-haiku-5-5")
    assert w.ask(B64, "prompt") is None
    body = json.loads(seen[0].content)
    assert "fallbacks" not in body and "anthropic-beta" not in seen[0].headers


# ─── Finding the film ─────────────────────────────────────────────────────────

class FakeWriter:
    def __init__(self, label, *answers, error=None):
        self.name = self.label = label
        self.answers = list(answers)
        self.error = error
        self.prompts = []

    def ask(self, b64, prompt):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return self.answers.pop(0) if self.answers else None


def test_film_check_picks_the_film_with_these_actors():
    class Checker(core.FilmChecker):
        def _lookup(self, title):
            return [{"id": "Q1", "title": "Rain Man", "first_year": 1999, "last_year": None,
                     "cast": ["Faramarz Gharibian"], "fame": 3},
                    {**RAIN_MAN, "fame": 60}]
    c = Checker()
    assert c.check("Rain Man", "", ["Tom Cruise"])["first_year"] == 1988
    assert c.check("Rain Man", "1999", [])["first_year"] == 1999       # year decides when no actors match
    assert c.check("Rain Man", "", [])["first_year"] == 1988           # else the better-known film


class FakeFilm:
    def __init__(self, result):
        self.result = result

    def check(self, title, year="", actors=()):
        if not self.result:
            return None
        names = {core.norm_key(n) for n in self.result["cast"]}
        return {**self.result, "matched": [a for a in core.split_names(actors) if core.norm_key(a) in names]}


class FakeRek:
    def __init__(self, faces):
        self.faces = faces

    def recognize_celebrities(self, Image):
        return {"CelebrityFaces": [{"Name": n, "MatchConfidence": c} for n, c in self.faces]}


def answer(movie, actors, year="", scene="", confidence="medium"):
    return {"movie_title": movie, "year": year, "actors": actors, "scene_description": scene,
            "confidence": confidence, "clues": "test"}


RAIN_MAN = {"id": "Q1", "title": "Rain Man", "first_year": 1988, "last_year": None,
            "cast": ["Dustin Hoffman", "Tom Cruise", "Valeria Golino"]}


@pytest.fixture
def pic(tmp_path):
    return photo(tmp_path / "SM_0001.jpg", (500, 600))


def test_everything_agrees_in_one_ai_call(pic, monkeypatch):
    monkeypatch.setattr(core, "google_vision_web_detect", lambda key, b64, session=None: {
        "entities": [{"name": "Tom Cruise", "score": 0.9}],
        "pages": [{"title": "Rain Man (1988) - IMDb", "url": "https://www.imdb.com/title/tt0095953/"}]})
    w1, w2 = FakeWriter("Gemini", answer("Rain Man", ["Dustin Hoffman", "Tom Cruise"], "1988", "casino floor")), FakeWriter("GPT")
    cfg = {"google_vision_api_key": "v", "aws_access_key_id": "a", "aws_secret_access_key": "s",
           "identify_mode": "thorough"}
    ident = core.Identifier(cfg, writers=[w1, w2], film=FakeFilm(RAIN_MAN),
                            rekognition_client=FakeRek([("Tom Cruise", 99.1), ("Dustin Hoffman", 97.5)]))
    r = ident.identify(pic)
    assert (r["movie"], r["year"], r["scene"], r["confidence"]) == ("Rain Man", "1988", "casino floor", "high")
    assert r["actors"] == ["Tom Cruise", "Dustin Hoffman"] and not r["warnings"]
    assert len(w1.prompts) == 1 and not w2.prompts
    assert "Face match: Tom Cruise (99%)" in w1.prompts[0] and "film website" in w1.prompts[0]
    assert r["sources"] == ["Google", "Amazon", "Gemini", "Film check"]


def test_two_ais_must_agree_when_thorough(pic):
    a = answer("Vertigo", ["James Stewart"], "1958")
    w1, w2 = FakeWriter("Gemini", dict(a)), FakeWriter("GPT", dict(a))
    r = core.Identifier({"identify_mode": "thorough"}, writers=[w1, w2], film=False).identify(pic)
    assert r["confidence"] == "high" and len(w1.prompts) == 1 and len(w2.prompts) == 1
    assert "Gemini: Vertigo (1958) with James Stewart" in w2.prompts[0]


def test_fast_mode_stops_at_the_first_full_answer(pic):
    w1, w2 = FakeWriter("Gemini", answer("Vertigo", ["James Stewart"])), FakeWriter("GPT")
    r = core.Identifier({"identify_mode": "fast"}, writers=[w1, w2], film=False).identify(pic)
    assert r["movie"] == "Vertigo" and not w2.prompts and r["confidence"] == "medium"


def test_face_match_beats_a_wrong_ai_guess(pic):
    w = FakeWriter("Gemini", answer("Top Gun", ["Matt Damon"]), answer("Top Gun", ["Matt Damon"]))
    r = core.Identifier({"aws_access_key_id": "a", "aws_secret_access_key": "s", "identify_mode": "fast"},
                        writers=[w], film=False, rekognition_client=FakeRek([("Tom Cruise", 99.0)])).identify(pic)
    assert r["actors"] == ["Tom Cruise"] and any("Matt Damon" in x for x in r["warnings"])


def test_film_database_fixes_the_year(pic):
    w = FakeWriter("Gemini", answer("Rain Man", ["Tom Cruise"], "1995"))
    r = core.Identifier({"identify_mode": "fast"}, writers=[w], film=FakeFilm(RAIN_MAN)).identify(pic)
    assert r["year"] == "1988" and r["confidence"] == "high"
    assert any("1995 to 1988" in x for x in r["warnings"])


def test_a_busy_film_database_is_not_reported_as_film_not_found(pic):
    class Busy(core.FilmChecker):
        def _lookup(self, title):
            raise RuntimeError("429 Too Many Requests")
    w = FakeWriter("Gemini", answer("Vertigo", ["James Stewart"]))
    r = core.Identifier({"identify_mode": "fast"}, writers=[w], film=Busy()).identify(pic)
    assert "The film database didn't answer, so the film wasn't checked" in r["warnings"]
    assert core.http("a@b.example").headers["User-Agent"].endswith("(photo listing tool; a@b.example)")


def test_a_bad_key_is_skipped_and_the_next_ai_used(pic):
    logs = []
    bad = FakeWriter("Claude", error=core.ProviderError("Claude key problem", fatal=True))
    good = FakeWriter("GPT", answer("Vertigo", ["James Stewart"]), answer("Vertigo", ["James Stewart"]))
    ident = core.Identifier({"identify_mode": "thorough"}, log_fn=lambda t, level="info": logs.append(t),
                            writers=[bad, good], film=False)
    r = ident.identify(pic)
    assert r["movie"] == "Vertigo" and len(bad.prompts) == 1 and "Claude" in ident.disabled
    assert any("skipping Claude" in t for t in logs)


def test_declined_and_empty_answers(pic):
    w = FakeWriter("Claude", None, answer("", []))
    r = core.Identifier({"identify_mode": "fast"}, writers=[w], film=False).identify(pic)
    assert r["confidence"] == "low" and "No film or show found" in r["warnings"]


@pytest.mark.skipif(not NETWORK, reason="set WIZARD_NETWORK_TESTS=1")
def test_real_film_database():
    film = core.FilmChecker()
    r = film.check("Rain Man", "1988", ["Dustin Hoffman", "Tom Cruise", "Matt Damon"])
    assert r["title"] == "Rain Man" and r["first_year"] == 1988
    assert r["matched"] == ["Dustin Hoffman", "Tom Cruise"]
    tv = film.check("Boy Meets World", "1995", ["Ben Savage"])
    assert tv["first_year"] == 1993 and tv["matched"] == ["Ben Savage"]
    assert film.check("Qwzxv Nonexistent Picture 1234") is None


def test_vision_reading():
    p = core.parse_vision({"entities": [{"name": "Black and white", "score": 0.9}, {"name": "1966", "score": 0.5},
                                        {"name": "Sophia Loren", "score": 0.8}],
                           "pages": [{"title": "Arabesque (1966) - Photo Gallery - IMDb", "url": "https://m.imdb.com/x"},
                                     {"title": "Sophia Loren vintage photo | eBay", "url": "https://ebay.com/itm/1"}]})
    assert (p["movie"], p["year"], p["actor"]) == ("Arabesque", "1966", "Sophia Loren")


# ─── Links, FTP, CSV ──────────────────────────────────────────────────────────

def test_link_checks():
    assert not core.check_link("http://images.example.com/a.jpg")[0]
    s = FakeSession(FakeResp(200, None, {"Content-Type": "image/jpeg"}))
    s.head = lambda url, timeout=None, allow_redirects=None: s.responses.pop(0)
    assert core.check_link("https://images.example.com/a.jpg", s)[0]
    s.responses.append(FakeResp(404, None, {}))
    ok, msg = core.check_link("https://images.example.com/a.jpg", s)
    assert not ok and "404" in msg


@pytest.fixture
def ftp_server(tmp_path):
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import ThreadedFTPServer
    root = tmp_path / "ftproot"
    root.mkdir()
    auth = DummyAuthorizer()
    auth.add_user("bob", "pw", str(root), perm="elradfmwMT")
    handler = type("Handler", (FTPHandler,), {"authorizer": auth})
    server = ThreadedFTPServer(("127.0.0.1", 0), handler)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            server.serve_forever(timeout=0.1, blocking=False)
        server.close_all()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    yield root, server.socket.getsockname()[1]
    stop.set()
    t.join(timeout=5)


def test_upload_falls_back_when_the_server_has_no_encryption(tmp_path, ftp_server):
    root, port = ftp_server
    cfg = {"ftp_host": f"127.0.0.1:{port}", "ftp_user": "bob", "ftp_pass": "pw", "ftp_remote_dir": "/",
           "ftp_security": "auto", "output_folder": str(tmp_path / "out")}
    batch = core.Batch.open_or_create(tmp_path / "out", "SM")
    items = []
    for n in (1, 2):
        item = {"number": n, "source": f"s{n}.jpg", "source_folder": str(tmp_path), "source_size": 1}
        photo(batch.low_path(item), (80, 100))
        items.append(item)
    batch.items.extend(items)
    logs = []
    assert core.upload_pending(cfg, batch, items, lambda t, level="info": logs.append((level, t))) == 2
    assert sorted(p.name for p in (root / batch.name).iterdir()) == [f"{batch.name}_0001.jpg", f"{batch.name}_0002.jpg"]
    assert all(i["uploaded"] for i in items) and logs and logs[0][0] == "warn"
    assert core.upload_pending(cfg, batch, items, lambda *a, **k: None) == 0     # nothing twice
    with pytest.raises(core.StageError):
        core.ftp_open({**cfg, "ftp_security": "tls"})
    with pytest.raises(core.StageError, match="log in"):
        core.ftp_open({**cfg, "ftp_pass": "wrong"})


def test_csv_rows_are_never_added_twice(tmp_path):
    p = tmp_path / "SM1.csv"
    row = {"SKU": "SM1/SM1_0001", "eBay Title": "T", "Title": "T", "PicURL": "https://x/y.jpg", "Description": "D"}
    assert core.append_csv(p, [row]) == 1
    assert core.append_csv(p, [row, {**row, "SKU": "SM1/SM1_0002"}]) == 1
    with open(p, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [r["SKU"] for r in rows] == ["SM1/SM1_0001", "SM1/SM1_0002"]
    assert core.find_last_number_in_csv(p, "SM1") == 2


# ─── The whole run ────────────────────────────────────────────────────────────

class FakeIdentifier:
    def __init__(self):
        self.seen = []

    def has_any_service(self):
        return True

    def identify(self, path, url=None):
        self.seen.append(Path(path).name)
        return {"movie": "Rain Man", "year": "1988", "actors": ["Tom Cruise"], "scene": "casino",
                "confidence": "high", "sources": ["Gemini"], "warnings": [], "clues": ""}


def run_cfg(tmp_path, **extra):
    cfg = core.default_config()
    cfg.update(input_folder=str(tmp_path / "scans"), output_folder=str(tmp_path / "finished"),
               upload_mode="manual", film_check=False, gemini_api_key="g",
               picture_url_template="https://images.example.com/{batch}/{file}")
    cfg.update(extra)
    return cfg


def run(cfg, batch, scans, identifier, stop_at=None):
    events = []
    stop = threading.Event()

    def emit(kind, **data):
        events.append((kind, data))
        if kind == "stage" and data["key"] == stop_at:
            stop.set()

    core.Pipeline(cfg, batch, emit, stop, scans=scans, identifier=identifier).run()
    return events


def test_one_and_done_run_then_finish(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "check_link", lambda url, session=None: (True, "Picture links work."))
    archived = []
    monkeypatch.setattr(core, "recycle_scan", lambda path, archive: archived.append(path.name) or path.unlink() or "archive")
    cfg = run_cfg(tmp_path)
    scans = Path(cfg["input_folder"])
    photo(scans / "scan 2.jpg")
    photo(scans / "scan 10.png", mode="RGBA", fmt="PNG")
    (scans / "scan 3.jpg").write_bytes(b"not a picture")
    if FACE.is_file():
        with Image.open(FACE) as im:
            im.convert("RGB").resize((819, 960)).transpose(Image.Transpose.ROTATE_180).save(scans / "scan 1.jpg")

    fresh, done = core.new_scans(cfg)
    assert done == 0 and [p.name for p in fresh][:2] == (["scan 1.jpg", "scan 2.jpg"] if FACE.is_file() else ["scan 2.jpg", "scan 3.jpg"])
    batch = core.Batch.open_or_create(cfg["output_folder"], "SM")
    ident = FakeIdentifier()
    events = run(cfg, batch, fresh, ident)
    assert events[-1][0] == "finished", events[-3:]
    items = {i["source"]: i for i in batch.items}
    assert items["scan 3.jpg"]["error"].startswith("Can't open")
    ok = [i for i in batch.items if not i.get("error")]
    for i in ok:
        assert batch.high_path(i).is_file() and batch.low_path(i).is_file() and i["identified"]
        with Image.open(batch.low_path(i)) as low:
            assert max(low.size) <= 800
        assert i["url"] == f"https://images.example.com/{batch.name}/{batch.sku(i)}.jpg"
        assert i["title"] == "Rain Man 1988 Tom Cruise Casino Vintage 8x10 Inch Photo"
    if FACE.is_file():
        assert items["scan 1.jpg"]["turn"] == 180 and items["scan 1.jpg"]["number"] == 1
    assert items["scan 10.png"]["format"] == "PNG"
    assert core.new_scans(cfg) == ([], len(batch.items))   # nothing is added twice
    assert batch.state_path.is_file()

    result = core.finish_batch(cfg, batch, lambda *a, **k: None)
    assert result == {"added": len(ok), "left_out": 0, "cleared": len(ok), "broken": ["scan 3.jpg"],
                      "kept": [], "missing": []}
    assert items["scan 3.jpg"]["number"] is None         # a broken file doesn't use up a number
    assert sorted(i["number"] for i in ok) == list(range(1, len(ok) + 1))
    assert core.broken_scans(cfg) == ["scan 3.jpg"]
    with open(batch.csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [r["SKU"] for r in rows] == [f"{batch.name}/{batch.sku(i)}" for i in ok]
    assert rows[0]["Description"] == core.DESC_SM and rows[0]["PicURL"].startswith("https://")
    assert sorted(archived) == sorted(i["source"] for i in ok)
    assert core.Batch.unfinished(cfg["output_folder"]) == []

    # A second batch the same day carries on numbering after the CSV
    photo(scans / "later.jpg")
    batch2 = core.Batch.open_or_create(cfg["output_folder"], "SM")
    run(cfg, batch2, core.new_scans(cfg)[0], FakeIdentifier())
    newest = [i for i in batch2.items if i["source"] == "later.jpg"][0]
    assert newest["number"] == max(i["number"] for i in batch.items if i["number"]) + 1


def test_a_stopped_run_picks_up_where_it_left_off(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "check_link", lambda url, session=None: (True, "ok"))
    cfg = run_cfg(tmp_path)
    for n in range(3):
        photo(Path(cfg["input_folder"]) / f"s{n}.jpg", (300, 400))
    batch = core.Batch.open_or_create(cfg["output_folder"], "SO")
    events = run(cfg, batch, core.new_scans(cfg)[0], FakeIdentifier(), stop_at="low")
    assert events[-1][0] == "stopped"
    assert all(i.get("high_done") and not i.get("low_done") for i in batch.items)
    again = core.Batch.load(batch.state_path)
    ident = FakeIdentifier()
    events = run(cfg, again, [], ident)
    assert events[-1][0] == "finished" and len(again.items) == 3 and len(ident.seen) == 3
    assert all(i["title"].endswith("Original 8x10 Inch Photo") for i in again.items)


def test_a_batch_of_only_broken_files_is_not_left_hanging(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(core, "check_link", lambda url, session=None: called.append(url) or (True, "ok"))
    cfg = run_cfg(tmp_path)
    (Path(cfg["input_folder"])).mkdir(parents=True)
    (Path(cfg["input_folder"]) / "x.jpg").write_bytes(b"nope")
    batch = core.Batch.open_or_create(cfg["output_folder"], "SM")
    events = run(cfg, batch, core.new_scans(cfg)[0], FakeIdentifier())
    assert events[-1][0] == "error" and "None of the new files could be opened" in events[-1][1]["message"]
    assert "x.jpg" in events[-1][1]["message"] and batch.items[0]["error"]
    assert core.Batch.unfinished(cfg["output_folder"]) == [] and core.broken_scans(cfg) == ["x.jpg"]
    assert not called          # "I upload them myself": no link check before anything is online


def test_upload_through_the_whole_run(tmp_path, monkeypatch, ftp_server):
    root, port = ftp_server
    monkeypatch.setattr(core, "check_link", lambda url, session=None: (True, "ok"))
    cfg = run_cfg(tmp_path, upload_mode="ftp", ftp_host=f"127.0.0.1:{port}", ftp_user="bob", ftp_pass="pw",
                  ftp_remote_dir="/pictures")
    photo(Path(cfg["input_folder"]) / "a.jpg", (300, 400))
    batch = core.Batch.open_or_create(cfg["output_folder"], "MM")
    events = run(cfg, batch, core.new_scans(cfg)[0], FakeIdentifier())
    assert events[-1][0] == "finished"
    assert (root / "pictures" / batch.name / f"{batch.name}_0001.jpg").is_file()
    assert batch.items[0]["uploaded"] and batch.data["link_check"]["ok"]
    assert batch.items[0]["title"].endswith("Vintage Photo")


def test_upload_not_set_up_stops_with_a_clear_message(tmp_path, monkeypatch):
    cfg = run_cfg(tmp_path, upload_mode="ftp", ftp_user="")
    photo(Path(cfg["input_folder"]) / "a.jpg", (300, 400))
    batch = core.Batch.open_or_create(cfg["output_folder"], "SM")
    events = run(cfg, batch, core.new_scans(cfg)[0], FakeIdentifier())
    assert events[-1][0] == "error" and "FTP username" in events[-1][1]["message"]
    assert batch.items[0]["low_done"]          # the work before the upload is kept


def test_cost_guide():
    cfg = core.default_config()
    assert core.cost_per_photo(cfg) == (0.0, 0.0)
    cfg.update(gemini_api_key="g", anthropic_api_key="c", google_vision_api_key="v")
    low, high = core.cost_per_photo(cfg)
    assert 0.005 < low < high < 0.05


# ─── Found by the independent review ──────────────────────────────────────────

def finished_batch(tmp_path, monkeypatch, n=2):
    monkeypatch.setattr(core, "check_link", lambda url, session=None: (True, "ok"))
    cfg = run_cfg(tmp_path)
    for k in range(n):
        photo(Path(cfg["input_folder"]) / f"s{k}.jpg", (300, 400))
    batch = core.Batch.open_or_create(cfg["output_folder"], "SM")
    run(cfg, batch, core.new_scans(cfg)[0], FakeIdentifier())
    return cfg, batch


def test_a_scan_replaced_after_processing_is_never_cleared(tmp_path, monkeypatch):
    cleared = []
    monkeypatch.setattr(core, "recycle_scan", lambda path, archive: cleared.append(path.name) or "archive")
    cfg, batch = finished_batch(tmp_path, monkeypatch)
    photo(Path(cfg["input_folder"]) / "s1.jpg", (999, 999))       # a different photo, same name
    result = core.finish_batch(cfg, batch, lambda *a, **k: None)
    assert cleared == ["s0.jpg"] and result["kept"] == ["s1.jpg"]


def test_missing_pictures_stay_open_and_continue_remakes_them(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "recycle_scan", lambda path, archive: path.unlink() or "archive")
    cfg, batch = finished_batch(tmp_path, monkeypatch)
    lost = batch.items[1]
    batch.high_path(lost).unlink()
    result = core.finish_batch(cfg, batch, lambda *a, **k: None)
    assert result["added"] == 1 and result["missing"] == [batch.sku(lost)]
    assert not lost.get("saved") and not batch.data["finished"]
    assert core.Batch.unfinished(cfg["output_folder"])[0].name == batch.name
    again = core.Batch.load(batch.state_path)
    run(cfg, again, [], FakeIdentifier())                 # Continue
    assert again.high_path(again.items[1]).is_file()
    result = core.finish_batch(cfg, again, lambda *a, **k: None)
    assert result["added"] == 1 and again.data["finished"]
    with open(again.csv_path, encoding="utf-8") as f:
        assert len(list(csv.DictReader(f))) == 2


def test_an_interrupted_save_shows_as_not_finished_and_completes(tmp_path, monkeypatch):
    cfg, batch = finished_batch(tmp_path, monkeypatch)
    boom = {"n": 0}

    def flaky(path, archive):
        boom["n"] += 1
        if boom["n"] == 2:
            raise OSError("disk went away")
        path.unlink()
        return "archive"

    monkeypatch.setattr(core, "recycle_scan", flaky)
    with pytest.raises(OSError):
        core.finish_batch(cfg, batch, lambda *a, **k: None)
    pending = core.Batch.unfinished(cfg["output_folder"])
    assert [b.name for b in pending] == [batch.name] and "didn't finish" in pending[0].summary()
    result = core.finish_batch(cfg, pending[0], lambda *a, **k: None)
    assert result["added"] == 0 and result["cleared"] == 1 and pending[0].data["finished"]
    assert list(Path(cfg["input_folder"]).iterdir()) == []


def test_a_broken_credential_store_never_stops_the_app(tmp_path):
    class Broken(MemKeyring):
        def set_password(self, s, k, v):
            raise RuntimeError("value too long")
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"ftp_pass": "pw", "gemini_api_key": "g"}))
    store = core.SecretStore(Broken())
    cfg = core.load_config(store, p)
    assert cfg["ftp_pass"] == "pw" and cfg["gemini_api_key"] == "g"
    cfg["openai_api_key"] = "sk"
    core.save_config(cfg, store, p)
    saved = json.loads(p.read_text())
    assert saved["ftp_pass"] == "pw" and saved["openai_api_key"] == "sk"     # kept in the file instead


def test_a_damaged_settings_file_is_backed_up(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text("{not json")
    cfg = core.load_config(core.SecretStore(backend=False), p)
    assert cfg["batch_type"] == "SM" and (tmp_path / "cfg.json.bak").read_text() == "{not json"


def test_csv_from_excel_and_csv_left_open(tmp_path):
    p = tmp_path / "SM1.csv"
    p.write_bytes("SKU,eBay Title,Title,PicURL,Description\r\nSM1/SM1_0001,Am\xe9lie,Am\xe9lie,u,d".encode("cp1252"))
    row = {"SKU": "SM1/SM1_0002", "eBay Title": "T", "Title": "T", "PicURL": "https://x/y.jpg", "Description": "D"}
    assert core.append_csv(p, [row, {**row, "SKU": "SM1/SM1_0001"}]) == 1
    text = p.read_bytes().decode("cp1252")
    assert text.endswith("SM1/SM1_0002,T,T,https://x/y.jpg,D\r\n") and "Am\xe9lie,u,d\r\nSM1/SM1_0002" in text
    p.chmod(0o444)
    try:
        with pytest.raises(core.StageError, match="Close it"):
            core.append_csv(p, [{**row, "SKU": "SM1/SM1_0003"}])
    finally:
        p.chmod(0o644)


@pytest.mark.parametrize("raw, expected", [("JAWS", "Jaws"), ("GIGI", "Gigi"), ("ET", "ET"), ("M*A*S*H", "M*A*S*H"),
                                           ("ET THE EXTRA TERRESTRIAL", "ET The Extra Terrestrial"),
                                           ("RuPaul", "RuPaul")])
def test_capitals(raw, expected):
    assert core.smart_case(raw) == expected


@pytest.mark.parametrize("raw, expected", [("two men", ""), ("boy and girl at dinner", "at dinner"),
                                           ("woman with dog", "dog"), ("couple dancing", "couple dancing"),
                                           ("old man and the sea", "sea")])
def test_scene_leftovers(raw, expected):
    assert core.clean_scene(raw) == expected


def test_title_limit_has_a_floor():
    t = core.build_title(SUFFIX, "Casablanca", "1942", ["Humphrey Bogart"], "", max_chars=5)
    assert len(t) <= 30 and t.startswith("Casablanca")
    assert len(core.build_title(SUFFIX, "Casablanca", "1942", ["Humphrey Bogart"], "", max_chars=0)) == 55  # 0 = default 80


def test_windows_style_ftp_folder(tmp_path, ftp_server):
    root, port = ftp_server
    cfg = {"ftp_host": f"127.0.0.1:{port}", "ftp_user": "bob", "ftp_pass": "pw", "ftp_security": "plain"}
    ftp, _ = core.ftp_open(cfg)
    core.ftp_enter(ftp, "\\photos\\2026", "SM1")
    ftp.quit()
    assert (root / "photos" / "2026" / "SM1").is_dir()
