"""Durable record of every stock clip that has already appeared in a posted video.

Per-video dedup (`used` in clip_assemble) keeps two ranks from sharing footage,
but nothing stopped TOMORROW's list from reusing today's shot: "Weirdest Cat
Breeds" and "Cutest Cat Breeds" search the same Pexels pool, and the judge picks
the same best Sphynx clip both times. Recycled footage across uploads is the
kind of reuse YouTube's duplicate-content detection suppresses.

Two files, read as a union:
  * sourcing/clip_used.json — committed; what local/manual runs write, so a
    manual drop reaches the worker on the next deploy.
  * $AGENTDROP_DATA_DIR/clip_used.json — the worker's copy on the persistent
    volume, which survives redeploys that the repo file would be reset by.

Each entry stores both the source id (pexels_123) and the content hash, since
the same footage is sometimes republished under a second id.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from agentdrop_common import DATA_DIR, PROJECT_ROOT

log = logging.getLogger("agentdrop")

REPO_LEDGER = PROJECT_ROOT / "sourcing" / "clip_used.json"
VOLUME_LEDGER = DATA_DIR / "clip_used.json"
# Locally DATA_DIR is the project root; write the committed file so the record
# travels with the repo. On the worker, write the volume copy.
WRITE_LEDGER = REPO_LEDGER if DATA_DIR == PROJECT_ROOT else VOLUME_LEDGER


def _read(path: Path) -> list[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("clips", [])
    except FileNotFoundError:
        return []
    except Exception as e:
        log.warning("[clip-ledger] could not read %s: %s", path, e)
        return []


def used_keys() -> set[str]:
    """Every clip id and content hash already shown in a posted video."""
    keys: set[str] = set()
    for path in {REPO_LEDGER, VOLUME_LEDGER}:
        for e in _read(path):
            keys.update(k for k in (e.get("id"), e.get("hash")) if k)
    return keys


def record(clips: list[tuple[str, str]], post_id: str) -> None:
    """Add (id, hash) pairs for the clips in `post_id` (idempotent by id)."""
    entries = _read(WRITE_LEDGER)
    have = {e.get("id") for e in entries}
    added = 0
    for cid, h in clips:
        if cid in have:
            continue
        entries.append({"id": cid, "hash": h, "post_id": post_id})
        have.add(cid)
        added += 1
    if not added:
        return
    try:
        WRITE_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        WRITE_LEDGER.write_text(json.dumps({"clips": entries}, indent=2),
                                encoding="utf-8")
        log.info("[clip-ledger] recorded %d clip(s) for %s", added, post_id)
    except Exception as e:
        log.warning("[clip-ledger] could not write %s: %s", WRITE_LEDGER, e)
