#!/usr/bin/env python3
"""Convert the ZipWave Silk Way XMLTV feed into the Toober <channel>/<airing> format.

Source: https://benevenstanciano.github.io/zip-epg/epg-silkway.xml
Output: docs/silkway.xml

Same layout as docs/pdv.xml:
  title, airing_type, startDateTime required
  endDateTime and duration in whole minutes
  timezone="UTC"
  startDateTime / endDateTime as YYYY-MM-DD HH:MM:SS

Rules:
  - Prefer the Latin-script duplicate when two programmes share a start time.
  - Drop leftover non-Latin titles/descriptions.
  - Empty or non-Latin titles become "Silk Way TV".
  - Empty or non-Latin descriptions become the Silk Way filler sentence.
  - Timeline holes between airings are filled with those same defaults.
"""

from __future__ import annotations

import argparse
import sys
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

USER_AGENT = "pdv-EPG/1.0 (+https://github.com/benevenstanciano/pdv-EPG)"
XMLTV_TIME_FORMATS = (
    "%Y%m%d%H%M%S %z",
    "%Y%m%d%H%M%S",
    "%Y%m%d%H%M %z",
    "%Y%m%d%H%M",
)

SOURCE = "https://benevenstanciano.github.io/zip-epg/epg-silkway.xml"
OUTPUT = Path("docs/silkway.xml")
FILLER_TITLE = "Silk Way TV"
FILLER_DESCRIPTION = (
    "Silk Way TV is Kazakhstan's global news, culture and business channel."
)


class ConversionError(RuntimeError):
    """Raised when a source feed cannot be turned into a valid EPG file."""


