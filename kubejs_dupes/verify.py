"""Точная проверка цикла: считаем кратности рецептов и баланс ресурсов на Fraction."""
from __future__ import annotations

from fractions import Fraction


def _tag_of(recipe, ident: str):
    """Какие теги-входы/выходы рецепта содержат этот конкретный id."""
    out = []
    for (space, name) in list(recipe.ins) + list(recipe.outs):
        if name.startswith("#"):
            out.append(name[1:])
    return out


class Verifier:
    def __init__(self, graph, resources):
        self.g = graph
        self.res = resources
        self.recipes = graph.recipes

    def _mem(self, space, tag):
        mem = self.g.tag_members_resolved.get((space, tag))
        if mem is None:
            mem = self.res.members(space, tag)
        return mem

    # ---------- количества ----------
    def _qty(self, ridx, node, side):
        """Сколько этого ресурса рецепт тратит/выдаёт за 1 крафт.

        Суммируем ВСЕ записи, которые резолвятся в этот предмет: и прямой id, и теги,
        в которых он состоит. Иначе рецепт вида "8x #c:tiny_dusts/coal + 1x coal_tiny_dust"
        читается как 1 штука, и цикл через него получает фиктивный профит ×9.
        """
        rec = self.recipes[ridx]
        space, ident = self.g.key(node)
        total = None
        for (sp, name), q in side.items():
            if sp != space:
                continue
            if name.startswith("#"):
                if ident in self._mem(sp, name[1:]):
                    total = q if total is None else total + q
            elif name == ident:
                total = q if total is None else total + q
        return total

    def qty_in(self, ridx, node):
        return self._qty(ridx, node, self.recipes[ridx].ins)

    def qty_out(self, ridx, node):
        return self._qty(ridx, node, self.recipes[ridx].outs)

    # ---------- конкретизация рецепта ----------
    def _concrete(self, ridx, chosen, flags):
        rec = self.recipes[ridx]
        ins = {}
        for (space, ident), q in rec.ins.items():
            if ident.startswith("#"):
                tag = ident[1:]
                member = chosen.get(tag)
                if member is None:
                    member = self.res.primary(space, tag)
                    flags.add("tag_primary")
                if member is None:
                    flags.add("unresolved_tag")
                    continue
                key = (space, member)
            else:
                key = (space, ident)
            ins[key] = ins.get(key, Fraction(0)) + q
        outs = {}
        for (space, ident), q in rec.outs.items():
            if ident.startswith("#"):
                tag = ident[1:]
                member = chosen.get(tag)
                if member is None:
                    member = self.res.primary(space, tag)
                    flags.add("tag_output_primary")
                if member is None:
                    flags.add("unresolved_tag")
                    continue
                key = (space, member)
            else:
                key = (space, ident)
            outs[key] = outs.get(key, Fraction(0)) + q
        return ins, outs

    # ---------- проверка ----------
    def verify(self, chain):
        """chain: [(u_node, v_node, ridx), ...] — замкнутый путь u0 -> ... -> u0.

        Возвращает dict:
            ok: bool (замкнуто и профит > 1)
            profit: Fraction
            net: {(space,id): Fraction}
            deficits: {(space,id): Fraction}
            flags: set
            steps: [(ridx, amount)]
        """
        flags = set()
        steps = []
        net = {}
        q = Fraction(1)
        cur_amount = None
        seed = chain[0][0]

        for idx, (u, v, ridx, _src_tag, _dst_tag) in enumerate(chain):
            if idx == 0:
                amount_in = q
            else:
                amount_in = cur_amount
            qi = self.qty_in(ridx, u)
            if not qi or qi <= 0:
                return {"ok": False, "reason": "no_input_qty", "flags": flags}
            n = amount_in / qi                     # сколько раз запускаем рецепт
            chosen = {}
            space, ident = self.g.key(u)
            for (sp, name) in self.recipes[ridx].ins:
                if name.startswith("#") and sp == space and ident in self._mem(sp, name[1:]):
                    chosen[name[1:]] = ident
            ins, outs = self._concrete(ridx, chosen, flags)
            for key, val in ins.items():
                net[key] = net.get(key, Fraction(0)) - val * n
            for key, val in outs.items():
                net[key] = net.get(key, Fraction(0)) + val * n

            qo = self.qty_out(ridx, v)
            if not qo or qo <= 0:
                return {"ok": False, "reason": "no_output_qty", "flags": flags}
            cur_amount = n * qo
            steps.append((ridx, n))

        # замыкание: последний шаг должен вернуться в seed
        if chain[-1][1] != seed:
            return {"ok": False, "reason": "not_closed", "flags": flags}

        seed_key = self.g.key(seed)
        profit = net.get(seed_key, Fraction(0)) + q
        net[seed_key] = profit - q          # чистый прирост seed
        deficits = {k: -v for k, v in net.items() if v < 0 and k != seed_key}
        # Санитарные флаги: таких чисел в честном цикле не бывает — обычно это неверно
        # прочитанный рецепт (бонусный выход, попавший во входы, и т.п.).
        if profit / q > 1000:
            flags.add("huge_profit")
        if deficits and max(deficits.values()) > 100000:
            flags.add("huge_deficit")
        return {
            "ok": (not deficits) and profit > q,
            "profit": profit / q,
            "net": net,
            "deficits": deficits,
            "flags": flags,
            "steps": steps,
            "seed": seed_key,
        }