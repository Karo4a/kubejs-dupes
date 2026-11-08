"""Ресурсы: реестры предметов/жидкостей и теги экспорта KubeJS.

Ключевой момент структуры тегов:
    tags/<datapack>/<registry>/<tag_ns>/<tag_path>.json
например tags/minecraft/item/c/wires/copper.json  ->  c:wires/copper (registry=item).
Registry — ВТОРОЙ сегмент пути, а не первый: иначе все c:* теги получают namespace "minecraft".
"""
from __future__ import annotations

import collections
import json
import os


class Resources:
    SPACES = ("item", "fluid", "chemical")

    def __init__(self, export_dir: str):
        self.export_dir = export_dir
        self.items = set(self._load_json(os.path.join(export_dir, "registries/item.json"), {}))
        self.fluids = set(self._load_json(os.path.join(export_dir, "registries/fluid.json"), {}))

        # tag id -> registry (item/fluid/block/...)
        self.tag_reg: dict[str, str] = {}
        # tag id -> list of raw entries ("other:item", "#c:nested/tag")
        self.tag_raw: dict[str, list[str]] = {}

        self._scan_tags()
        self._expanded: dict[str, frozenset[str]] = {}

    # ---------- io ----------
    @staticmethod
    def _load_json(path, default):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return default

    def _scan_tags(self) -> None:
        root = os.path.join(self.export_dir, "tags")
        for dirpath, _dirnames, filenames in os.walk(root):
            for fname in filenames:
                if not fname.endswith(".json"):
                    continue
                full = os.path.join(dirpath, fname)
                rel = os.path.relpath(full, root).replace("\\", "/")
                parts = rel.split("/")
                if len(parts) < 4:
                    continue  # datapack / registry / ns / path...
                registry = parts[1]
                tag_id = parts[2] + ":" + "/".join(parts[3:])[:-5]
                self.tag_reg[tag_id] = registry
                data = self._load_json(full, [])
                if isinstance(data, dict):
                    data = data.get("values", [])
                entries = []
                if isinstance(data, list):
                    for entry in data:
                        if isinstance(entry, str):
                            entries.append(entry.rstrip("?"))
                        elif isinstance(entry, dict) and "id" in entry:
                            entries.append(str(entry["id"]).rstrip("?"))
                self.tag_raw[tag_id] = entries

    # ---------- tag helpers ----------
    def space_of_tag(self, tag_id: str, hint: str | None = None) -> str:
        reg = self.tag_reg.get(tag_id)
        if reg in ("fluid", "chemical", "gases"):
            return "fluid" if reg == "fluid" else "chemical"
        if reg in ("item", "block"):
            return "item"
        return hint or "item"

    def tag_space_guess(self, tag_id: str) -> str | None:
        reg = self.tag_reg.get(tag_id)
        if reg in ("fluid", "chemical", "gases"):
            return "fluid" if reg == "fluid" else "chemical"
        if reg in ("item", "block"):
            return "item"
        return None

    def members(self, space: str, tag_id: str) -> frozenset[str]:
        """Рекурсивное разворачивание тега; результат кэшируется."""
        if tag_id in self._expanded:
            return self._expanded[tag_id]
        out: set[str] = set()
        stack = [tag_id]
        seen = set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            for entry in self.tag_raw.get(cur, ()):
                if entry.startswith("#"):
                    stack.append(entry[1:])
                else:
                    out.add(entry)
        res = frozenset(out)
        self._expanded[tag_id] = res
        return res

    def primary(self, space: str, tag_id: str) -> str | None:
        """Канонический член тега (первый, который есть в реестре)."""
        mem = self.members(space, tag_id)
        if not mem:
            return None
        known = self.items if space == "item" else self.fluids
        for entry in sorted(mem):
            if entry in known:
                return entry
        return sorted(mem)[0]

    def is_known_item(self, ident: str) -> bool:
        return ident in self.items