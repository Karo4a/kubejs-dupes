"""Отчёты: markdown для чтения, json для машинной обработки.

Стиль шага (согласован): таблица «# | Вход | → | Выход | Машина (тип рецепта) | Крафтов».
Только сырые данные экспорта: полные id ресурсов, тип рецепта как есть, тег в скобках
рядом с конкретным предметом, который реально течёт по циклу. Никаких переводов.
"""
from __future__ import annotations

import json
from fractions import Fraction

from .graph import node_quantities


def _name(key):
    space, ident = key
    prefix = {"item": "", "fluid": "fluid:", "chemical": "gas:"}.get(space, f"{space}:")
    return prefix + ident


def _fmt_qty(q):
    q = Fraction(q)
    if q.denominator == 1:
        return str(q.numerator)
    return f"{float(q):.4g}"


def _fmt_ratio(q):
    return f"×{float(q):.4g}"


def _batch(steps, cap=1440):
    """Наименьшее число прогонов цикла, при котором все крафты целые."""
    from math import gcd
    den = 1
    for _ridx, amount in steps:
        d = Fraction(amount).denominator
        den = den * d // gcd(den, d)
        if den > cap:
            return 1
    return den


def _members(resources, space, tag):
    return resources.members(space, tag)


def _alt_recipes(graph, recipes, needed):
    """{(u,v): ratio} -> {pair: [ridx]} рецепты, дающие ту же пару с тем же коэффициентом."""
    from . import graph as graph_mod

    out = {pair: [] for pair in needed}
    for ridx, rec in enumerate(recipes):
        ins, outs = graph_mod.node_quantities(graph, rec)
        if not ins or not outs:
            continue
        for u, qi in ins.items():
            if not qi:
                continue
            for v, qo in outs.items():
                pair = (u, v)
                if pair not in needed:
                    continue
                if Fraction(qo) / Fraction(qi) == needed[pair]:
                    out[pair].append(ridx)
    return out


def _needed_ratios(graph, recipes, items):
    """Пары (u,v) из шагов с их коэффициентом — чтобы потом искать альтернативные рецепты."""
    needed = {}
    for res in items:
        for u, v, ridx, _st, _dt in res["chain"]:
            if (u, v) in needed:
                continue
            ins, outs = node_quantities(graph, recipes[ridx])
            qi, qo = ins.get(u), outs.get(v)
            if qi and qo:
                needed[(u, v)] = Fraction(qo) / Fraction(qi)
    return needed


def _cycle_table(graph, recipes, resources, res):
    """Таблица шагов цикла. Возвращает (lines, catalysts)."""
    from .verify import Verifier

    ver = Verifier(graph, resources)
    scale = _batch(res["steps"])
    chain_nodes = set()
    for step in res["chain"]:
        chain_nodes.add(graph.key(step[0]))
        chain_nodes.add(graph.key(step[1]))

    lines = ["| # | Вход | → | Выход | Машина (тип рецепта) | Крафтов |",
             "|---|------|---|-------|----------------------|---------|"]
    catalysts = []

    for i, (step, (ridx, amount)) in enumerate(zip(res["chain"], res["steps"]), 1):
        u, v, _r, _st, _dt = step
        rec = recipes[ridx]
        crafts = Fraction(amount) * scale
        u_space, u_ident = graph.key(u)

        # какой член тега реально течёт в этом шаге
        chosen = {}
        for (sp, name) in rec.ins:
            if name.startswith("#") and sp == u_space and u_ident in _members(resources, sp, name[1:]):
                chosen[name[1:]] = u_ident

        ins, outs = ver._concrete(ridx, chosen, set())

        # конкретный предмет -> тег, через который он пришёл
        tag_of = {}
        for (sp, name) in list(rec.ins) + list(rec.outs):
            if not name.startswith("#"):
                continue
            member = chosen.get(name[1:])
            if member is None:
                member = resources.primary(sp, name[1:])
            if member is not None:
                tag_of.setdefault((sp, member), name)

        def cell(items):
            parts = []
            for key, q in items.items():
                qty = Fraction(q) * crafts
                label = _name(key)
                if key in chain_nodes:
                    label = f"**{label}**"
                tag = tag_of.get(key)
                tag_note = f" *(тег {tag})*" if tag else ""
                parts.append(f"{_fmt_qty(qty)}× {label}{tag_note}")
            return " + ".join(parts) if parts else "—"

        lines.append(f"| {i} | {cell(ins)} | → | {cell(outs)} "
                     f"| `{rec.rtype}` | {_fmt_qty(crafts)} |")
        for cat in sorted(rec.catalysts):
            label = f"{_name(cat)}"
            catalysts.append(label)

    return lines, catalysts


