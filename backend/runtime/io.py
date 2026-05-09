"""Async file IO helpers built on top of ``aoffload``.

These wrap the few sync filesystem patterns the pipeline uses (read/write
text, read/write bytes, mkdir, exists, remove) so callers can ``await`` them
without thinking about the executor. ``awrite_text`` / ``awrite_bytes`` use
an atomic ``tmp + replace`` so a process crash mid-write never leaves a
half-written ``manuscript.md`` or state JSON.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Union

from .offload import aoffload

PathLike = Union[str, Path]


def _read_text(path: PathLike, encoding: str, errors: str) -> str:
    return Path(path).read_text(encoding=encoding, errors=errors)


def _write_text_atomic(path: PathLike, data: str, encoding: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(data, encoding=encoding)
    os.replace(tmp, p)


def _read_bytes(path: PathLike) -> bytes:
    return Path(path).read_bytes()


def _write_bytes_atomic(path: PathLike, data: bytes) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, p)


def _ensure_dir(path: PathLike) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def _exists(path: PathLike) -> bool:
    return Path(path).exists()


def _remove(path: PathLike, missing_ok: bool) -> None:
    p = Path(path)
    try:
        p.unlink()
    except FileNotFoundError:
        if not missing_ok:
            raise


async def aread_text(
    path: PathLike, encoding: str = "utf-8", errors: str = "strict"
) -> str:
    return await aoffload(_read_text, path, encoding, errors)


async def awrite_text(path: PathLike, data: str, encoding: str = "utf-8") -> None:
    await aoffload(_write_text_atomic, path, data, encoding)


async def aread_bytes(path: PathLike) -> bytes:
    return await aoffload(_read_bytes, path)


async def awrite_bytes(path: PathLike, data: bytes) -> None:
    await aoffload(_write_bytes_atomic, path, data)


async def aensure_dir(path: PathLike) -> None:
    await aoffload(_ensure_dir, path)


async def apath_exists(path: PathLike) -> bool:
    return await aoffload(_exists, path)


async def aremove(path: PathLike, *, missing_ok: bool = True) -> None:
    await aoffload(_remove, path, missing_ok)
