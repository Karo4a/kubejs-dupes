"""Тесты kubejs-dupes: синтетическая мини-сборка + проверка на реальном экспорте.

Синтетические тесты работают всегда. Интеграционные требуют готового экспорта:
переменная окружения KUBEJS_DUPES_EXPORT или папка export/ рядом с репозиторием
(см. README, раздел «Тесты»).

Запуск:
    python -m unittest discover -s tests -t . -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kubejs_dupes import graph as graph_mod
from kubejs_dupes import loader, prove, report as report_mod, search
from kubejs_dupes.verify import Verifier

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Экспорт задаётся окружением или копией в export/ рядом с репозиторием (в git её нет).
EXPORT = os.environ.get("KUBEJS_DUPES_EXPORT") or os.path.join(_ROOT, "export")
if not os.path.isdir(EXPORT):
    EXPORT = ""


def _write(root, rel, obj):
    path = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def build_fake_export(root):
    """Мини-сборка с заранее известным дюпом ×2 и одним «не дюпом»."""
    _write(root, "registries/item.json", {"a:ingot": "I", "a:dust": "I", "a:ore": "I",
                                          "a:block": "I", "a:stick": "I"})
    _write(root, "registries/fluid.json", {"a:molten": "F"})
    # тег с двумя членами: a:dust и a:dust2 (второй — алиас)
    _write(root, "tags/minecraft/item/c/dusts/a.json", ["a:dust"])
    _write(root, "registries/item.json", {"a:ingot": "I", "a:dust": "I", "a:ore": "I",
                                          "a:block": "I", "a:stick": "I", "a:dust2": "I"})
    # настоящий дюп: 1 слиток -> 3 пыли, 3 пыли -> 2 слитка
    _write(root, "recipes/a/smelt_claim.json", {
        "type": "minecraft:crafting_shapeless",
        "ingredients": [{"item": "a:ingot"}],
        "result": {"id": "a:dust", "count": 3},
    })
    _write(root, "recipes/a/dust_to_ingot.json", {
        "type": "minecraft:smelting",
        "ingredient": {"tag": "c:dusts/a"},
        "result": {"id": "a:ingot", "count": 2},
    })
    # тупик: 2 пыли -> 1 слиток (не дюп)
    _write(root, "recipes/a/dust_to_ingot_bad.json", {
        "type": "minecraft:smelting",
        "ingredient": {"item": "a:dust2"},
        "result": {"id": "a:stick", "count": 1},
    })
    # шанс 50 %: ore -> ore не должен считаться дюпом
    _write(root, "recipes/a/ore_chance.json", {
        "type": "minecraft:crushing",
        "ingredient": {"item": "a:ore"},
        "result": [{"id": "a:ore", "chance": 0.5}],
    })
    # литьё: форма-катализатор + жидкость
    _write(root, "recipes/a/cast.json", {
        "type": "a:item_casting",
        "cast": {"item": "a:ingot_cast"},
        "consume_cast": False,
        "fluid": {"tag": "c:molten/a", "amount": 90},
        "result": {"id": "a:block", "count": 1},
    })


class FakeExportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="kubejs_dupes_test_")
        build_fake_export(cls.tmp)
        cls.res, cls.recipes, cls.stats = loader.load(cls.tmp, use_cache=False, verbose=False)
        cls.g = graph_mod.build(cls.recipes, cls.res, verbose=False)

    def test_cast_is_catalyst(self):
        rec = next(r for r in self.recipes if r.rid == "a:cast")
        self.assertNotIn(("item", "a:ingot_cast"), rec.ins)
        self.assertIn(("fluid", "#c:molten/a"), rec.ins)
        self.assertEqual(rec.ins[("fluid", "#c:molten/a")], 90)

    def test_chance_not_dupe(self):
        rec = next(r for r in self.recipes if r.rid == "a:ore_chance")
        self.assertLess(rec.outs[("item", "a:ore")], 1)
        closed, _open = search.verify_candidates(
            self.g, self.res, search.find_candidates(self.g, max_len=2, min_profit=1.01, verbose=False),
            min_profit=1.01, verbose=False)
        seeds = {r["seed"][1] for r in closed}
        self.assertNotIn("a:ore", seeds)

    def test_dupe_found(self):
        cands = search.find_candidates(self.g, max_len=3, min_profit=1.01, verbose=False)
        closed, _open = search.verify_candidates(self.g, self.res, cands, min_profit=1.01,
                                                 verbose=False)
        found = [r for r in closed if r["seed"] in (("item", "a:ingot"), ("item", "a:dust"))]
        self.assertTrue(found, "дюп ×2 не найден")
        self.assertEqual(float(found[0]["profit"]), 6.0)

    def test_prove_detects_dupe(self):
        res = prove.run(self.g, self.res, self.recipes, verbose=False)
        self.assertTrue(res["available"])
        self.assertFalse(res["no_dupes"])


class ShapedTagPlusItemTest(unittest.TestCase):
    """Рецепт, берущий один и тот же предмет и напрямую, и через тег.

    Реальный пример: modern_industrialization:materials/coal/craft/dust_from_tiny_dust —
    pattern xxx/xyx/xxx, x = #c:tiny_dusts/coal, y = coal_tiny_dust: за крафт уходит 9 пылинок,
    а не 1. От этого зависел профит дюпа (×28 вместо реальных ×4).
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="kubejs_dupes_shaped_")
        _write(cls.tmp, "registries/item.json", {"a:dust": "I", "a:block": "I"})
        _write(cls.tmp, "tags/minecraft/item/c/dusts/a.json", ["a:dust"])
        _write(cls.tmp, "recipes/a/block_from_dust.json", {
            "type": "minecraft:crafting_shaped",
            "key": {"x": {"tag": "c:dusts/a"}, "y": {"item": "a:dust"}},
            "pattern": ["xxx", "xyx", "xxx"],
            "result": {"id": "a:block", "count": 1},
        })
        _write(cls.tmp, "recipes/a/dust_from_block.json", {
            "type": "minecraft:crafting_shapeless",
            "ingredients": [{"item": "a:block"}],
            "result": {"id": "a:dust", "count": 10},
        })
        cls.res, cls.recipes, _ = loader.load(cls.tmp, use_cache=False, verbose=False)
        cls.g = graph_mod.build(cls.recipes, cls.res, verbose=False)

    def test_qty_sums_tag_and_direct_entry(self):
        ver = Verifier(self.g, self.res)
        ridx = next(i for i, r in enumerate(self.recipes) if r.rid == "a:block_from_dust")
        node = self.g.index_of[("item", "a:dust")]
        self.assertEqual(ver.qty_in(ridx, node), 9)

    def test_graph_edge_ratio_is_honest(self):
        u = self.g.index_of[("item", "a:dust")]
        v = self.g.index_of[("item", "a:block")]
        ratios = [e.ratio for e in self.g.adj[u] if e.dst == v]
        self.assertEqual(ratios, [1 / 9])

    def test_cycle_profit_is_ten_ninths(self):
        cands = search.find_candidates(self.g, max_len=2, min_profit=1.01, verbose=False)
        closed, _open = search.verify_candidates(self.g, self.res, cands, min_profit=1.01,
                                                 verbose=False)
        profits = {r["seed"][1]: r["profit"] for r in closed}
        self.assertEqual(profits.get("a:dust"), Fraction(10, 9))


