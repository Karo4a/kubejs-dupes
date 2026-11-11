"""Загрузка экспорта: обход recipes/, разбор errors.log, кэш нормализации."""
from __future__ import annotations

import hashlib
import json
import os
import pickle
import re
import sys
import time

from .normalize import Normalizer
from .resources import Resources


class Recipe:
    __slots__ = ("rid", "rtype", "file", "ins", "outs", "flags", "catalysts")

    def __init__(self, rid, rtype, file, ins, outs, flags, catalysts=None):
        self.rid = rid
        self.rtype = rtype or "?"
        self.file = file
        self.ins = ins
        self.outs = outs
        self.flags = flags
        self.catalysts = catalysts or set()

    def __repr__(self):  # pragma: no cover
        return f"<Recipe {self.rid} {self.rtype} in={len(self.ins)} out={len(self.outs)}>"


def recipe_id_from_path(rel: str) -> str:
    stem = rel[:-5] if rel.endswith(".json") else rel
    ns, _, path = stem.partition("/")
    return f"{ns}:{path}" if path else ns


def read_error_ids(export_dir: str) -> set[str]:
    """Рецепты, которые KubeJS сгенерировал с ошибкой и игра НЕ загрузила."""
    ids = set()
    path = os.path.join(export_dir, "errors.log")
    if not os.path.exists(path):
        return ids
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = re.search(r"Error parsing recipe ([^\s(]+)", line)
            if m:
                ids.add(m.group(1))
    return ids


JUNK_IDS = {"minecraft:air", "minecraft:barrier", "minecraft:structure_void",
            "ifeu:air", "tfmg:air"}


def _is_junk(key) -> bool:
    ident = key[1]
    return "jei_dummy" in ident or ident in JUNK_IDS


def iter_recipe_files(export_dir: str):
    """Только recipes/ — added_recipes дублирует их побайтово, removed_recipes содержит СТАРЫЕ версии."""
    root = os.path.join(export_dir, "recipes")
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            if fname.endswith(".json"):
                yield os.path.join(dirpath, fname)


def load(export_dir: str, chance_mode: str = "expected", use_cache: bool = True,
         verbose: bool = True, cache_dir: str | None = None):
    """-> (resources, recipes, stats)

    cache_dir — куда класть кэш нормализации. CLI передаёт <--out>/cache, чтобы кэш лежал
    рядом с отчётами; по умолчанию берётся <repo>/out/cache.
    """
    export_dir = os.path.abspath(export_dir)
    if cache_dir is None:
        cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "out", "cache")
    stamp = _stamp(export_dir, chance_mode)
    # Кэш привязан к пути экспорта: у разных экспортов (игра + копия в repo/export)
    # иначе один файл, а штамп с путём внутри — и каждый прогон греет кэш заново.
    tag = hashlib.sha1(export_dir.lower().encode("utf-8")).hexdigest()[:8]
    cache_file = os.path.join(cache_dir, f"normalized_{chance_mode}_{tag}.pkl")
    legacy = os.path.join(cache_dir, f"normalized_{chance_mode}.pkl")

    if use_cache:
        os.makedirs(cache_dir, exist_ok=True)
        payload = _read_cache(cache_file, stamp, verbose)
        if payload is None and os.path.exists(legacy):
            payload = _read_cache(legacy, stamp, verbose, quiet=True)
            if payload is not None:                     # старый общий файл — переезжает
                try:
                    os.replace(legacy, cache_file)
                except OSError:
                    pass
        if payload is not None:
            return payload["resources"], payload["recipes"], payload["stats"]

    t0 = time.time()
    resources = Resources(export_dir)
    normalizer = Normalizer(resources, chance_mode=chance_mode)
    bad_ids = read_error_ids(export_dir)

    recipes = []
    stats = {"files": 0, "bad_json": 0, "skipped_error_log": 0, "no_inputs": 0,
             "no_outputs": 0, "by_type": {}, "flags": {}}
    for path in iter_recipe_files(export_dir):
        stats["files"] += 1
        rel = os.path.relpath(path, export_dir).replace("\\", "/")
        rel_in_recipes = rel[len("recipes/"):] if rel.startswith("recipes/") else rel
        rid = recipe_id_from_path(rel_in_recipes)
        if rid in bad_ids:
            stats["skipped_error_log"] += 1
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            stats["bad_json"] += 1
            continue
        if not isinstance(data, dict):
            stats["bad_json"] += 1
            continue
        rtype = data.get("type") if isinstance(data.get("type"), str) else "?"
        try:
            rec = normalizer.parse(data, rid, rtype, rel)
        except Exception:
            stats["bad_json"] += 1
            continue
        per = stats["by_type"].setdefault(rtype, {"total": 0, "ok": 0, "no_in": 0, "no_out": 0})
        per["total"] += 1
        rec.ins = {k: v for k, v in rec.ins.items() if not _is_junk(k)}
        rec.outs = {k: v for k, v in rec.outs.items() if not _is_junk(k)}
        if not rec.ins:
            per["no_in"] += 1
            stats["no_inputs"] += 1
        if not rec.outs:
            per["no_out"] += 1
            stats["no_outputs"] += 1
        if rec.ins and rec.outs:
            per["ok"] += 1
            recipes.append(rec)
        for flag in rec.flags:
            stats["flags"][flag] = stats["flags"].get(flag, 0) + 1

    stats["recipes_used"] = len(recipes)
    stats["seconds"] = round(time.time() - t0, 1)
    if verbose:
        print(f"[load] {stats['files']} файлов, использовано {len(recipes)} рецептов, "
              f"{stats['seconds']} c")

    if use_cache:
        try:
            with open(cache_file, "wb") as fh:
                pickle.dump({"stamp": stamp, "resources": resources,
                             "recipes": recipes, "stats": stats}, fh, protocol=4)
        except Exception as exc:  # pragma: no cover
            print(f"[cache] не сохранилось: {exc}", file=sys.stderr)
    return resources, recipes, stats


def _read_cache(path: str, stamp: str, verbose: bool, quiet: bool = False):
    """Прочитать кэш, если его штамп совпал с текущими данными. Иначе None."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as fh:
            payload = pickle.load(fh)
    except Exception as exc:
        if verbose and not quiet:
            print(f"[cache] не прочитался ({type(exc).__name__}: {exc}) — разбираю заново",
                  file=sys.stderr)
        return None
    if payload.get("stamp") != stamp:
        if verbose and not quiet:
            print(f"[cache] устарел: {payload.get('stamp')} != {stamp}", file=sys.stderr)
        return None
    if verbose:
        print(f"[cache] {path}")
    return payload


def _stamp(export_dir: str, chance_mode: str) -> str:
    try:
        mt = os.path.getmtime(os.path.join(export_dir, "recipes"))
    except OSError:
        mt = 0
    return f"{export_dir}|{mt}|{chance_mode}|v13"
