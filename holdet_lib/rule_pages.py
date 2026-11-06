"""Capture official rules, including iframe documents and collapsed sections."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

from .errors import PayloadError
from .http import HttpClient
from .models import GameUrl
from .persistence import aware_local, publish_immutable_text
from .rules import GameRuleProfile, game_rule_from_dict, game_rule_to_dict


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str | None] = field(default_factory=dict)
    children: list = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, _Node):
                yield from child.walk()

    def text(self) -> str:
        if self.tag in {"script", "style", "noscript"}:
            return ""
        return " ".join(
            child.text() if isinstance(child, _Node) else child
            for child in self.children
        )

    def has_class(self, name: str) -> bool:
        return name in (self.attrs.get("class") or "").split()


class _HTML(HTMLParser):
    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self.stack = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input",
                       "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(_Node(tag, dict(attrs)))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_rule_document(html: str) -> dict:
    """Read hidden text as data; never execute document scripts."""
    if len(html) > 4_000_000:
        raise PayloadError("Rules document exceeds the 4 MB parser limit")
    root = _HTML(html).root
    sections = []
    for panel in root.walk():
        if not (panel.has_class("collapsible-panel") or panel.tag == "details"):
            continue
        headings = [node for node in panel.walk()
                    if node.has_class("collapsible") or node.tag == "summary"]
        contents = [node for node in panel.walk() if node.has_class("collapsible-content")]
        heading = _clean(headings[0].text()) if headings else ""
        if contents:
            text = _clean(" ".join(node.text() for node in contents))
        elif panel.tag == "details":
            text = _clean(" ".join(
                child.text() if isinstance(child, _Node) else child
                for child in panel.children
                if not isinstance(child, _Node) or child.tag != "summary"
            ))
        else:
            text = ""
        sections.append({"heading": heading, "text": text})
    tables = []
    for table in (node for node in root.walk() if node.tag == "table"):
        rows = [
            [_clean(cell.text()) for cell in row.children
             if isinstance(cell, _Node) and cell.tag in {"td", "th"}]
            for row in table.walk() if row.tag == "tr"
        ]
        tables.append([row for row in rows if row])
    text = _clean(root.text())
    missing = []
    if not tables or not any(tables):
        missing.append("scoring_tables")
    if len(text) < 300:
        missing.append("rule_text")
    missing.extend(f"empty_section:{index + 1}" for index, section in enumerate(sections)
                   if not section["heading"] or not section["text"])
    return {
        "text": text, "tables": tables, "sections": sections,
        "section_count": len(sections), "table_count": len(tables),
        "extraction_complete": not missing, "missing": missing,
    }


def _allowed_document(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https" and parsed.hostname == "cdn.holdet.dk"
        and parsed.port in {None, 443} and not parsed.username and not parsed.password
        and parsed.path.startswith("/nexus/fantasy-app-rule-pages/")
        and parsed.path.endswith(".html") and not parsed.query and not parsed.fragment
    )


def fetch_rule_page(game: GameUrl, *, fetch_text=None, now=None) -> dict:
    fetch = fetch_text or HttpClient().fetch_text
    page_url = f"https://www.holdet.dk/{game.locale}/fantasy/{game.slug}/rules"
    page_html = fetch(page_url)
    parsed_page = _HTML(page_html).root
    sources = tuple(dict.fromkeys(
        urljoin(page_url, node.attrs.get("src") or "")
        for node in parsed_page.walk() if node.tag == "iframe"
        and _allowed_document(urljoin(page_url, node.attrs.get("src") or ""))
    ))
    if len(sources) > 1:
        raise PayloadError("Rules page references multiple rule documents; review in the browser")
    document_url = sources[0] if sources else page_url
    document_html = fetch(document_url) if sources else page_html
    parsed = parse_rule_document(document_html)
    if not sources and not parsed["extraction_complete"]:
        raise PayloadError("No readable rules document found; inspect the rules page in the browser")
    return {
        "schema_version": 1, "parser_version": 1,
        "game": {"locale": game.locale.casefold(), "slug": game.slug},
        "source_url": page_url, "document_url": document_url,
        "fetched_at": aware_local(now).isoformat(),
        "source_sha256": sha256(document_html.encode("utf-8")).hexdigest(),
        "page_sha256": sha256(page_html.encode("utf-8")).hexdigest(),
        "html": document_html, "page_html": page_html,
        "rules_verified": False, **parsed,
    }


def public_capture(capture: dict) -> dict:
    return {key: value for key, value in capture.items()
            if key not in {"html", "page_html"}}


def _evidence_time(value: object, label: str) -> datetime:
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.utcoffset() is None:
            raise ValueError()
    except (ValueError, TypeError) as exc:
        raise PayloadError(f"Rules {label} needs a timezone-aware timestamp") from exc
    return stamp.astimezone(timezone.utc)


def _latest_record(paths, timestamp: str, label: str) -> dict | None:
    records = []
    for path in paths:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise PayloadError(f"Unsupported rules {label}")
        records.append((_evidence_time(raw.get(timestamp), label), path.name, raw))
    # Legacy filenames use local wall time, so only record timestamps establish order.
    return max(records, key=lambda row: row[:2])[2] if records else None


def validate_capture(capture: object, game: GameUrl) -> dict:
    if not isinstance(capture, dict) or capture.get("schema_version") != 1:
        raise PayloadError("Unsupported rules capture")
    identity = {"locale": game.locale.casefold(), "slug": game.slug}
    expected_url = f"https://www.holdet.dk/{game.locale}/fantasy/{game.slug}/rules"
    if capture.get("game") != identity or capture.get("source_url") != expected_url:
        raise PayloadError("Rules capture belongs to another game")
    html = capture.get("html")
    if not isinstance(html, str) or sha256(html.encode("utf-8")).hexdigest() != capture.get("source_sha256"):
        raise PayloadError("Rules capture fingerprint mismatch")
    document_url = capture.get("document_url")
    if not isinstance(document_url, str) or not (
        document_url == expected_url or _allowed_document(document_url)
    ):
        raise PayloadError("Rules document must be an official Holdet source")
    page_html = capture.get("page_html")
    if not isinstance(page_html, str) or sha256(page_html.encode("utf-8")).hexdigest() != capture.get("page_sha256"):
        raise PayloadError("Rules page fingerprint mismatch")
    if document_url != expected_url and not any(
        node.tag == "iframe" and urljoin(expected_url, node.attrs.get("src") or "") == document_url
        for node in _HTML(page_html).root.walk()
    ):
        raise PayloadError("Rules page does not reference the captured document")
    _evidence_time(capture.get("fetched_at"), "capture")
    parsed = parse_rule_document(html)
    if any(capture.get(key) != value for key, value in parsed.items()):
        raise PayloadError("Rules capture extraction does not match its HTML")
    return capture


def reviewed_rule_profile(game: GameUrl, capture: dict, raw_profile: dict, note: str) -> GameRuleProfile:
    """Validate a review without creating evidence, audit files, or locks."""

    validate_capture(capture, game)
    if not capture["extraction_complete"]:
        raise PayloadError("Incomplete rules capture cannot be audited")
    if not isinstance(note, str) or not note.strip():
        raise PayloadError("A review note explaining the checked rules is required")
    allowed = game_rule_to_dict(GameRuleProfile(game.locale, game.slug, game.slug)).keys()
    if unknown := raw_profile.keys() - allowed:
        raise PayloadError(f"Unknown rule fields: {', '.join(sorted(unknown))}")
    locale = raw_profile.get("game_locale", game.locale)
    if not isinstance(locale, str) or locale.casefold() != game.locale.casefold() or raw_profile.get("game_slug", game.slug) != game.slug:
        raise PayloadError("Rule profile belongs to another game")
    raw = {
        **raw_profile, "game_locale": game.locale.casefold(), "game_slug": game.slug,
        "verified": True, "source_url": capture["source_url"],
        "document_url": capture["document_url"], "source_sha256": capture["source_sha256"],
        "accessed_on": datetime.fromisoformat(capture["fetched_at"]).date().isoformat(),
    }
    return game_rule_from_dict(raw)


class RulePageStore:
    """Immutable evidence and explicitly reviewed season profiles."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def _root(self, game: GameUrl) -> Path:
        if not all(re.fullmatch(r"[a-zA-Z0-9_-]+", value) for value in (game.locale, game.slug)):
            raise PayloadError("Invalid rules game identity")
        return self.directory / game.locale.casefold() / game.slug

    def save(self, game: GameUrl, capture: dict) -> Path:
        validate_capture(capture, game)
        stamp = _evidence_time(capture["fetched_at"], "capture").strftime("%Y%m%dT%H%M%S%fZ")
        path = self._root(game) / f"capture-{stamp}-{capture['source_sha256'][:12]}.json"
        content = json.dumps(capture, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            publish_immutable_text(path, content)
        return path

    def latest(self, game: GameUrl) -> dict | None:
        raw = _latest_record(self._root(game).glob("capture-*.json"), "fetched_at", "capture")
        return validate_capture(raw, game) if raw is not None else None

    def audit(self, game: GameUrl, capture: dict, raw_profile: dict, note: str) -> GameRuleProfile:
        profile = reviewed_rule_profile(game, capture, raw_profile, note)
        reviewed_at = aware_local()
        stamp = reviewed_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = self._root(game) / f"audit-{stamp}.json"
        self.save(game, capture)
        publish_immutable_text(path, json.dumps({
            "schema_version": 1, "reviewed_at": reviewed_at.isoformat(),
            "review_note": note.strip(), "profile": game_rule_to_dict(profile),
        }, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        return profile

    def profile(self, game: GameUrl) -> GameRuleProfile | None:
        raw = _latest_record(self._root(game).glob("audit-*.json"), "reviewed_at", "audit")
        if raw is None:
            return None
        if raw.get("schema_version") != 1:
            raise PayloadError("Unsupported rules audit")
        profile = game_rule_from_dict(raw.get("profile"))
        if profile.identity != (game.locale.casefold(), game.slug) or not profile.verified:
            raise PayloadError("Rules audit has an invalid game identity")
        latest = self.latest(game)
        if latest is None:
            raise PayloadError("Audited rules have no matching evidence capture")
        if (latest["source_sha256"] != profile.source_sha256
                or latest["document_url"] != profile.document_url
                or latest["source_url"] != profile.source_url):
            return None
        return profile
