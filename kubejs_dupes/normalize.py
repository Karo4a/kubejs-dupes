"""Нормализация рецептов KubeJS-экспорта в единый вид.

Recipe = {
    rid, rtype, file,
    ins : {(space, id_или_"#tag"): Fraction},   # списывается за 1 крафт
    outs: {(space, id_или_"#tag"): Fraction},   # ожидаемое количество (count*chance)
    flags: set[str],
}

Правила:
  * катализаторы (формы литья, молды, инструменты, входы с probability=0) НЕ попадают во входы;
  * шансы выходов умножаются (режим expected), факт использования отмечается флагом chance_used;
  * количества — Fraction, чтобы 135 mB -> 1.5 слитка не превращалось в 1.4999999;
  * shaped-рецепты (pattern+key) считаются по числу вхождений символа в паттерн.
"""
from __future__ import annotations

from fractions import Fraction

import re

_MOLD_RE = re.compile(r"(^|:)(mold_|molds?/)|(_mold|_cast)$")

# --- словари ключей -------------------------------------------------------
INPUT_KEYS = {
    "ingredient", "ingredients", "input", "inputs", "input_item", "input_items",
    "inputitem", "inputitems", "item_input", "item_inputs", "iteminput", "iteminputs",
    "fluid_input", "fluid_inputs", "fluidinput", "fluidinputs", "input_fluid",
    "input_fluids", "inputfluid", "main_input", "extra_input", "extra_inputs",
    "extrainputs", "materials", "material", "cast", "solvent", "solvents",
    "sources", "source", "additives", "addition", "activation_item", "base",
    "fluid", "inputitem_1", "items_in", "inputstack",
}
OUTPUT_KEYS = {
    "result", "results", "output", "outputs", "item_outputs", "itemoutputs",
    "itemoutput", "item_output", "item_result", "itemresult", "fluid_output",
    "fluidoutput", "output_fluid", "outputfluid", "output_item", "outputitem",
    "main_output", "mainoutput", "secondary", "secondaries", "secondary_output",
    "secondary_outputs", "secondaryoutput", "secondaryoutputs", "fluid_outputs",
    "guaranteed_outputs", "guaranteedoutputs", "bonus_outputs", "bonusoutputs",
    "bonus_items", "bonusitems", "chance_outputs", "extra_output", "extra_outputs",
    # chemicalscience / electrodynamics: itembi/fluidbi/gasbi — списки бонусных выходов с шансом
    "itembi", "fluidbi", "gasbi",
}
# шанс отдельным ключом рядом с выходом (mekanism sawing: secondary_output + secondary_chance)
SIBLING_CHANCE = {
    "secondary_output": "secondary_chance",
    "secondaryoutput": "secondary_chance",
    "secondary_outputs": "secondary_chance",
    "secondaryoutputs": "secondary_chance",
}
# инструменты/формы — не расходуются никогда
CATALYST_KEYS = {"tool", "tools", "mold", "molds", "template", "templates", "cast_item",
                 "catalyst"}
# служебные поля
SKIP_KEYS = {
    "type", "category", "group", "pattern", "key", "key_", "count", "amount",
    "chance", "probability", "experience", "cookingtime", "processing_time",
    "processingtime", "duration", "time", "energy", "eu", "ticks", "usagepertick",
    "heat_requirement", "minimum_temperature", "maximum_temperature", "consume_cast",
    "loops", "transitional_item", "neoforge:conditions", "bookshelf:load_conditions",
    "_kubejs_changed_marker", "show_notification", "hammering", "has_quality",
    "needs_minigame", "tier", "spirits", "ritual_type", "crafttype", "research",
    "soulcost", "mercuryflux", "required_advancement", "particleeffectidentifier",
    "particlecount", "soundeventidentifier", "bonus", "per_tick_usage",
    "ignore_crushing_multiplier", "temperature", "min_temperature", "max_temperature",
    "id", "item", "tag", "fluid", "chemical", "gas", "ingredient_item",
}
# типы, у которых ключ "fluid"/"item" означает ВЫХОД, а не вход
TYPE_OVERRIDES = {
    "actuallyadditions:pressing": {"fluid": "out"},
    "caupona:bowl": {"fluid": "out", "item": "out"},
    "actuallyadditions:liquid_fuel": {"fluid": "out"},
}
# Корневой count — это СКОЛЬКО ПРЕДМЕТОВ СЪЕДАЕТ крафт (damage рядом — износ инструмента),
# а не количество выхода: {"type": "…forge_hammer", "count": 3, "ingredient": …, "result": {"count": 4}}.
ROOT_COUNT_AS_INPUT = {"modern_industrialization:forge_hammer"}
# Quark mixed chest/furnace: сетка 3x3, центр ПУСТОЙ, остальные 8 слотов — предметы одного тега,
# причём разных типов. "placeholder" — только для JEI/книги рецептов, в крафте НЕ участвует
# (MixedExclusionRecipe.matches: input.size()==9, input.getItem(4).isEmpty(), 8 слотов по тегу).
MIXED_EXCLUSION = "quark:mixed_exclusion"