def parse_xmltv_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = " ".join(value.strip().split())
    for fmt in XMLTV_TIME_FORMATS:
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def format_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def snap_to_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def align_airing_times(start: datetime, end: datetime) -> tuple[datetime, datetime, int]:
    """Make duration a whole number of minutes that exactly matches end - start."""
    start = snap_to_minute(start)
    end = snap_to_minute(end)
    if end <= start:
        end = start + timedelta(minutes=1)
    minutes = int((end - start).total_seconds() // 60)
    return start, end, minutes


def first_text(element: ET.Element | None, names: tuple[str, ...]) -> str:
    if element is None:
        return ""
    for name in names:
        child = element.find(name)
        if child is not None and child.text and child.text.strip():
            return " ".join(child.text.split())
    return ""


def is_latin_text(value: str) -> bool:
    """True when every letter is Latin script. Punctuation and digits are allowed."""
    if not value or not value.strip():
        return False
    letters = [ch for ch in value if ch.isalpha()]
    if not letters:
        return False
    for ch in letters:
        name = unicodedata.name(ch, "")
        if not name.startswith("LATIN"):
            return False
    return True


def latin_score(title: str, description: str) -> tuple[int, int, int]:
    """Higher is better: Latin title, then Latin description, then longer title."""
    return (
        1 if is_latin_text(title) else 0,
        1 if is_latin_text(description) else 0,
        len(title),
    )


def linearize_airings(airings: list[dict[str, str | int]]) -> list[dict[str, str | int]]:
    """Drop nested programmes and clip partial overlaps so the guide is linear."""
    linearized: list[dict[str, str | int]] = []
    dropped = 0
    clipped = 0

    for item in airings:
        current = dict(item)
        if linearized and str(current["start"]) < str(linearized[-1]["end"]):
            if str(current["end"]) <= str(linearized[-1]["end"]):
                dropped += 1
                continue
            linearized[-1]["end"] = current["start"]
            prev_start = datetime.strptime(str(linearized[-1]["start"]), "%Y-%m-%d %H:%M:%S")
            prev_end = datetime.strptime(str(linearized[-1]["end"]), "%Y-%m-%d %H:%M:%S")
            minutes = int((prev_end - prev_start).total_seconds() // 60)
            if minutes <= 0:
                linearized.pop()
            else:
                linearized[-1]["duration"] = minutes
                clipped += 1
        linearized.append(current)

    print(
        f"Linearized to {len(linearized)} airings (clipped {clipped}, dropped nested {dropped}).",
        file=sys.stderr,
    )
    return linearized


def fill_gaps(
    airings: list[dict[str, str | int]],
    title: str,
    description: str,
) -> list[dict[str, str | int]]:
    """Insert filler airings in holes between consecutive programmes."""
    if not title or len(airings) < 2:
        return airings

    filled: list[dict[str, str | int]] = []
    inserted = 0
    for item in airings:
        if filled:
            prev_end = datetime.strptime(str(filled[-1]["end"]), "%Y-%m-%d %H:%M:%S")
            next_start = datetime.strptime(str(item["start"]), "%Y-%m-%d %H:%M:%S")
            minutes = int((next_start - prev_end).total_seconds() // 60)
            if minutes > 0:
                filled.append(
                    {
                        "start": filled[-1]["end"],
                        "end": item["start"],
                        "duration": minutes,
                        "title": title,
                        "description": description,
                    }
                )
                inserted += 1
        filled.append(item)

    print(f"Filled {inserted} gaps with {title!r}.", file=sys.stderr)
    return filled


def fetch_bytes(source: str) -> bytes:
    path = Path(source)
    if path.exists() and path.is_file():
        return path.read_bytes()

    request = Request(
        source,
        headers={"User-Agent": USER_AGENT, "Accept": "application/xml,text/xml,*/*"},
    )
    try:
        with urlopen(request, timeout=60) as response:
            return response.read()
    except HTTPError as exc:
        raise ConversionError(f"HTTP {exc.code} while fetching {source}") from exc
    except URLError as exc:
        raise ConversionError(f"Failed to fetch {source}: {exc.reason}") from exc


def parse_source_xml(payload: bytes) -> ET.Element:
    encodings = ("utf-8", "cp1252", "iso-8859-1")
    last_error: Exception | None = None
    for encoding in encodings:
        try:
            return ET.fromstring(payload.decode(encoding))
        except (UnicodeDecodeError, ET.ParseError) as exc:
            last_error = exc
    try:
        return ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ConversionError(f"Could not parse source XML: {last_error or exc}") from exc


def normalize_copy(title: str, description: str) -> tuple[str, str]:
    if not is_latin_text(title):
        title = FILLER_TITLE
    if not is_latin_text(description):
        description = FILLER_DESCRIPTION
    return title, description


def convert_programmes(root: ET.Element) -> list[dict[str, str | int]]:
    grouped: dict[datetime, list[dict[str, object]]] = {}
    skipped = 0

    for programme in root.findall("programme"):
        start = parse_xmltv_datetime(programme.get("start"))
        stop = parse_xmltv_datetime(programme.get("stop"))
        title = first_text(programme, ("title",))
        description = first_text(programme, ("desc", "description", "sub-title"))

        if start is None or stop is None:
            skipped += 1
            continue

        start, stop, minutes = align_airing_times(start, stop)
        if minutes <= 0:
            skipped += 1
            continue

        grouped.setdefault(start, []).append(
            {
                "start": start,
                "stop": stop,
                "minutes": minutes,
                "title": title,
                "description": description,
            }
        )

    airings: list[dict[str, str | int]] = []
    ignored_non_latin = 0
    for start in sorted(grouped):
        candidates = grouped[start]
        chosen = max(
            candidates,
            key=lambda item: latin_score(str(item["title"]), str(item["description"])),
        )
        ignored_non_latin += max(0, len(candidates) - 1)

        title, description = normalize_copy(
            str(chosen["title"]),
            str(chosen["description"]),
        )
        stop = chosen["stop"]
        assert isinstance(stop, datetime)
        start_utc, stop_utc, minutes = align_airing_times(start, stop)
        airings.append(
            {
                "start": format_utc(start_utc),
                "end": format_utc(stop_utc),
                "duration": minutes,
                "title": title,
                "description": description,
            }
        )

    airings.sort(key=lambda item: (item["start"], item["end"], item["title"]))
    airings = linearize_airings(airings)
    airings = fill_gaps(airings, FILLER_TITLE, FILLER_DESCRIPTION)
    if not airings:
        raise ConversionError("Source feed contained no convertible programmes.")
    print(
        f"Converted {len(airings)} airings "
        f"(skipped {skipped}, ignored {ignored_non_latin} same-time duplicates).",
        file=sys.stderr,
    )
    return airings


def build_output_xml(airings: list[dict[str, str | int]]) -> ET.Element:
    channel = ET.Element("channel")
    for item in airings:
        airing = ET.SubElement(
            channel,
            "airing",
            {
                "startDateTime": str(item["start"]),
                "endDateTime": str(item["end"]),
                "duration": str(item["duration"]),
                "timezone": "UTC",
            },
        )
        title = ET.SubElement(airing, "title", {"lang": "en"})
        title.text = str(item["title"])
        airing_type = ET.SubElement(airing, "airing_type")
        airing_type.text = "episode"
        description = ET.SubElement(airing, "description", {"lang": "en"})
        description.text = str(item["description"])
    return channel


def render_xml(root: ET.Element, source: str) -> str:
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    header = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "\n"
        "<!--\n"
        "title, airing_type, startDateTime are required fields\n"
        "either endDateTime or duration is required\n"
        "timezone preferred as UTC\n"
        "startDateTime and endDateTime date format must be YYYY-MM-DD HH:MM:SS\n"
        "duration value must be minutes\n"
        f"generated from {source}\n"
        "-->\n"
        "\n"
    )
    return header + body + "\n"


def write_output(path: Path, xml_text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(xml_text, encoding="utf-8")
    print(f"Wrote {path} ({path.stat().st_size} bytes).", file=sys.stderr)


def convert_channel(source: str, output: Path) -> None:
    payload = fetch_bytes(source)
    source_root = parse_source_xml(payload)
    airings = convert_programmes(source_root)
    output_root = build_output_xml(airings)
    write_output(output, render_xml(output_root, source))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        default=SOURCE,
        help="ZipWave Silk Way XMLTV URL or local file",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT,
        help="Destination Toober XML path",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(f"Fetching Silk Way from {args.source}...", file=sys.stderr)
    convert_channel(args.source, args.output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConversionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
