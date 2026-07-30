"""Next.js Flight extraction and structural traversal helpers."""

from __future__ import annotations

from html.parser import HTMLParser
import json
from typing import Iterator

from .errors import PayloadError


class _ScriptTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_script = False
        self._parts: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.casefold() == "script":
            self._in_script = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._in_script:
            self.scripts.append("".join(self._parts))
            self._in_script = False
            self._parts = []


def extract_flight_text(html: str) -> str:
    parser = _ScriptTextParser()
    parser.feed(html)
    decoder = json.JSONDecoder()
    marker = "self.__next_f.push("
    records: list[str] = []
    marker_count = 0

    for script in parser.scripts:
        offset = 0
        while True:
            marker_index = script.find(marker, offset)
            if marker_index < 0:
                break
            marker_count += 1
            value_start = marker_index + len(marker)
            try:
                value, consumed = decoder.raw_decode(script[value_start:])
            except json.JSONDecodeError:
                offset = value_start + 1
                continue
            offset = value_start + consumed
            if (
                isinstance(value, list)
                and len(value) >= 2
                and isinstance(value[1], str)
            ):
                records.append(value[1])

    if not marker_count:
        raise PayloadError("page did not contain a Next.js Flight payload")
    if not records:
        raise PayloadError("Next.js Flight payload contained no string records")
    return "".join(records)


def iter_flight_values(html: str) -> Iterator[object]:
    """Yield JSON values from complete Flight record lines."""

    decoder = json.JSONDecoder()
    text = extract_flight_text(html)
    for line in text.splitlines():
        colon = line.find(":")
        if colon < 0:
            continue
        raw = line[colon + 1 :]
        try:
            value, _ = decoder.raw_decode(raw)
        except json.JSONDecodeError:
            continue
        yield value


def walk(value: object) -> Iterator[object]:
    """Depth-first traversal of dictionaries and lists."""

    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def walk_flight(html: str) -> Iterator[object]:
    for record in iter_flight_values(html):
        yield from walk(record)


def rendered_scalars(value: object) -> list[str | int]:
    """Collect visible scalar children from an RSC element tree."""

    if isinstance(value, (str, int)) and not isinstance(value, bool):
        if isinstance(value, str) and value.startswith("$"):
            return []
        return [value]
    if isinstance(value, list):
        if len(value) >= 4 and value[0] == "$" and isinstance(value[3], dict):
            return rendered_scalars(value[3].get("children"))
        result: list[str | int] = []
        for child in value:
            result.extend(rendered_scalars(child))
        return result
    if isinstance(value, dict):
        return rendered_scalars(value.get("children"))
    return []
