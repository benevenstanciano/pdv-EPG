#!/usr/bin/env python3
"""Convert Parole di Vita / Be Joy Kids XMLTV into the custom <channel>/<airing> format."""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

USER_AGENT = "pdv-EPG/1.0 (+https://github.com/benevenstanciano/pdv-EPG)"
XMLTV_TIME_FORMATS = (
    "%Y%m%d%H%M%S %z",
    "%Y%m%d%H%M%S",
    "%Y%m%d%H%M %z",
    "%Y%m%d%H%M",
)

CHANNELS = (
    {
        "id": "ParolediVita.it",
        "name": "Parole di Vita",
        "source": "https://admin.paroledivita.org/api/v1/paroledivita-xmltv",
        "output": Path("docs/pdv.xml"),
    },
    {
        "id": "BeJoyKids.it",
        "name": "Be Joy Kids",
        "source": "https://admin.paroledivita.org/api/v1/bejoy-xmltv",
        "output": Path("docs/bejoy.xml"),
    },
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


def duration_minutes(start: datetime, end: datetime) -> int:
    seconds = (end - start).total_seconds()
    if seconds <= 0:
        return 0
    minutes = int(round(seconds / 60.0))
    return minutes if minutes > 0 else 1


def first_text(element: ET.Element | None, names: Iterable[str]) -> str:
    if element is None:
        return ""
    for name in names:
        child = element.find(name)
        if child is not None and child.text and child.text.strip():
            return " ".join(child.text.split())
    return ""


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
    encodings = ("utf-8", "iso-8859-1", "cp1252")
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


def convert_programmes(root: ET.Element) -> list[dict[str, str | int]]:
    airings: list[dict[str, str | int]] = []
    skipped = 0

    for programme in root.findall("programme"):
        start = parse_xmltv_datetime(programme.get("start"))
        stop = parse_xmltv_datetime(programme.get("stop"))
        title = first_text(programme, ("title",))
        description = first_text(programme, ("desc", "description", "sub-title"))

        if start is None or stop is None or not title:
            skipped += 1
            continue

        minutes = duration_minutes(start, stop)
        if minutes <= 0:
            skipped += 1
            continue

        airings.append(
            {
                "start": format_utc(start),
                "end": format_utc(stop),
                "duration": minutes,
                "title": title,
                "description": description,
            }
        )

    airings.sort(key=lambda item: (item["start"], item["end"], item["title"]))
    if not airings:
        raise ConversionError("Source feed contained no convertible programmes.")
    print(f"Converted {len(airings)} airings (skipped {skipped}).", file=sys.stderr)
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
        "<?xml version='1.0' encoding='UTF-8'?>\n"
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
    parser.add_argument("--source", help="Convert a single XMLTV URL or local file")
    parser.add_argument("--output", type=Path, help="Destination XML path (used with --source)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.source or args.output:
        if not args.source or not args.output:
            raise ConversionError("Use both --source and --output together.")
        convert_channel(args.source, args.output)
        return 0

    errors: list[str] = []
    for channel in CHANNELS:
        print(f"Fetching {channel['name']}...", file=sys.stderr)
        try:
            convert_channel(channel["source"], channel["output"])
        except ConversionError as exc:
            errors.append(f"{channel['name']}: {exc}")
    if errors:
        raise ConversionError("\n".join(errors))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConversionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
