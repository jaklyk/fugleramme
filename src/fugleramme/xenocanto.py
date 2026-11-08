"""Reference recordings from xeno-canto, for the kiosk's tap-to-listen card.

The frame fetches them, never the browser: the key stays on the server and a
kiosk only ever talks to one address. One recording per species is downloaded
into the data folder with the credit it has to carry, and kept, so a species is
asked about once.

Which recording: a song before a call, then the country it was recorded in,
by the tiers `FUGLERAMME_XENO_CANTO_COUNTRIES` lists (`Thailand; Malaysia,
Laos` is Thailand first, then either neighbour, then anywhere), then quality A
before B and so on. Quality A is asked for first, since a common species has
thousands of recordings and one page of answers is all that is read; only a
species with none at all is asked again at any quality.

The key is launch-only (`FUGLERAMME_XENO_CANTO_KEY`), never a setting:
settings.json is shown on the admin page and copied around, and xeno-canto
revokes a key that leaks.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import __version__
from .names import normalize
from .source import Unavailable

log = logging.getLogger(__name__)

API = "https://xeno-canto.org/api/3/recordings"
KEY = os.environ.get("FUGLERAMME_XENO_CANTO_KEY", "").strip()
COUNTRIES = os.environ.get("FUGLERAMME_XENO_CANTO_COUNTRIES", "")
_TIMEOUT = 30
_PER_PAGE = 500  # the API's most; a first page is all that is ranked
_QUALITIES = "ABCDE"
# A species xeno-canto has nothing for is asked again after this long.
_NONE_FOR = 7 * 24 * 3600
_AGENT = f"Fugleramme/{__version__} (+https://github.com/arnegiacomo/fugleramme)"


@dataclass(frozen=True)
class Reference:
    """One downloaded recording and what the card has to credit it with."""

    id: str
    recordist: str
    licence: str  # the licence's URL, as xeno-canto gives it
    link: str  # the recording's own page
    country: str
    kind: str  # xeno-canto's `type`: "song", "call", "alarm call, song", ...
    quality: str
    audio: str  # file names in the cache folder
    sonogram: str


def tiers(spec: str) -> tuple[tuple[str, ...], ...]:
    """`FUGLERAMME_XENO_CANTO_COUNTRIES` as tiers of lowercase country names."""
    found = (
        tuple(c.strip().lower() for c in tier.split(",") if c.strip()) for tier in spec.split(";")
    )
    return tuple(tier for tier in found if tier)


def _sung(recording: dict[str, Any]) -> bool:
    return any(part.strip() == "song" for part in str(recording.get("type", "")).split(","))


def rank(recording: dict[str, Any], countries: tuple[tuple[str, ...], ...]) -> tuple:
    """Sort key: a song first, then the nearest country tier, then the quality."""
    country = str(recording.get("cnt", "")).strip().lower()
    tier = next((i for i, names in enumerate(countries) if country in names), len(countries))
    quality = str(recording.get("q", "")).strip().upper()
    graded = _QUALITIES.index(quality) if quality and quality in _QUALITIES else len(_QUALITIES)
    return (not _sung(recording), tier, graded)


def pick(
    recordings: list[dict[str, Any]], countries: tuple[tuple[str, ...], ...]
) -> dict[str, Any] | None:
    """The recording the card plays, or None. Only one that can be downloaded,
    and one whose species xeno-canto calls identified."""
    usable = [
        r for r in recordings if r.get("file") and r.get("status", "identified") == "identified"
    ]
    return min(usable, key=lambda r: rank(r, countries), default=None)


def query(scientific: str, quality: str = "") -> str:
    """The v3 search for one species, which takes search tags only."""
    genus, _, epithet = scientific.strip().partition(" ")
    tags = [f"gen:{genus}", f"sp:{epithet.split(' ')[0]}", "grp:birds"]
    return " ".join([*tags, f"q:{quality}"] if quality else tags)


def _get(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": _AGENT})
    try:
        with urlopen(request, timeout=_TIMEOUT) as response:
            return response.read()
    except (HTTPError, URLError, OSError) as error:
        # The key rides in the query string, so the URL stays out of the message.
        raise Unavailable(f"xeno-canto did not answer: {getattr(error, 'code', error)}") from error


def _search(scientific: str, key: str, quality: str) -> list[dict[str, Any]]:
    params = urlencode({"query": query(scientific, quality), "key": key, "per_page": _PER_PAGE})
    try:
        answer = json.loads(_get(f"{API}?{params}"))
    except ValueError as error:
        raise Unavailable("xeno-canto answered no JSON") from error
    recordings = answer.get("recordings") if isinstance(answer, dict) else None
    return recordings if isinstance(recordings, list) else []


def _suffix(recording: dict[str, Any]) -> str:
    name = str(recording.get("file-name", ""))
    suffix = Path(name).suffix.lower()
    return suffix if suffix in (".mp3", ".wav", ".m4a", ".ogg", ".flac") else ".mp3"


def _download(recording: dict[str, Any], folder: Path) -> Reference:
    number = str(recording["id"])
    audio = f"XC{number}{_suffix(recording)}"
    # The medium sonogram: the colour one is several megabytes for a card a few hundred
    # pixels wide.
    sono = recording.get("sono") or {}
    sono_url = sono.get("med") or sono.get("large")
    sonogram = f"XC{number}.png" if sono_url else ""
    folder.mkdir(parents=True, exist_ok=True)
    _write(folder / audio, _get(str(recording["file"])))
    if sono_url:
        _write(folder / sonogram, _get(str(sono_url)))
    return Reference(
        id=number,
        recordist=str(recording.get("rec", "")),
        licence=_https(str(recording.get("lic", ""))),
        link=_https(str(recording.get("url", "")) or f"https://xeno-canto.org/{number}"),
        country=str(recording.get("cnt", "")),
        kind=str(recording.get("type", "")),
        quality=str(recording.get("q", "")),
        audio=audio,
        sonogram=sonogram,
    )


def _https(url: str) -> str:
    """v2 answered protocol-relative links (`//xeno-canto.org/...`); a card opened
    over plain HTTP would turn those into http:// ones."""
    return f"https:{url}" if url.startswith("//") else url