def write_markdown(path, closed, open_, stats, graph, resources, recipes, top=None,
                   closed_folded=(), open_folded=(), pack=None):
    lines = []
    title = "# Циклические рецепты с дюпом"
    if pack:
        title += f" — {pack}"
    lines.append(title + "\n")
    lines.append(f"Рецептов разобрано: **{stats['recipes_used']}** из {stats['files']} файлов; "
                 f"ресурсов в графе: {stats['nodes']}; дуг: {stats['arcs']}; "
                 f"SCC>1: {stats['scc_big']}.\n")
    folded_note = ""
    if closed_folded:
        kinds = {}
        for f in closed_folded:
            kinds[f.get("kind", "wrap")] = kinds.get(f.get("kind", "wrap"), 0) + 1
        parts = []
        if kinds.get("wrap"):
            parts.append(f"{kinds['wrap']} с обвязкой 1:1")
        if kinds.get("unroll"):
            parts.append(f"{kinds['unroll']} с развёрнутым шагом")
        if kinds.get("glue"):
            parts.append(f"{kinds['glue']} склеек двух дюпов")
        folded_note = (f" Свёрнуто ещё {len(closed_folded)} вариантов того же дюпа "
                       f"({', '.join(parts)}) — см. раздел «Свёрнутые дубли».")
    lines.append(f"Найдено замкнутых циклов с профитом: **{len(closed)}**.{folded_note} "
                 f"Открытых (нужен внешний ресурс): {len(open_)}.\n")
    lines.append("Как читать: **жирным** — ресурсы, участвующие в цикле; обычным — то, что "
                 "приходит извне. `Машина` — сырой `type` рецепта из экспорта. `Крафтов` — "
                 "сколько раз запустить рецепт за один прогон цикла (партия подобрана так, "
                 "чтобы все крафты были целыми). `(тег …)` — рецепт принимает любой предмет "
                 "этого тега, в цикле используется указанный.\n")

    core_no = {id(it): i for i, it in enumerate(closed, 1)}
    alts = {}
    if closed or closed_folded:
        needed = _needed_ratios(graph, recipes,
                                list(closed) + [f["item"] for f in closed_folded])
        alts = _alt_recipes(graph, recipes, needed) if needed else {}

    if closed:
        lines.append("## Сводка\n")
        lines.append("| # | профит | ресурс | шагов | флаги |")
        lines.append("|---|--------|--------|-------|-------|")
        detailed = closed if top is None else closed[:top]
        for i, res in enumerate(closed, 1):
            flags = ", ".join(sorted(res.get("flags", ()))) or "—"
            lines.append(f"| {i} | {_fmt_ratio(res['profit'])} | {_name(res['seed'])} "
                         f"| {len(res['chain'])} | {flags} |")
        lines.append("")

        lines.append("## Подробно\n")
        for i, res in enumerate(detailed, 1):
            lines.append(f"### {i}. {_fmt_ratio(res['profit'])} — {_name(res['seed'])}\n")
            table, cats = _cycle_table(graph, recipes, resources, res)
            lines.extend(table)
            lines.append("")
            if cats:
                uniq = sorted(set(cats))
                lines.append(f"не тратится: {', '.join(uniq)}\n")
            lines.append("рецепты: " + ", ".join(f"`{recipes[step[2]].rid}`" for step in res["chain"]) + "\n")
            alt_parts = []
            for i, (u, v, ridx, _st, _dt) in enumerate(res["chain"], 1):
                others = sorted(r for r in alts.get((u, v), []) if r != ridx)
                if not others:
                    continue
                shown_alts = ", ".join(f"`{recipes[r].rid}`" for r in others[:4])
                if len(others) > 4:
                    shown_alts += f" и ещё {len(others) - 4}"
                alt_parts.append(f"шаг {i} — {shown_alts}")
            if alt_parts:
                lines.append("альтернативные рецепты (тот же коэффициент): " + "; ".join(alt_parts) + "\n")
            scale = Fraction(_batch(res["steps"]))
            extra_ins = [f"{_name(k)} {_fmt_qty(Fraction(v) * scale)}" for k, v in res["net"].items()
                         if v < 0 and k != res["seed"]]
            extra_outs = [f"{_name(k)} {_fmt_qty(Fraction(v) * scale)}" for k, v in res["net"].items()
                          if v > 0 and k != res["seed"]]
            if extra_ins:
                lines.append(f"доп. расход вне цикла: {', '.join(extra_ins)}\n")
            if extra_outs:
                lines.append(f"бонусные излишки: {', '.join(extra_outs)}\n")

    if closed_folded:
        lines.append("## Свёрнутые дубли (то же ядро, лишние шаги)\n")
        lines.append("Отдельного дюпа эти циклы не дают: обвязка 1:1 переводит ресурс сам в себя "
                     "с коэффициентом 1; развёрнутый шаг — это путь между теми же узлами с тем же "
                     "произведением коэффициентов, что и шаг ядра; склейка — два других дюпа "
                     "подряд, профит перемножается.\n")
        lines.append("| # | причина | профит | ресурс | ядро | лишние рецепты |")
        lines.append("|---|---------|--------|--------|------|----------------|")
        for i, f in enumerate(closed_folded, 1):
            res, core = f["item"], f["core"]
            kind = f.get("kind", "wrap")
            if kind == "glue" and f.get("glue") is not None:
                why = (f"склейка #{core_no.get(id(core), '?')} ⊗ "
                       f"#{core_no.get(id(f['glue']), '?')}")
            elif kind == "unroll":
                why = "шаг развёрнут"
            else:
                why = "обвязка 1:1"
            extra = ", ".join(f"`{recipes[r].rid}`" for r in f["extra_ridx"])
            lines.append(f"| {i} | {why} | {_fmt_ratio(res['profit'])} | {_name(res['seed'])} "
                         f"| #{core_no.get(id(core), '?')} (шагов: {len(core['chain'])}) | {extra} |")
        lines.append("")
        folded_alts = []
        for i, f in enumerate(closed_folded, 1):
            extra_set = set(f["extra_ridx"])
            parts = []
            for u, v, ridx, _st, _dt in f["item"]["chain"]:
                if ridx not in extra_set:
                    continue
                others = sorted(r for r in alts.get((u, v), []) if r != ridx)
                if not others:
                    continue
                parts.append(f"вместо `{recipes[ridx].rid}` — " +
                             ", ".join(f"`{recipes[r].rid}`" for r in others[:4]))
            if parts:
                folded_alts.append(f"- свёрнутый #{i}: " + "; ".join(parts))
        if folded_alts:
            lines.append("Альтернативы в обвязке (та же пара, тот же коэффициент):\n")
            lines.extend(folded_alts)
            lines.append("")

    if open_:
        lines.append("## Открытые циклы (не хватает внешнего ресурса)\n")
        lines.append("Такие циклы дают профит, но требуют ресурса извне. Если он у тебя "
                     "производится (см. подсказку после «←»), цикл превращается в реальный дюп.\n")
        lines.append("⚠ — подозрительный масштаб (профит > 1000 или дефицит > 100 000): почти всегда "
                     "признак неверно прочитанного рецепта (бонусный выход, попавший во входы).\n")
        lines.append("| # | профит | стартовый ресурс | шагов | не хватает |")
        lines.append("|---|--------|------------------|-------|------------|")
        for i, res in enumerate(open_[:60], 1):
            parts = []
            for k, v in sorted(res.get("deficits", {}).items(), key=lambda kv: -kv[1])[:3]:
                node = graph.index_of.get(k)
                hint = ""
                if node is not None and graph.rev[node]:
                    hint = f" ← {recipes[graph.rev[node][0][1]].rid}"
                parts.append(f"{_name(k)} {_fmt_qty(v)}{hint}")
            warn = "⚠ " if ({"huge_profit", "huge_deficit"} & set(res.get("flags", ()))) else ""
            lines.append(f"| {i} | {warn}{_fmt_ratio(res['profit'])} | {_name(res['seed'])} "
                         f"| {len(res['chain'])} | {'; '.join(parts)} |")
        lines.append("")
        if open_folded:
            lines.append(f"Свёрнуто ещё {len(open_folded)} открытых циклов с обвязкой 1:1 — "
                         f"список в `open_cycles.md`.\n")

    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))