class CollapseRedundantTest(unittest.TestCase):
    """Свёртка «удлинённых» вариантов одного дюпа: ядро + обвязка 1:1."""

    @staticmethod
    def _item(ridx, profit, seed=("item", "a:dust")):
        return {"chain": [(0, 0, r, False, False) for r in ridx],
                "steps": [(r, Fraction(1)) for r in ridx],
                "profit": Fraction(profit), "seed": seed, "net": {}, "deficits": {}}

    def test_stretched_copy_is_folded(self):
        core = self._item([1, 2], 4)
        stretched = self._item([1, 2, 3, 4], 4)
        other = self._item([5, 6], 3)
        cores, folded = search.collapse_redundant([core, stretched, other])
        self.assertEqual(cores, [core, other])
        self.assertEqual(len(folded), 1)
        self.assertIs(folded[0]["core"], core)
        self.assertEqual(folded[0]["extra_ridx"], [3, 4])

    def test_different_profit_kept(self):
        a = self._item([1, 2], 4)
        b = self._item([1, 2, 3, 4], 5)
        cores, folded = search.collapse_redundant([a, b])
        self.assertEqual(cores, [a, b])
        self.assertEqual(folded, [])

    def test_chain_of_supersets_folds_to_innermost(self):
        a = self._item([1, 2], 4)
        b = self._item([1, 2, 3], 4)
        c = self._item([1, 2, 3, 4], 4)
        cores, folded = search.collapse_redundant([a, b, c])
        self.assertEqual(cores, [a])
        self.assertEqual({id(f["core"]) for f in folded}, {id(a)})


