"""Tap-to-listen (`Settings.tap_to_listen`): what the kiosk needs to find a bird
on its picture and play it.

`describe` is the page as the card sees it: the picture's size, its token, and
for each bird the box the render drew it in, its names and its latest call. The
boxes are the ones `modes.page` drew the PNG with, never a second render, and
the calls come off the source the page was drawn from, so describing a page
costs no round trip of its own while the feed is fresh.

`clip` passes a detection's audio or spectrogram through from BirdNET-Go, so the
kiosk only ever talks to the frame. Its Range header goes along with it: Safari
plays nothing it cannot ask for in pieces.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from . import modes
from .languages import Namer
from .names import normalize
from .source import Detection

# What BirdNET-Go serves for a detection, by the name the kiosk asks for it under.
CLIPS = {"audio": "/audio/{}", "spectrogram": "/spectrogram/{}"}
# Passed back to the browser as BirdNET-Go sent them. Retry-After comes with the
# 503 of a clip still being written.
PASSED = (
    "content-type",
    "content-range",
    "accept-ranges",
    "retry-after",
    "etag",
    "last-modified",
)


class Detector(Protocol):
    """BirdNET-Go's own API, as `api.Configured.request` answers it."""

    def request(
        self,
        path: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, str], bytes]: ...


def clip(
    detector: Detector, what: str, detection: int, wanted: str = ""
) -> tuple[int, dict[str, str], bytes]:
    """BirdNET-Go's answer for one detection's `what` (a `CLIPS` key), as it
    came: a 206 for a range, 503 with Retry-After while the clip is written, 404
    for one it no longer has."""
    status, headers, body = detector.request(
        CLIPS[what].format(detection), headers={"Range": wanted} if wanted else None
    )
    return status, {name: headers[name] for name in PASSED if name in headers}, body


def byte_range(header: str, size: int) -> tuple[int, int] | None:
    """The one span (first, last byte) a `Range: bytes=` header asks of a file of
    `size` bytes, or None for the whole file: no header, several spans, or one
    that misses the file. `bytes=-500` is the last 500 bytes."""
    unit, _, spec = header.partition("=")
    first, dash, last = spec.strip().partition("-")
    if unit.strip() != "bytes" or not dash or "," in spec or not size:
        return None
    if not first:
        return (max(0, size - int(last)), size - 1) if last.isdigit() and int(last) else None
    if not first.isdigit() or (last and not last.isdigit()):
        return None
    start, end = int(first), min(int(last) if last else size - 1, size - 1)
    return (start, end) if start <= end else None


def latest_calls(ctx: modes.Context) -> dict[str, Detection]:
    """Each species' newest detection in the feed the modes already read."""
    calls: dict[str, Detection] = {}
    for detection in ctx.source.recent(modes.RECENT_SCAN):  # newest first
        calls.setdefault(normalize(detection.scientific_name), detection)
    return calls


def _when(at: datetime) -> str:
    return at.isoformat(timespec="seconds")


def describe(
    page: modes.Page,
    token: str,
    calls: dict[str, Detection],
    namer: Namer,
    english: Namer,
    clips: bool,
) -> dict[str, Any]:
    """The page as `/page.json` serves it. `english` names a bird when the page's
    own languages leave only its scientific name; `clips` is whether the
    detector serves them at all (a BirdWeather station does not)."""
    birds = []
    for bird in page.birds:
        call = calls.get(normalize(bird.name))
        named = [n for n in namer.parts(bird.name) if n != bird.name]
        birds.append(
            {
                "box": list(bird.box),
                "scientific": bird.name,
                "common": named[0] if named else english.parts(bird.name)[0],
                "detection": None
                if call is None
                else {"id": call.id if clips else None, "at": _when(call.detected_at)},
            }
        )
    return {"token": token, "width": page.size[0], "height": page.size[1], "birds": birds}