def write_open(path, open_, stats, graph, recipes, resources, top=40, open_folded=()):
    """Отдельный файл: циклы, которым не хватает внешнего ресурса."""
    lines = ["# Открытые циклы (нужен внешний ресурс)\n",
             f"Всего: {len(open_)}. Первые {min(top, len(open_))} — подробно.",
             f"Свёрнуто вариантов с обвязкой 1:1 (то же ядро): {len(open_folded)}.\n",
             "Такие циклы дают профит, но требуют ресурса извне. Если он у тебя производится "
             "(см. подсказку после «←»), цикл превращается в реальный дюп.\n",
             "| # | профит | стартовый ресурс | шагов | не хватает |",
             "|---|--------|------------------|-------|------------|"]
    for i, res in enumerate(open_[:top], 1):
        parts = []
        for k, v in sorted(res.get("deficits", {}).items(), key=lambda kv: -kv[1])[:3]:
            node = graph.index_of.get(k)
            hint = ""
            if node is not None and graph.rev[node]:
                hint = f" ← {recipes[graph.rev[node][0][1]].rid}"
            parts.append(f"{_name(k)} {_fmt_qty(v)}{hint}")
        lines.append(f"| {i} | {_fmt_ratio(res['profit'])} | {_name(res['seed'])} "
                     f"| {len(res['chain'])} | {'; '.join(parts)} |")
    lines.append("")

    for i, res in enumerate(open_[:top], 1):
        lines.append(f"### {i}. {_fmt_ratio(res['profit'])} — {_name(res['seed'])}\n")
        table, cats = _cycle_table(graph, recipes, resources, res)
        lines.extend(table)
        lines.append("")
        if cats:
            lines.append(f"не тратится: {', '.join(sorted(set(cats)))}\n")
        miss = ", ".join(f"{_name(k)} {_fmt_qty(v)}" for k, v in res.get("deficits", {}).items())
        lines.append(f"не хватает: {miss}\n")

    if open_folded:
        lines.append("## Свёрнутые открытые циклы (то же ядро + обвязка 1:1)\n")
        lines.append("| # | профит | стартовый ресурс | ядро | лишние рецепты |")
        lines.append("|---|--------|------------------|------|----------------|")
        for i, f in enumerate(open_folded, 1):
            res, core = f["item"], f["core"]
            extra = ", ".join(f"`{recipes[r].rid}`" for r in f["extra_ridx"])
            lines.append(f"| {i} | {_fmt_ratio(res['profit'])} | {_name(res['seed'])} "
                         f"| {_fmt_ratio(core['profit'])} {_name(core['seed'])} "
                         f"(шагов: {len(core['chain'])}) | {extra} |")
        lines.append("")

    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))


