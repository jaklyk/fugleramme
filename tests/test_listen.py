"""Tap-to-listen: the page description, the clip pass-through, the reference
recordings, and the kiosk's mapping of a bird's box onto the picture on screen."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from fugleramme import listen, modes, xenocanto
from fugleramme.render.collage import compose_collage, render_collage
from fugleramme.render.page import Bird
from fugleramme.render.plate import compose_plate
from fugleramme.settings import Settings, SettingsStore, from_env
from fugleramme.source import Detection
from fugleramme.status import Status
from fugleramme.web import STATIC_DIR, server
from fugleramme.xenocanto import References

SPECIES = ("Turdus merula", "Parus major")


def _fetch(url: str, headers: dict | None = None) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.status, dict(error.headers), error.read()


class _Clips:
    """BirdNET-Go's /audio and /spectrogram, as `listen.Detector` asks them."""

    def __init__(self, answers: dict[str, tuple[int, dict[str, str], bytes]]):
        self.answers = answers
        self.asked: list[tuple[str, dict[str, str] | None]] = []

    def request(self, path, method="GET", headers=None, params=None):
        self.asked.append((path, headers))
        return self.answers.get(path, (404, {"content-type": "application/json"}, b"{}"))


def _serve(tmp_path, source, settings: Settings, detector=None, references=None):
    style = tmp_path / "images" / "classic"
    (style / "birds").mkdir(parents=True, exist_ok=True)
    for key in ("turdus-merula", "parus-major"):
        Image.new("RGBA", (120, 90), (40, 40, 40, 255)).save(style / "birds" / f"{key}.png")
    store = SettingsStore(tmp_path / "s.json", settings)
    handler = server.make_handler(
        source,
        tmp_path / "images",
        store,
        None,
        Status(),
        detector=detector,
        references=references,
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


@pytest.fixture
def listening(tmp_path, source):
    clips = _Clips(
        {
            "/audio/7": (206, {"content-type": "audio/wav", "content-range": "bytes 0-1/4"}, b"RI"),
            "/audio/8": (503, {"content-type": "text/plain", "retry-after": "3"}, b"busy"),
        }
    )
    httpd, base = _serve(
        tmp_path, source(count=40, seed=0), Settings(tap_to_listen=True), detector=clips
    )
    yield base, clips
    httpd.shutdown()


@pytest.fixture
def quiet(tmp_path, source):
    """The same frame with tap-to-listen at its default."""
    httpd, base = _serve(tmp_path, source(count=40, seed=0), Settings(), detector=_Clips({}))
    yield base
    httpd.shutdown()


# -- the page ---------------------------------------------------------------


def test_every_listen_route_is_absent_while_it_is_off(quiet):
    for route in (
        "/page.json",
        "/listen/plate?name=Turdus%20merula",
        "/listen/clip?what=audio&id=7",
        "/listen/reference?name=Turdus%20merula",
    ):
        assert _fetch(quiet + route)[0] == 404, route


def test_the_kiosk_only_loads_the_listener_when_the_page_answers():
    kiosk = (STATIC_DIR / "kiosk.html").read_text()
    assert 'fetch("/page.json", {method: "HEAD"' in kiosk
    assert 'import("/listen.js")' in kiosk


def test_the_page_json_carries_the_state_token_and_the_picture_s_size(listening):
    base, _clips = listening
    page = json.loads(_fetch(base + "/page.json")[2])
    state = json.loads(_fetch(base + "/state")[2])
    picture = Image.open(io.BytesIO(_fetch(base + "/collage.png")[2]))
    assert page["token"] == state["token"]
    assert (page["width"], page["height"]) == picture.size


def test_each_bird_has_a_box_on_the_page_its_names_and_its_latest_call(listening):
    base, _clips = listening
    page = json.loads(_fetch(base + "/page.json")[2])
    assert {bird["scientific"] for bird in page["birds"]} <= set(SPECIES)
    assert page["birds"]
    for bird in page["birds"]:
        x0, y0, x1, y1 = bird["box"]
        assert 0 <= x0 < x1 <= page["width"] and 0 <= y0 < y1 <= page["height"]
        assert bird["common"]
        assert isinstance(bird["detection"]["id"], int)
        assert bird["detection"]["at"]


def test_the_boxes_are_the_ones_the_picture_was_drawn_with(tmp_path):
    art = tmp_path / "bird.png"
    Image.new("RGBA", (200, 150), (40, 40, 40, 255)).save(art)
    entries = [(f"Genus species{n}", art) for n in range(5)]
    drawn = compose_collage(entries, (800, 600), show_names=False, textured=False)
    assert np.array_equal(
        np.asarray(drawn.image),
        np.asarray(render_collage(entries, (800, 600), show_names=False, textured=False)),
    )
    ink = np.asarray(drawn.image.convert("L")) < 100  # the birds are dark, the paper is not
    inside = np.zeros_like(ink)
    for bird in drawn.birds:
        x0, y0, x1, y1 = bird.box
        inside[y0:y1, x0:x1] = True
    assert len(drawn.birds) == 5
    assert ink.any() and not (ink & ~inside).any()


def test_a_plate_boxes_its_one_bird(tmp_path):
    art = tmp_path / "bird.png"
    Image.new("RGBA", (200, 150), (40, 40, 40, 255)).save(art)
    drawn = compose_plate(art, "Turdus merula", "Blackbird", resolution=(800, 600), textured=False)
    (bird,) = drawn.birds
    x0, y0, x1, y1 = bird.box
    ink = np.argwhere(np.asarray(drawn.image.convert("L")) < 100)
    assert bird.name == "Turdus merula"
    # The bird's ink, not the caption's: the caption sits under the box. The paper
    # feathers the cut-out's edge a pixel or two inside it.
    top = ink[ink[:, 0] < y1]
    left, upper = top[:, 1].min(), top[:, 0].min()
    right, lower = top[:, 1].max() + 1, top[:, 0].max() + 1
    assert x0 <= left <= x0 + 3 and y0 <= upper <= y0 + 3
    assert x1 - 3 <= right <= x1 and y1 - 3 <= lower <= y1


def test_describe_falls_back_to_english_and_hides_clip_ids_without_clips():
    class Names:
        def __init__(self, table):
            self.table = table

        def parts(self, scientific, drawable=None):
            return (self.table.get(scientific, scientific),)

    page = modes.Page((), b"", (100, 80), (Bird("Turdus merula", (1, 2, 30, 40)),))
    call = Detection(5, datetime(2026, 1, 1, 9), "Turdus merula", 0.9, None)
    described = listen.describe(
        page,
        "abc",
        {"turdus-merula": call},
        Names({}),
        Names({"Turdus merula": "Blackbird"}),
        False,
    )
    (bird,) = described["birds"]
    assert described["token"] == "abc" and (described["width"], described["height"]) == (100, 80)
    assert bird["common"] == "Blackbird"
    assert bird["box"] == [1, 2, 30, 40]
    assert bird["detection"] == {"id": None, "at": "2026-01-01T09:00:00"}


# -- the clips --------------------------------------------------------------


def test_a_clip_passes_through_with_its_range(listening):
    base, clips = listening
    status, headers, body = _fetch(
        base + "/listen/clip?what=audio&id=7", headers={"Range": "bytes=0-1"}
    )
    assert status == 206 and body == b"RI"
    assert headers["content-range"] == "bytes 0-1/4"
    assert clips.asked[-1] == ("/audio/7", {"Range": "bytes=0-1"})


def test_a_clip_still_being_written_keeps_its_retry_after(listening):
    base, _clips = listening
    status, headers, _body = _fetch(base + "/listen/clip?what=audio&id=8")
    assert status == 503 and headers["retry-after"] == "3"


def test_a_clip_the_detector_no_longer_has_is_a_404(listening):
    base, _clips = listening
    assert _fetch(base + "/listen/clip?what=spectrogram&id=9")[0] == 404


@pytest.mark.parametrize("query", ["what=audio&id=x", "what=audio&id=-1", "what=wav&id=7", "id=7"])
def test_only_a_detection_number_and_a_known_kind_are_asked_for(listening, query):
    base, clips = listening
    assert _fetch(f"{base}/listen/clip?{query}")[0] == 404
    assert not clips.asked


@pytest.mark.parametrize(
    "header, span",
    [
        ("", None),
        ("bytes=0-1", (0, 1)),
        ("bytes=5-", (5, 9)),
        ("bytes=-3", (7, 9)),
        ("bytes=4-400", (4, 9)),
        ("bytes=12-", None),
        ("bytes=0-1,4-5", None),
        ("lines=0-1", None),
        ("bytes=x-1", None),
    ],
)
def test_byte_range(header, span):
    assert listen.byte_range(header, 10) == span


# -- the reference recordings -----------------------------------------------

THAILAND = xenocanto.tiers("Thailand; Malaysia, Myanmar, Laos, Cambodia, Vietnam")


def _recording(number, country="Spain", kind="song", quality="A", **extra):
    return {
        "id": str(number),
        "cnt": country,
        "type": kind,
        "q": quality,
        "rec": f"Recordist {number}",
        "lic": "//creativecommons.org/licenses/by-nc-sa/4.0/",
        "url": f"//xeno-canto.org/{number}",
        "file": f"https://xeno-canto.org/{number}/download",
        "file-name": f"XC{number}-a.mp3",
        "sono": {"large": f"https://xeno-canto.org/sono/{number}.png"},
        "status": "identified",
        **extra,
    }


def test_tiers_read_the_country_preference():
    assert THAILAND == (("thailand",), ("malaysia", "myanmar", "laos", "cambodia", "vietnam"))
    assert xenocanto.tiers("") == ()


def test_a_song_from_thailand_beats_one_from_a_neighbour_and_one_from_anywhere():
    picked = xenocanto.pick(
        [_recording(1, "Spain"), _recording(2, "Laos"), _recording(3, "Thailand")], THAILAND
    )
    assert picked["id"] == "3"


def test_a_neighbour_beats_anywhere():
    assert xenocanto.pick([_recording(1, "India"), _recording(2, "Vietnam")], THAILAND)["id"] == "2"


def test_any_song_beats_a_call_from_thailand():
    picked = xenocanto.pick(
        [_recording(1, "Thailand", kind="call"), _recording(2, "Spain", kind="call, song")],
        THAILAND,
    )
    assert picked["id"] == "2"


def test_a_call_when_there_is_no_song():
    picked = xenocanto.pick(
        [_recording(1, "Spain", kind="call"), _recording(2, "Malaysia", kind="flight call")],
        THAILAND,
    )
    assert picked["id"] == "2"


def test_better_quality_breaks_a_tie_and_unusable_recordings_are_skipped():
    picked = xenocanto.pick(
        [
            _recording(1, "Thailand", quality="C"),
            _recording(2, "Thailand", quality="B"),
            _recording(3, "Thailand", file=""),
            _recording(4, "Thailand", status="questioned"),
        ],
        THAILAND,
    )
    assert picked["id"] == "2"
    assert xenocanto.pick([], THAILAND) is None


def test_the_query_uses_v3_search_tags():
    assert xenocanto.query("Halcyon smyrnensis", "A") == "gen:Halcyon sp:smyrnensis grp:birds q:A"
    assert xenocanto.query("Halcyon smyrnensis") == "gen:Halcyon sp:smyrnensis grp:birds"


@pytest.fixture
def network(monkeypatch):
    """xeno-canto, mocked: what each search answers, and every URL asked for."""
    asked: list[str] = []
    searches: dict[str, list[dict]] = {}

    def get(url: str) -> bytes:
        asked.append(url)
        if url.startswith(xenocanto.API):
            quality = "A" if "q%3AA" in url else ""
            return json.dumps({"recordings": searches.get(quality, [])}).encode()
        return b"audio" if url.endswith("/download") else b"\x89PNG"

    monkeypatch.setattr(xenocanto, "_get", get)
    return asked, searches


def test_one_recording_is_downloaded_with_its_credit_and_kept(tmp_path, network):
    asked, searches = network
    searches["A"] = [_recording(11, "Thailand"), _recording(12, "Spain")]
    references = References(tmp_path / "xc", key="secret", countries="Thailand")

    found = references.get("Halcyon smyrnensis")
    assert found is not None
    assert (found.id, found.recordist, found.country) == ("11", "Recordist 11", "Thailand")
    assert found.licence == "https://creativecommons.org/licenses/by-nc-sa/4.0/"
    assert found.link == "https://xeno-canto.org/11"
    assert (tmp_path / "xc" / found.audio).read_bytes() == b"audio"
    assert (tmp_path / "xc" / found.sonogram).read_bytes() == b"\x89PNG"

    before = len(asked)
    assert references.get("Halcyon smyrnensis") == found
    assert len(asked) == before  # read back from the data folder, not asked again
    assert References(tmp_path / "xc", key="secret").held("Halcyon smyrnensis") == found


def test_any_quality_is_asked_for_only_when_quality_a_has_nothing(tmp_path, network):
    asked, searches = network
    searches[""] = [_recording(21, "Malaysia", quality="C")]
    found = References(tmp_path / "xc", key="secret").get("Phylloscopus fuscatus")
    assert found is not None and found.id == "21"
    assert sum(url.startswith(xenocanto.API) for url in asked) == 2


def test_a_species_with_no_recording_is_not_asked_again_at_once(tmp_path, network):
    asked, _searches = network
    references = References(tmp_path / "xc", key="secret")
    assert references.get("Genus nothing") is None
    before = len(asked)
    assert references.get("Genus nothing") is None
    assert len(asked) == before


def test_without_a_key_nothing_is_asked(tmp_path, network):
    asked, _searches = network
    assert References(tmp_path / "xc", key="").get("Halcyon smyrnensis") is None
    assert not asked


def test_the_reference_route_serves_the_credit_audio_in_ranges_and_sonogram(
    tmp_path, source, network
):
    _asked, searches = network
    searches["A"] = [_recording(31, "Thailand")]
    references = References(tmp_path / "xc", key="secret", countries="Thailand")
    httpd, base = _serve(
        tmp_path, source(count=40, seed=0), Settings(tap_to_listen=True), references=references
    )
    try:
        status, _headers, body = _fetch(base + "/listen/reference?name=Turdus%20merula")
        assert status == 200
        credit = json.loads(body)
        assert credit["recordist"] == "Recordist 31" and "secret" not in body.decode()
        status, headers, body = _fetch(
            base + "/listen/reference?what=audio&name=Turdus%20merula",
            headers={"Range": "bytes=1-2"},
        )
        assert (status, body, headers["Content-Type"]) == (206, b"ud", "audio/mpeg")
        assert headers["Content-Range"] == "bytes 1-2/5"
        assert _fetch(base + "/listen/reference?what=sonogram&name=Turdus%20merula")[0] == 200
    finally:
        httpd.shutdown()


# -- the tap targets ----------------------------------------------------------

NODE = shutil.which("node")


def _mapped(script: str) -> object:
    """Run `script` against web/static/listen.js's exports in Node, and read
    back what it prints as JSON."""
    module = Path(STATIC_DIR / "listen.js").as_uri()
    program = f"import {{ contentBox, target, stacking }} from {json.dumps(module)};\n{script}"
    done = subprocess.run(
        [NODE or "node", "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(done.stdout)


needs_node = pytest.mark.skipif(NODE is None, reason="the kiosk's mapping runs in Node")


@needs_node
def test_a_box_lands_on_the_picture_when_the_element_is_its_exact_shape():
    shown = _mapped(
        "const box = contentBox({left: 10, top: 20, width: 720, height: 540}, {width: 1440, height: 1080});"
        "console.log(JSON.stringify(target([144, 108, 288, 216], {width: 1440, height: 1080}, box)));"
    )
    assert shown == {"left": 82, "top": 74, "width": 72, "height": 54}


@needs_node
def test_a_letterboxed_picture_is_centred_in_its_element():
    # A 4:3 page in a portrait element, as on an iPad turned on its side.
    shown = _mapped(
        "console.log(JSON.stringify(contentBox({left: 0, top: 0, width: 768, height: 1024}, {width: 1440, height: 1080})));"
    )
    assert shown == {"left": 0, "top": 224, "width": 768, "height": 576}


@needs_node
def test_a_pillarboxed_picture_maps_its_corners_onto_the_screen():
    shown = _mapped(
        "const box = contentBox({left: 0, top: 0, width: 1366, height: 768}, {width: 1024, height: 768});"
        "console.log(JSON.stringify([box, target([0, 0, 1024, 768], {width: 1024, height: 768}, box)]));"
    )
    page = {"left": 171, "top": 0, "width": 1024, "height": 768}
    assert shown == [page, page]


@needs_node
def test_the_smallest_bird_is_on_top():
    order = _mapped(
        "console.log(JSON.stringify(stacking(["
        "{scientific: 'small', box: [0, 0, 10, 10]},"
        "{scientific: 'big', box: [0, 0, 100, 100]}]).map((b) => b.scientific)));"
    )
    assert order == ["big", "small"]


def test_off_by_default_and_seeded_like_the_others(monkeypatch):
    assert Settings().tap_to_listen is False
    monkeypatch.setenv("FUGLERAMME_TAP_TO_LISTEN", "true")
    assert from_env().tap_to_listen is True
    assert replace(Settings(), tap_to_listen=True).tap_to_listen
