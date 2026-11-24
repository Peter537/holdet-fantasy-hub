"""Shared validation and bounded reads for portable Hub archives."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import ntpath
import stat
from typing import BinaryIO
from zipfile import ZipFile, ZipInfo

from .errors import PayloadError


COPY_CHUNK = 1024 * 1024


@dataclass(slots=True)
class _Entry:
    name: str
    children: dict[str, _Entry] | None


def portable_parts(name: str) -> tuple[str, ...]:
    """Validate member components before a host filesystem interprets them."""

    parts = tuple(name.split("/"))
    if any(
        not part
        or ":" in part
        or "\\" in part
        or part.endswith((".", " "))
        or ntpath.isreserved(part)
        for part in parts
    ):
        raise PayloadError(f"Usikker sti i ZIP: {name}")
    return parts


def preflight_archive(
    archive: ZipFile,
    *,
    max_members: int,
    max_member_bytes: int,
    max_total_bytes: int,
    max_compression_ratio: int,
) -> dict[str, ZipInfo]:
    infos = archive.infolist()
    if len(infos) > max_members:
        raise PayloadError("ZIP-filen indeholder for mange medlemmer")
    if sum(info.file_size for info in infos) > max_total_bytes:
        raise PayloadError("ZIP-filens udpakkede størrelse er for stor")

    root: dict[str, _Entry] = {}
    members: dict[str, ZipInfo] = {}
    for info in infos:
        name = info.orig_filename
        parts = portable_parts(name)
        if name != info.filename or info.is_dir() or stat.S_ISLNK(info.external_attr >> 16):
            raise PayloadError(f"Usikker sti i ZIP: {name}")
        if info.file_size > max_member_bytes:
            raise PayloadError(f"ZIP-medlemmet er for stort: {name}")
        if info.file_size > info.compress_size * max_compression_ratio:
            raise PayloadError(f"ZIP-medlemmet har mistænkelig kompressionsratio: {name}")
        directory = root
        for part in parts[:-1]:
            key = part.casefold()
            entry = directory.get(key)
            if entry is None:
                entry = directory[key] = _Entry(part, {})
            if entry.name != part or entry.children is None:
                raise PayloadError(f"ZIP-filen har en stikonflikt: {name}")
            directory = entry.children
        key = parts[-1].casefold()
        if key in directory:
            raise PayloadError(f"ZIP-filen har dubletter eller en stikonflikt: {name}")
        directory[key] = _Entry(parts[-1], None)
        members[name] = info
    return members


def bounded_chunks(handle: BinaryIO, limit: int, *, error: str) -> Iterator[bytes]:
    remaining = limit
    while True:
        chunk = handle.read(min(COPY_CHUNK, remaining + 1))
        if not isinstance(chunk, bytes):
            raise PayloadError("Kilden skal levere bytes")
        if not chunk:
            return
        if len(chunk) > remaining:
            raise PayloadError(error)
        remaining -= len(chunk)
        yield chunk


def member_chunks(archive: ZipFile, info: ZipInfo, *, limit: int) -> Iterator[bytes]:
    error = f"ZIP-medlemmet er for stort: {info.filename}"
    if info.file_size > limit:
        raise PayloadError(error)
    size = 0
    with archive.open(info) as member:
        for chunk in bounded_chunks(member, info.file_size, error=error):
            size += len(chunk)
            yield chunk
    if size != info.file_size:
        raise PayloadError(f"ZIP-medlemmets størrelse ændrede sig: {info.filename}")


def read_member(archive: ZipFile, info: ZipInfo, *, limit: int) -> bytes:
    return b"".join(member_chunks(archive, info, limit=limit))