def _dump_folded(f, cores, recipes, graph=None):
    """Свёрнутый дубль для json: 1-based номер ядра в списке cores."""
    no = next((i for i, it in enumerate(cores, 1) if it is f["core"]), None)
    out = {
        "seed": list(f["item"]["seed"]),
        "profit": float(f["item"]["profit"]),
        "core": no,
        "kind": f.get("kind", "wrap"),
        "extra_recipes": [recipes[r].rid for r in f["extra_ridx"]],
    }
    glue = f.get("glue")
    if glue is not None:
        out["glue"] = next((i for i, it in enumerate(cores, 1) if it is glue), None)
    if graph is not None:      # шаги варианта: по ним видно, какой шаг развёрнут
        out["steps"] = [
            {"recipe": recipes[ridx].rid,
             "bitem": list(graph.key(step[0])),
             "eitem": list(graph.key(step[1]))}
            for step, (ridx, _amount) in zip(f["item"]["chain"], f["item"]["steps"])
        ]
    return out


def write_json(path, closed, open_, stats, graph, recipes, closed_folded=(), open_folded=()):
    def dump_result(res):
        return {
            "seed": list(res["seed"]),
            "profit": float(res["profit"]),
            "flags": sorted(res.get("flags", ())),
            "steps": [
                {
                    "recipe": recipes[ridx].rid,
                    "type": recipes[ridx].rtype,
                    "file": recipes[ridx].file,
                    "bitem": list(graph.key(step[0])),
                    "eitem": list(graph.key(step[1])),
                    "crafts": str(amount),
                }
                for step, (ridx, amount) in zip(res["chain"], res["steps"])
            ],
            "net": {f"{k[0]}:{k[1]}": str(v) for k, v in res["net"].items()},
            # batch: во сколько прогонов цикла разложена таблица в markdown;
            # net дан на 1 прогон, умножить на batch, чтобы получить числа из таблицы
            "batch": str(_batch(res["steps"])),
            "deficits": {f"{k[0]}:{k[1]}": str(v) for k, v in res.get("deficits", {}).items()},
        }

    payload = {
        "stats": stats,
        "closed": [dump_result(r) for r in closed],
        "open": [dump_result(r) for r in open_],
        # свёрнутые варианты того же дюпа (набор рецептов ядра строго вложен, профит тот же)
        "redundant": {
            "closed": [_dump_folded(f, closed, recipes, graph) for f in closed_folded],
            "open": [_dump_folded(f, open_, recipes) for f in open_folded],
        },
    }
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)


def write_coverage(path, stats):
    lines = ["# Покрытие парсера\n",
             f"Всего файлов: {stats['files']}",
             f"Использовано рецептов: {stats['recipes_used']}",
             f"Битый JSON: {stats['bad_json']}",
             f"Исключено по errors.log: {stats['skipped_error_log']}",
             f"Без входов: {stats['no_inputs']}, без выходов: {stats['no_outputs']}",
             "",
             "| тип | всего | разобрано | без входов | без выходов |",
             "|-----|-------|-----------|------------|-------------|"]
    rows = sorted(stats["by_type"].items(), key=lambda kv: -kv[1]["total"])
    for rtype, c in rows:
        lines.append(f"| {rtype} | {c['total']} | {c['ok']} | {c['no_in']} | {c['no_out']} |")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))