def _write(path: Path, body: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(body)
    tmp.replace(path)


class References:
    """The recordings already downloaded, under `folder`, one record per species.
    A lock across a fetch, so two taps on one bird download it once."""

    def __init__(self, folder: Path, key: str = KEY, countries: str = COUNTRIES):
        self.folder = folder
        self._key = key
        self._countries = tiers(countries)
        self._lock = threading.Lock()
        self._none: dict[str, float] = {}  # species -> when xeno-canto had nothing

    @property
    def enabled(self) -> bool:
        return bool(self._key)

    def held(self, scientific: str) -> Reference | None:
        """The species' recording if it is already downloaded, without asking."""
        try:
            raw = json.loads((self.folder / f"{normalize(scientific)}.json").read_text())
            found = Reference(**raw)
        except (OSError, ValueError, TypeError):
            return None
        return found if (self.folder / found.audio).is_file() else None

    def get(self, scientific: str) -> Reference | None:
        """The species' recording, downloaded on first ask. None when xeno-canto
        has none or no key is set; Unavailable when it could not be asked."""
        if not self._key:
            return None
        with self._lock:
            held = self.held(scientific)
            if held is not None:
                return held
            if time.monotonic() - self._none.get(scientific, -_NONE_FOR) < _NONE_FOR:
                return None
            chosen = pick(_search(scientific, self._key, "A"), self._countries) or pick(
                _search(scientific, self._key, ""), self._countries
            )
            if chosen is None:
                log.info("xeno-canto has no recording of %s", scientific)
                self._none[scientific] = time.monotonic()
                return None
            reference = _download(chosen, self.folder)
            _write(
                self.folder / f"{normalize(scientific)}.json",
                (json.dumps(asdict(reference), indent=2) + "\n").encode(),
            )
            log.info("Downloaded xeno-canto XC%s for %s", reference.id, scientific)
            return reference