class _RatioVer:
    """Заглушка Verifier: qty_in = 1, qty_out = коэффициент шага."""

    def __init__(self, ratios):
        self.ratios = ratios                    # {(ridx, target_node): Fraction}

    def qty_in(self, ridx, node):
        return Fraction(1)

    def qty_out(self, ridx, node):
        return self.ratios.get((ridx, node), Fraction(1))


class UnrollAndGlueTest(unittest.TestCase):
    """Свёртка того, чего не видно по набору рецептов.

    * **развёрнутый шаг**: `ingot → plate → dust` вместо `ingot → dust`. Наборы рецептов
      не вложены (шаг ядра заменён, а не дополнен), но произведение коэффициентов пути
      равно коэффициенту шага — это тот же дюп.
    * **склейка**: длинный цикл = два готовых дюпа подряд, профит перемножается.
    * **другой маршрут при том же профите** (доска → дверь → доска против
      доска → fence gate → доска) — самостоятельный дюп, его трогать нельзя.
    """

    @staticmethod
    def _cyc(steps, profit, seed=("item", "a:dust")):
        return {"chain": [(u, v, r, False, False) for u, v, r in steps],
                "steps": [(r, Fraction(1)) for _, _, r in steps],
                "profit": Fraction(profit), "seed": seed, "net": {}, "deficits": {}}

    def test_unrolled_step_is_folded(self):
        core = self._cyc([(0, 1, 1), (1, 0, 2)], 6)                 # 0→1 ×2, 1→0 ×3
        unrolled = self._cyc([(0, 1, 1), (1, 2, 3), (2, 0, 4)], 6)  # шаг 1→0 развёрнут
        ver = _RatioVer({(1, 1): Fraction(2), (2, 0): Fraction(3),
                         (3, 2): Fraction(3), (4, 0): Fraction(1)})
        cores, folded = search.collapse_redundant([core, unrolled], ver=ver)
        self.assertEqual(cores, [core])
        self.assertEqual(len(folded), 1)
        self.assertEqual(folded[0]["kind"], "unroll")
        self.assertIs(folded[0]["core"], core)
        self.assertEqual(folded[0]["extra_ridx"], [3, 4])

    def test_same_profit_other_route_kept(self):
        core = self._cyc([(0, 1, 1), (1, 0, 2)], 6)
        other = self._cyc([(0, 3, 5), (3, 0, 6)], 6)                # другой промежуточный ресурс
        ver = _RatioVer({(1, 1): Fraction(2), (2, 0): Fraction(3),
                         (5, 3): Fraction(2), (6, 0): Fraction(3)})
        cores, folded = search.collapse_redundant([core, other], ver=ver)
        self.assertEqual(cores, [core, other])
        self.assertEqual(folded, [])

    def test_glued_pair_of_dupes_is_folded(self):
        a = self._cyc([(0, 1, 1), (1, 0, 2)], 4, seed=("item", "a:dust"))
        b = self._cyc([(2, 3, 3), (3, 2, 4)], 3, seed=("item", "b:dust"))
        glued = self._cyc([(0, 1, 1), (1, 0, 2), (2, 3, 3), (3, 2, 4)], 12,
                          seed=("item", "c:dust"))
        cores, folded = search.collapse_redundant([a, b, glued], ver=_RatioVer({}))
        self.assertEqual(cores, [a, b])
        self.assertEqual(len(folded), 1)
        self.assertEqual(folded[0]["kind"], "glue")
        self.assertIs(folded[0]["core"], a)
        self.assertIs(folded[0]["glue"], b)

    def test_glue_needs_exact_profit_product(self):
        a = self._cyc([(0, 1, 1), (1, 0, 2)], 4, seed=("item", "a:dust"))
        b = self._cyc([(2, 3, 3), (3, 2, 4)], 3, seed=("item", "b:dust"))
        fake = self._cyc([(0, 1, 1), (1, 0, 2), (2, 3, 3), (3, 2, 4)], 13,
                         seed=("item", "c:dust"))
        cores, folded = search.collapse_redundant([a, b, fake], ver=_RatioVer({}))
        self.assertEqual(cores, [a, b, fake])
        self.assertEqual(folded, [])