def _qty_field(d):
    for k in ("count", "amount"):
        if k in d:
            v = d[k]
            if isinstance(v, (int, float)):
                return Fraction(str(v)), True
            if isinstance(v, str) and v.replace(".", "", 1).isdigit():
                return Fraction(v), True
    return None, False


class Normalizer:
    def __init__(self, resources, chance_mode: str = "expected"):
        self.res = resources
        self.chance_mode = chance_mode
        self.unknown = set()          # ключи, которые не удалось классифицировать

    # ---------- определение ресурса ----------
    def _classify_ref(self, d):
        """dict -> (space, id, qty, chance) или None, если это не ссылка на ресурс."""
        tag = d.get("tag")
        if isinstance(tag, str):
            space = self.res.tag_space_guess(tag)
            if space is None:
                space = "fluid" if ("amount" in d and "count" not in d) else "item"
            qty, _ = _qty_field(d)
            return space, "#" + tag, (qty if qty is not None else None)

        for key, space in (("fluid", "fluid"), ("chemical", "chemical"), ("gas", "chemical")):
            val = d.get(key)
            if isinstance(val, str):
                qty, _ = _qty_field(d)
                return space, val, (qty if qty is not None else None)

        item = d.get("item")
        if isinstance(item, str):
            qty, _ = _qty_field(d)
            return "item", item, (qty if qty is not None else None)

        ident = d.get("id")
        if isinstance(ident, str):
            # fluid.csv/productivemetalworks: "id" + "amount" без "count" — это жидкость,
            # даже если такой предмет с тем же id тоже существует (pneumaticcraft:plastic)
            if "amount" in d and "count" not in d:
                space = "fluid"
            else:
                space = "fluid" if (ident in self.res.fluids and ident not in self.res.items) else "item"
            qty, _ = _qty_field(d)
            return space, ident, (qty if qty is not None else None)
        return None

    @staticmethod
    def _chance_of(d):
        for k in ("chance", "probability"):
            if k in d:
                v = d[k]
                if isinstance(v, (int, float)):
                    return Fraction(str(v))
        return None

    def _refs_of(self, value):
        """Все ссылки на ресурсы внутри значения (без количеств) — для катализаторов."""
        out = []
        stack = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                ref = self._classify_ref(item)
                if ref is not None:
                    out.append((ref[0], ref[1]))
                else:
                    stack.extend(v for v in item.values() if isinstance(v, (dict, list)))
        return out

    # ---------- сбор ссылок ----------
    def _collect(self, value, side, mult, acc, inherit=False, depth=0):
        if depth > 8 or value is None:
            return
        if isinstance(value, list):
            for entry in value:
                self._collect(entry, side, mult, acc, inherit, depth + 1)
            return
        if isinstance(value, str):
            if side == "in":
                acc[side][("item", value)] = acc[side].get(("item", value), 0) + mult
            else:
                acc[side][("item", value)] = acc[side].get(("item", value), 0) + mult
            return
        if not isinstance(value, dict):
            return

        ref = self._classify_ref(value)
        if ref is not None:
            space, ident, qty = ref
            if qty is None:
                # количество не задано: предмет = 1, жидкость = 1 бакет,
                # но если множитель пришёл из amount родителя — считаем, что он уже учтён
                qty = Fraction(1) if (space == "item" or inherit) else Fraction(1000)
            chance = self._chance_of(value)
            if side == "in":
                if chance is not None and chance == 0:
                    acc["flags"].add("catalyst_dropped")
                    acc["cat"].add((space, ident))   # не расходуемый катализатор
                    return
                amount = mult * qty
            else:
                amount = mult * qty
                if chance is not None and chance < 1:
                    if self.chance_mode == "strict":
                        acc["flags"].add("chance_skipped")
                        return
                    amount *= chance
                    acc["flags"].add("chance_used")
            acc[side][(space, ident)] = acc[side].get((space, ident), Fraction(0)) + amount
            return

        # контейнер: множитель из count/amount этого уровня
        m = mult
        inherit_child = inherit
        qty, had = _qty_field(value)
        if had and qty is not None:
            m = mult * qty
            inherit_child = True
        # шанс может стоять рядом с вложенной ссылкой, а не в ней:
        # enderio:sag_milling → {"chance": 0.35, "item": {"count": 1, "id": …}}
        # Шанс на уровне контейнера описывает вероятность всего поддерева и должен применяться
        # независимо от того, в какую сторону его разбирают: immersiveengineering:crusher →
        # "secondaries": [{"chance": 0.33, "output": {...}}] (иначе 1 + 1 = 2 пыли вместо 4/3).
        ch = self._chance_of(value)
        if ch is not None:
            if ch == 0:
                if side == "out":
                    acc["flags"].add("chance_skipped")
                else:
                    acc["flags"].add("catalyst_dropped")
                    acc["cat"].update(self._refs_of(value))
                return
            if ch < 1:
                if self.chance_mode == "strict" and side == "out":
                    acc["flags"].add("chance_skipped")
                    return
                if self.chance_mode != "strict":
                    m = m * ch
                    acc["flags"].add("chance_used")

        for key, val in value.items():
            kl = key.lower()
            if kl in CATALYST_KEYS:
                acc["flags"].add("catalyst_dropped")
                acc["cat"].update(self._refs_of(val))
                continue
            if kl in INPUT_KEYS:
                self._collect(val, "in", m, acc, inherit_child, depth + 1)
            elif kl in OUTPUT_KEYS:
                self._collect(val, "out", m, acc, inherit_child, depth + 1)
            elif kl in SKIP_KEYS:
                # {"item": {...}} — вложенная ссылка под ключом item
                if kl == "item" and isinstance(val, dict):
                    self._collect(val, side, m, acc, inherit_child, depth + 1)
                continue
            elif isinstance(val, (dict, list)):
                self._collect(val, side, m, acc, inherit_child, depth + 1)

    # ---------- shaped ----------
    def _pattern_inputs(self, d, acc):
        key = d.get("key") or d.get("key_") or {}
        if not isinstance(key, dict):
            return False
        letters = {}
        for row in d.get("pattern") or []:
            if isinstance(row, str):
                for ch in row:
                    if ch != " ":
                        letters[ch] = letters.get(ch, 0) + 1
        if not letters:
            return False
        for ch, n in letters.items():
            entry = key.get(ch)
            if entry is None:
                continue
            self._collect(entry, "in", Fraction(n), acc)
        return True

    # ---------- основной вход ----------
    def parse(self, data: dict, rid: str, rtype: str, file: str):
        from .loader import Recipe

        acc = {"in": {}, "out": {}, "flags": set(), "cat": set()}
        overrides = TYPE_OVERRIDES.get(rtype, {})

        shaped = False
        if isinstance(data.get("pattern"), list):
            shaped = self._pattern_inputs(data, acc)

        for key, val in data.items():
            kl = key.lower()
            if kl.startswith("_"):
                continue
            forced = overrides.get(kl)
            if forced:
                self._collect(val, forced, Fraction(1), acc)
                continue
            if kl in CATALYST_KEYS:
                acc["flags"].add("catalyst_dropped")
                acc["cat"].update(self._refs_of(val))
                continue
            if kl in INPUT_KEYS:
                if shaped and kl in ("key", "key_"):
                    continue
                self._collect(val, "in", Fraction(1), acc)
            elif kl in OUTPUT_KEYS:
                m = Fraction(1)
                sib = SIBLING_CHANCE.get(kl)
                if sib:
                    raw = next((v for k, v in data.items() if k.lower() == sib), None)
                    if isinstance(raw, (int, float)) and 0 <= raw < 1:
                        if self.chance_mode == "strict":
                            acc["flags"].add("chance_skipped")
                            continue
                        m = Fraction(str(raw))
                        acc["flags"].add("chance_used")
                self._collect(val, "out", m, acc)
            elif kl in SKIP_KEYS:
                continue
            elif isinstance(val, (dict, list)):
                self._collect(val, "in", Fraction(1), acc)

        # --- типы со своим размещением количеств ---------------------------
        if rtype in ROOT_COUNT_AS_INPUT:
            n, ok = _qty_field(data)
            if ok and n and n > 1:
                acc["in"] = {k: v * n for k, v in acc["in"].items()}
                acc["flags"].add("root_count_input")

        if rtype == MIXED_EXCLUSION:
            tag = data.get("tag")
            ph = data.get("placeholder")
            if isinstance(tag, str):
                space = self.res.tag_space_guess(tag) or "item"
                # 8 слотов по тегу, центр пустой; placeholder не расходуется
                acc["in"] = {(space, "#" + tag): Fraction(8)}
                acc["flags"].add("mixed_exclusion_8_tag")
                if isinstance(ph, dict):
                    ref = self._classify_ref(ph)
                    if ref:
                        acc["cat"].add((ref[0], ref[1]))
                        acc["flags"].add("catalyst_dropped")

        # форма для литья: по умолчанию НЕ тратится, если явно не сказано consume_cast=true
        cast = data.get("cast")
        if isinstance(cast, dict) and data.get("consume_cast") is not True:
            ref = self._classify_ref(cast)
            if ref:
                acc["in"].pop((ref[0], ref[1]), None)
                acc["flags"].add("catalyst_dropped")
                acc["cat"].add((ref[0], ref[1]))

        ins = {k: v for k, v in acc["in"].items() if v > 0}
        outs = {k: v for k, v in acc["out"].items() if v > 0}

        # Молды/формы в литейных рецептах (evolvedmekanism:solidifying и пр.) — многоразовые.
        # Признак: рецепт потребляет жидкость, а среди входов есть предмет с mold/cast в id.
        if any(space == "fluid" for space, _ in ins):
            for key in list(ins):
                if key[0] == "item" and _MOLD_RE.search(key[1]):
                    ins.pop(key)
                    acc["flags"].add("catalyst_dropped")
                    acc["cat"].add(key)

        return Recipe(rid=rid, rtype=rtype, file=file, ins=ins, outs=outs, flags=acc["flags"],
                      catalysts=acc["cat"])