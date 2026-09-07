"""On-disk cache for raw and parsed market data.

Layout (root defaults to ``<repo>/data/cache``)::

    data/cache/
      spot/YAHOO_EURUSD_1d.parquet          + .meta.json
      rates/FRED_SOFR.parquet               + .meta.json
      vol/YAHOO_OPT_FXE_20260918.parquet    + .meta.json
      oi/CME_6E_20260907.parquet            + .meta.json
      raw/<sha1>.bin                        raw payloads kept for replay/debug

Properties we need and guarantee:

* **Atomic** -- write to ``*.tmp-<pid>`` in the same directory then ``os.replace``; a reader
  never sees a half-written file, and a crash cannot corrupt a good cache entry.
* **TTL per data type** -- see :data:`DEFAULT_TTL`. Expiry is advisory: :meth:`Cache.get`
  returns ``None`` past TTL, :meth:`Cache.get_stale` returns it anyway (badged ``cached``).
  That is what makes the app usable offline.
* **read_only** -- a cache mounted read-only still serves; writes become no-ops.
* **refresh** -- ``Cache(..., refresh=True)`` (wired to ``--refresh``) makes every ``get``
  miss so the live adapters re-fetch, while writes still land.
* Parquet when :mod:`pyarrow` is importable, CSV otherwise. Sidecar ``.meta.json`` records
  source, url, fetch timestamp and row count so provenance survives a restart.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)

__all__ = ["Cache", "CacheEntry", "DEFAULT_TTL", "default_cache_root", "get_cache"]

#: seconds; keyed by `kind`
DEFAULT_TTL: dict[str, float] = {
    "spot_intraday": 60.0,            # last price
    "spot": 6 * 3600.0,               # daily OHLC history -- one bar a day
    "rates": 12 * 3600.0,             # policy/overnight rates move at most daily
    "vol_chain": 15 * 60.0,           # listed option chains during the US session
    "vol_index": 12 * 3600.0,         # EVZ & friends are daily closes
    "oi": 12 * 3600.0,                # exchange OI publishes once a day (T+1)
    "events": 24 * 3600.0,
    "raw": 24 * 3600.0,
}
_FALLBACK_TTL = 3600.0

try:                                   # parquet is preferred; CSV keeps us dependency-light
    import pyarrow  # noqa: F401
    _HAVE_PARQUET = True
except Exception:                      # pragma: no cover
    _HAVE_PARQUET = False


def default_cache_root() -> Path:
    env = os.environ.get("FXGAMMA_CACHE_DIR")
    if env:
        return Path(env).expanduser()
    # <repo>/data/cache -- fxgamma/data/cache.py -> parents[2] == repo root
    return Path(__file__).resolve().parents[2] / "data" / "cache"


@dataclass(frozen=True)
class CacheEntry:
    kind: str
    key: str
    path: Path
    fetched_at: datetime
    source: str = ""
    url: str = ""
    rows: int = 0
    note: str = ""

    def age(self) -> float:
        return (datetime.now(timezone.utc) - self.fetched_at).total_seconds()

    def fresh(self, ttl: float) -> bool:
        return self.age() < ttl


def _safe(key: str) -> str:
    keep = "".join(c if (c.isalnum() or c in "-_.=") else "_" for c in key)
    return keep[:120] if len(keep) <= 120 else keep[:100] + "_" + hashlib.sha1(
        key.encode()).hexdigest()[:12]


class Cache:
    def __init__(self, root: str | Path | None = None, *, refresh: bool = False,
                 read_only: bool = False, ttl: dict[str, float] | None = None):
        self.root = Path(root) if root is not None else default_cache_root()
        self.refresh = refresh
        self.read_only = read_only
        self.ttl = dict(DEFAULT_TTL)
        if ttl:
            self.ttl.update(ttl)
        if not self.read_only:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:               # read-only mount -> degrade, don't crash
                log.warning("cache root %s not writable (%s); running read-only",
                            self.root, exc)
                self.read_only = True

    # ------------------------------------------------------------------ paths
    def _dir(self, kind: str) -> Path:
        return self.root / kind

    def _path(self, kind: str, key: str) -> Path:
        ext = ".parquet" if _HAVE_PARQUET else ".csv"
        return self._dir(kind) / (_safe(key) + ext)

    def _meta_path(self, kind: str, key: str) -> Path:
        return self._path(kind, key).with_suffix(".meta.json")

    def ttl_for(self, kind: str) -> float:
        return self.ttl.get(kind, _FALLBACK_TTL)

    # ------------------------------------------------------------------ read
    def entry(self, kind: str, key: str) -> CacheEntry | None:
        p, m = self._path(kind, key), self._meta_path(kind, key)
        if not p.exists():
            return None
        meta: dict[str, Any] = {}
        if m.exists():
            try:
                meta = json.loads(m.read_text())
            except (OSError, ValueError):
                meta = {}
        ts = meta.get("fetched_at")
        try:
            fetched = (datetime.fromisoformat(ts) if ts
                       else datetime.fromtimestamp(p.stat().st_mtime, timezone.utc))
        except ValueError:
            fetched = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc)
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        return CacheEntry(kind=kind, key=key, path=p, fetched_at=fetched,
                          source=meta.get("source", ""), url=meta.get("url", ""),
                          rows=int(meta.get("rows", 0)), note=meta.get("note", ""))

    def _read(self, path: Path) -> pd.DataFrame:
        if path.suffix == ".parquet":
            return pd.read_parquet(path)
        df = pd.read_csv(path)
        for c in ("date", "datetime"):
            if c in df.columns:
                df[c] = pd.to_datetime(df[c], utc=True, errors="coerce")
        if "date" in df.columns:
            df = df.set_index("date")
        return df

    def get(self, kind: str, key: str, *, ttl: float | None = None) -> pd.DataFrame | None:
        """Fresh hit or ``None``. ``refresh=True`` always misses."""
        if self.refresh:
            return None
        e = self.entry(kind, key)
        if e is None or not e.fresh(self.ttl_for(kind) if ttl is None else ttl):
            return None
        try:
            return self._read(e.path)
        except Exception as exc:                 # noqa: BLE001
            log.warning("unreadable cache entry %s: %s", e.path, exc)
            return None

    def get_stale(self, kind: str, key: str) -> tuple[pd.DataFrame, CacheEntry] | None:
        """Any hit regardless of age -- the offline fallback. Caller must badge ``cached``."""
        e = self.entry(kind, key)
        if e is None:
            return None
        try:
            return self._read(e.path), e
        except Exception as exc:                 # noqa: BLE001
            log.warning("unreadable cache entry %s: %s", e.path, exc)
            return None

    # ------------------------------------------------------------------ write
    def put(self, kind: str, key: str, df: pd.DataFrame, *, source: str = "",
            url: str = "", note: str = "") -> Path | None:
        if self.read_only:
            return None
        path = self._path(kind, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{int(time.time()*1e6)}")
        try:
            out = df
            if path.suffix == ".parquet":
                if out.index.name or isinstance(out.index, pd.DatetimeIndex):
                    out = out.reset_index()
                out.to_parquet(tmp, index=False)
            else:
                out.to_csv(tmp, index=bool(out.index.name))
            os.replace(tmp, path)                       # atomic within the filesystem
        except Exception as exc:                        # noqa: BLE001
            log.warning("cache write failed for %s/%s: %s", kind, key, exc)
            tmp.unlink(missing_ok=True)
            return None
        meta = {"kind": kind, "key": key, "source": source, "url": url, "note": note,
                "rows": int(len(df)),
                "fetched_at": datetime.now(timezone.utc).isoformat()}
        mtmp = self._meta_path(kind, key).with_suffix(f".json.tmp-{os.getpid()}")
        try:
            mtmp.write_text(json.dumps(meta, indent=1))
            os.replace(mtmp, self._meta_path(kind, key))
        except OSError as exc:                          # pragma: no cover
            log.debug("meta write failed: %s", exc)
            mtmp.unlink(missing_ok=True)
        return path

    # -------------------------------------------------------------- raw blobs
    def put_raw(self, key: str, payload: bytes, *, source: str = "", url: str = "") -> Path | None:
        if self.read_only:
            return None
        d = self._dir("raw")
        d.mkdir(parents=True, exist_ok=True)
        p = d / (_safe(key) + ".bin")
        tmp = p.with_name(p.name + f".tmp-{os.getpid()}")
        try:
            tmp.write_bytes(payload)
            os.replace(tmp, p)
        except OSError as exc:                          # pragma: no cover
            log.warning("raw cache write failed: %s", exc)
            tmp.unlink(missing_ok=True)
            return None
        try:
            (d / (_safe(key) + ".meta.json")).write_text(json.dumps(
                {"source": source, "url": url, "bytes": len(payload),
                 "fetched_at": datetime.now(timezone.utc).isoformat()}, indent=1))
        except OSError:                                 # pragma: no cover
            pass
        return p

    def get_raw(self, key: str, *, ttl: float | None = None) -> bytes | None:
        p = self._dir("raw") / (_safe(key) + ".bin")
        if not p.exists() or self.refresh:
            return None
        if ttl is not None and time.time() - p.stat().st_mtime > ttl:
            return None
        try:
            return p.read_bytes()
        except OSError:                                 # pragma: no cover
            return None

    # ------------------------------------------------------------ maintenance
    def list_entries(self) -> list[CacheEntry]:
        out: list[CacheEntry] = []
        if not self.root.exists():
            return out
        for d in sorted(self.root.iterdir()):
            if not d.is_dir():
                continue
            for f in sorted(d.iterdir()):
                if f.suffix in (".parquet", ".csv"):
                    e = self.entry(d.name, f.stem)
                    if e:
                        out.append(e)
        return out

    def summary(self) -> pd.DataFrame:
        rows = [{**asdict(e), "path": str(e.path), "age_s": round(e.age(), 1),
                 "fresh": e.fresh(self.ttl_for(e.kind))} for e in self.list_entries()]
        return pd.DataFrame(rows, columns=["kind", "key", "source", "rows", "fetched_at",
                                           "age_s", "fresh", "url", "note", "path"])

    def clear(self, kind: str | None = None) -> int:
        if self.read_only:
            return 0
        target = self._dir(kind) if kind else self.root
        if not target.exists():
            return 0
        n = sum(1 for _ in target.rglob("*") if _.is_file())
        shutil.rmtree(target, ignore_errors=True)
        return n


_default: Cache | None = None


def get_cache(**kw) -> Cache:
    """Process-wide default cache (created on first use)."""
    global _default
    if _default is None or kw:
        c = Cache(**kw)
        if not kw:
            _default = c
        return c
    return _default