class RootCountAndMixedExclusionTest(unittest.TestCase):
    """Две формы рецептов, которые читались неверно.

    * modern_industrialization:forge_hammer — корневой `count` это число съедаемых предметов
      (3 raw → 4 dust), а не выход; из-за этого медь считалась дюпом ×4 вместо ×4/3.
    * quark:mixed_exclusion — сетка 3x3 с пустым центром: 8 предметов тега, `placeholder`
      только для JEI (иначе 1 доска → сундук → 8 досок давали фиктивный дюп ×8).
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="kubejs_dupes_roots_")
        _write(cls.tmp, "registries/item.json", {"a:raw_copper": "I", "a:copper_dust": "I",
                                                 "a:plank": "I", "a:chest": "I"})
        _write(cls.tmp, "tags/minecraft/item/c/raw_materials/copper.json", ["a:raw_copper"])
        _write(cls.tmp, "tags/minecraft/item/minecraft/planks.json", ["a:plank"])
        _write(cls.tmp, "recipes/a/hammer.json", {
            "type": "modern_industrialization:forge_hammer", "count": 3, "damage": 30,
            "ingredient": {"tag": "c:raw_materials/copper"},
            "result": {"count": 4, "id": "a:copper_dust"},
        })
        _write(cls.tmp, "recipes/a/chest.json", {
            "type": "quark:mixed_exclusion", "group": "chest",
            "output": {"id": "a:chest", "count": 1},
            "tag": "minecraft:planks",
            "placeholder": {"id": "a:plank", "count": 1},
        })
        _write(cls.tmp, "recipes/a/saw.json", {
            "type": "mekanism:sawing",
            "input": {"item": "a:chest"},
            "mainOutput": {"id": "a:plank", "count": 8},
        })
        # шанс стоит рядом с вложенной ссылкой, а не внутри неё (enderio:sag_milling)
        _write(cls.tmp, "recipes/a/mill.json", {
            "type": "enderio:sag_milling",
            "input": {"tag": "c:raw_materials/copper"},
            "outputs": [
                {"item": {"count": 1, "id": "a:copper_dust"}},
                {"chance": 0.35, "item": {"count": 1, "id": "a:copper_dust"}},
                {"chance": 0.043, "item": {"count": 1, "id": "a:grit"}},
            ],
        })
        # шанс на уровне контейнера, а выход — вложенным ключом (immersiveengineering:crusher)
        _write(cls.tmp, "recipes/a/crush.json", {
            "type": "immersiveengineering:crusher",
            "input": {"tag": "c:raw_materials/copper"},
            "result": {"item": "a:copper_dust"},
            "secondaries": [{"chance": 0.33333334, "output": {"item": "a:copper_dust"}}],
        })
        # бонусные выходы отдельным ключом itembi (chemicalscience/electrodynamics) —
        # раньше читались как ВХОДЫ и рождали циклы с профитом в миллионы
        _write(cls.tmp, "recipes/a/redox.json", {
            "type": "chemicalscience:redox_furnace_recipe",
            "iteminputs": [{"count": 1, "ingredient": {"tag": "c:raw_materials/copper"}}],
            "output": {"count": 1, "id": "a:copper_dust"},
            "itembi": [{"chance": 0.35, "item": {"count": 1, "id": "a:grit"}}],
        })
        # вторичный выход + шанс отдельным ключом (mekanism sawing)
        _write(cls.tmp, "recipes/a/saw2.json", {
            "type": "mekanism:sawing",
            "input": {"count": 1, "item": "a:chest"},
            "main_output": {"count": 8, "id": "a:plank"},
            "secondary_output": {"count": 2, "id": "a:grit"},
            "secondary_chance": 0.25,
        })
        cls.res, cls.recipes, _ = loader.load(cls.tmp, use_cache=False, verbose=False)
        cls.g = graph_mod.build(cls.recipes, cls.res, verbose=False)

    def _rec(self, rid):
        return next(r for r in self.recipes if r.rid == rid)

    def test_hammer_root_count_is_input(self):
        rec = self._rec("a:hammer")
        self.assertEqual(rec.ins[("item", "#c:raw_materials/copper")], 3)
        self.assertEqual(rec.outs[("item", "a:copper_dust")], 4)

    def test_mixed_exclusion_takes_eight_tag_items(self):
        rec = self._rec("a:chest")
        self.assertEqual(rec.ins, {("item", "#minecraft:planks"): 8})
        self.assertIn(("item", "a:plank"), rec.catalysts)

    def test_chance_beside_nested_item(self):
        rec = self._rec("a:mill")
        self.assertEqual(rec.outs[("item", "a:copper_dust")], Fraction(27, 20))
        self.assertEqual(rec.outs[("item", "a:grit")], Fraction(43, 1000))

    def test_bonus_items_are_outputs(self):
        rec = self._rec("a:redox")
        self.assertEqual(rec.ins, {("item", "#c:raw_materials/copper"): 1})
        self.assertEqual(rec.outs[("item", "a:copper_dust")], 1)
        self.assertEqual(rec.outs[("item", "a:grit")], Fraction(7, 20))

    def test_secondary_output_with_sibling_chance(self):
        rec = self._rec("a:saw2")
        self.assertEqual(rec.outs[("item", "a:plank")], 8)
        self.assertEqual(rec.outs[("item", "a:grit")], Fraction(1, 2))

    def test_container_chance_before_output_key(self):
        rec = self._rec("a:crush")
        self.assertEqual(rec.outs[("item", "a:copper_dust")],
                         Fraction(1) + Fraction("0.33333334"))

    def test_chest_not_a_dupe(self):
        cands = search.find_candidates(self.g, max_len=3, min_profit=1.01, verbose=False)
        closed, _open = search.verify_candidates(self.g, self.res, cands, min_profit=1.01,
                                                 verbose=False)
        self.assertEqual([r["seed"][1] for r in closed], [])


class OritechInputCountTest(unittest.TestCase):
    """oritech объявляет count у входа, но мод его не читает.

    OritechRecipeType.ORI_RECIPE_CODEC = Ingredient.CODEC_NONEMPTY.listOf(), OritechRecipe.inputs —
    List<Ingredient>, complexMatch() съедает по 1 предмету. Поэтому `count: 5` у стальной пыли —
    фикция: реально 1 пыль → 1 железо + 1 уголь (проверено в игре). Если верить count, коэффициент
    ребра 0.2 вместо 1.0 и настоящий дюп исчезает из отчёта.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="kubejs_dupes_oritech_")
        _write(cls.tmp, "registries/item.json", {"a:steel_dust": "I", "a:iron_dust": "I",
                                                 "a:coal_dust": "I", "a:steel_ingot": "I"})
        _write(cls.tmp, "tags/minecraft/item/c/dusts/steel.json", ["a:steel_dust"])
        _write(cls.tmp, "recipes/a/centrifuge.json", {
            "type": "oritech:centrifuge",
            "ingredients": [{"tag": "c:dusts/steel", "count": 5}],
            "results": [{"count": 1, "id": "a:iron_dust"}, {"count": 1, "id": "a:coal_dust"}],
            "time": 200,
        })
        # а у «обычного» мода count во входе — работает
        _write(cls.tmp, "recipes/a/plain_count.json", {
            "type": "minecraft:crafting_shapeless",
            "ingredients": [{"item": "a:iron_dust", "count": 5}],
            "result": {"id": "a:coal_dust", "count": 1},
        })
        cls.res, cls.recipes, _ = loader.load(cls.tmp, use_cache=False, verbose=False)
        cls.g = graph_mod.build(cls.recipes, cls.res, verbose=False)

    def _rec(self, rid):
        return next(r for r in self.recipes if r.rid == rid)

    def test_oritech_input_count_is_one(self):
        rec = self._rec("a:centrifuge")
        self.assertEqual(rec.ins, {("item", "#c:dusts/steel"): 1})
        self.assertEqual(rec.outs[("item", "a:iron_dust")], 1)
        self.assertIn("input_count_ignored", rec.flags)

    def test_oritech_edge_ratio_is_honest(self):
        u = self.g.index_of[("item", "a:steel_dust")]
        v = self.g.index_of[("item", "a:iron_dust")]
        ratios = [e.ratio for e in self.g.adj[u] if e.dst == v]
        self.assertEqual(ratios, [1.0])

    def test_other_mod_count_still_honoured(self):
        rec = self._rec("a:plain_count")
        self.assertEqual(rec.ins, {("item", "a:iron_dust"): 5})


@unittest.skipUnless(os.path.isdir(EXPORT), "экспорт сборки недоступен")
class RealExportTest(unittest.TestCase):
    """Интеграционные проверки на настоящем экспорте.

    Абсолютные значения (сталь ×3, медь ×1.5) — регресс эталонной сборки; на чужом
    экспорте таких рецептов нет, и проверка помечается skip, а не падает.
    """

    @classmethod
    def setUpClass(cls):
        cls.res, cls.recipes, cls.stats = loader.load(EXPORT)
        cls.g = graph_mod.build(cls.recipes, cls.res, verbose=False)
        cls.cands = search.find_candidates(cls.g, max_len=4, min_profit=1.02, verbose=False)
        cls.closed, cls.open_ = search.verify_candidates(cls.g, cls.res, cls.cands,
                                                         min_profit=1.02, verbose=False)

    def _recipe(self, rid):
        try:
            return next(r for r in self.recipes if r.rid == rid)
        except StopIteration:
            self.skipTest(f"в экспорте нет рецепта {rid} — тест написан под эталонную сборку")

    def test_steel_dupe_x3(self):
        hits = [r for r in self.closed
                if any("overgeared:steel_plate" in self.recipes[s[2]].rid for s in r["chain"])]
        if not hits:
            self.skipTest("дюпа стали нет в этом экспорте")
        self.assertAlmostEqual(float(hits[0]["profit"]), 3.0, places=3)

    def test_copper_dupe_x1_5(self):
        hits = [r for r in self.closed
                if any("melting/copper/from_wire" in self.recipes[s[2]].rid for s in r["chain"])]
        if not hits:
            self.skipTest("дюпа меди нет в этом экспорте")
        self.assertTrue(any(abs(float(r["profit"]) - 1.5) < 1e-6 for r in hits),
                        f"нет цикла ×1.5, есть: {[float(r['profit']) for r in hits]}")

    def test_parser_coverage(self):
        if self.stats["files"] < 5000:
            self.skipTest("маленький экспорт: проверка покрытия написана под полную сборку")
        ratio = self.stats["recipes_used"] / max(self.stats["files"], 1)
        self.assertGreater(ratio, 0.8, "разобрано меньше 80 % файлов рецептов")

    def test_forge_hammer_root_count_is_input(self):
        rec = self._recipe(
            "modern_industrialization:materials/copper/forge_hammer/raw_metal_to_dust_with_tool")
        self.assertEqual(rec.ins[("item", "#c:raw_materials/copper")], 3)
        self.assertEqual(rec.outs[("item", "alltheores:copper_dust")], 4)

    def test_mixed_chest_takes_eight_planks(self):
        rec = self._recipe("quark:building/crafting/chests/mixed_chest")
        self.assertEqual(rec.ins, {("item", "#minecraft:planks"): 8})
        self.assertIn(("item", "minecraft:oak_planks"), rec.catalysts)

    def test_crusher_secondary_chance(self):
        rec = self._recipe("immersiveengineering:crusher/raw_ore_copper")
        self.assertEqual(rec.outs[("item", "alltheores:copper_dust")],
                         Fraction(1) + Fraction("0.33333334"))

    def test_sag_milling_chance(self):
        rec = self._recipe("enderio:sag_milling/raw_copper")
        self.assertEqual(rec.outs[("item", "alltheores:copper_dust")], Fraction(27, 20))
        self.assertEqual(rec.outs[("item", "alltheores:gold_dust")], Fraction(43, 1000))

    def test_chest_dupe_gone(self):
        for res in self.closed:
            rids = {self.recipes[s[2]].rid for s in res["chain"]}
            if "mekanism:sawing/chest" in rids:
                self.assertNotIn("quark:building/crafting/chests/mixed_chest", rids,
                                 f"доска/сундук снова считается дюпом: {sorted(rids)}")

    def test_alternatives_for_tiny_dust_pair(self):
        for ident in ("modern_industrialization:coal_tiny_dust",
                      "modern_industrialization:coal_dust"):
            if ("item", ident) not in self.g.index_of:
                self.skipTest(f"в графе нет ресурса {ident} — тест под эталонную сборку")
        u = self.g.index_of[("item", "modern_industrialization:coal_tiny_dust")]
        v = self.g.index_of[("item", "modern_industrialization:coal_dust")]
        alts = report_mod._alt_recipes(self.g, self.recipes, {(u, v): Fraction(1, 9)})[(u, v)]
        ids = {self.recipes[r].rid for r in alts}
        self.assertIn("modern_industrialization:materials/coal/craft/dust_from_tiny_dust", ids)
        self.assertIn("modern_industrialization:materials/coal/packer/dust", ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)