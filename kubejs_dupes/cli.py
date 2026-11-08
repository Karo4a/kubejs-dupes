"""CLI: python -m kubejs_dupes --export <путь> --out <папка>"""
from __future__ import annotations

import argparse
import os
import sys
import time

from . import graph as graph_mod
from . import loader, prove, report, search
from .verify import Verifier


def build_parser():
    p = argparse.ArgumentParser(description="Поиск циклических рецептов-дюпов в KubeJS-экспорте")
    p.add_argument("--export", required=True,
                   help="папка экспорта KubeJS (local/kubejs/export)")
    p.add_argument("--out", default=None, help="папка для отчётов (по умолчанию <repo>/out)")
    p.add_argument("--pack", default=None,
                   help="имя сборки для заголовка отчёта (необязательно)")
    p.add_argument("--depth", type=int, default=4, help="максимум шагов в цикле (2..6)")
    p.add_argument("--min-profit", type=float, default=1.02, help="минимальный множитель")
    p.add_argument("--max-verify", type=int, default=50000, help="сколько кандидатов проверять точно")
    p.add_argument("--tags", choices=["node", "primary"], default="node",
                   help="node: любой член тега; primary: только канонический член")
    p.add_argument("--chance", choices=["expected", "strict"], default="expected",
                   help="как учитывать шанс выхода")
    p.add_argument("--max-edges", type=int, default=0, help="лимит исходящих рёбер на ресурс (0 = без лимита)")
    p.add_argument("--top", type=int, default=60, help="сколько дюпов расписать подробно (0 = все)")
    p.add_argument("--prove", action="store_true", help="LP-проверка «дюпов нет» (нужен scipy)")
    p.add_argument("--no-collapse", action="store_true",
                   help="не сворачивать варианты того же дюпа с обвязкой 1:1")
    p.add_argument("--no-cache", action="store_true", help="не использовать кэш нормализации")
    return p


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    parser = build_parser()
    args = parser.parse_args(argv)

    if not os.path.isdir(os.path.join(os.path.abspath(args.export), "recipes")):
        parser.error(f"в {args.export} нет папки recipes/ — это не папка экспорта KubeJS "
                     f"(нужна та, что появляется после /kubejs export debug)")

    out_dir = args.out or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out")
    os.makedirs(out_dir, exist_ok=True)
    t_start = time.time()

    resources, recipes, stats = loader.load(args.export, chance_mode=args.chance,
                                            use_cache=not args.no_cache,
                                            cache_dir=os.path.join(out_dir, "cache"))
    g = graph_mod.build(recipes, resources, max_edges_per_node=args.max_edges,
                        tag_mode=args.tags)

    stats["recipes_used"] = len(recipes)
    stats["nodes"] = len(g.key_of)
    stats["arcs"] = sum(len(e) for e in g.adj)
    stats["scc_big"] = sum(1 for n, c in g.scc_size.items() if n > 1)

    cands = search.find_candidates(g, max_len=args.depth, min_profit=args.min_profit)
    closed, open_ = search.verify_candidates(g, resources, cands, min_profit=args.min_profit,
                                             max_verify=args.max_verify)
    closed.sort(key=lambda r: r["profit"], reverse=True)
    open_.sort(key=lambda r: r["profit"], reverse=True)

    closed_folded, open_folded = [], []
    if not args.no_collapse:
        # Для замкнутых дополнительно сворачиваем «развёрнутый шаг» и склейку двух дюпов:
        # для этого нужны коэффициенты шагов, то есть Verifier поверх того же графа.
        closed, closed_folded = search.collapse_redundant(closed, ver=Verifier(g, resources))
        open_, open_folded = search.collapse_redundant(open_)
    stats["closed_folded"] = len(closed_folded)
    stats["open_folded"] = len(open_folded)
    stats["closed_folded_kinds"] = {}
    for f in closed_folded:
        k = f.get("kind", "wrap")
        stats["closed_folded_kinds"][k] = stats["closed_folded_kinds"].get(k, 0) + 1

    report.write_markdown(os.path.join(out_dir, "dupes.md"), closed, open_, stats, g,
                          resources, recipes, top=(args.top or None),
                          closed_folded=closed_folded, open_folded=open_folded,
                          pack=args.pack)
    report.write_json(os.path.join(out_dir, "dupes.json"), closed, open_, stats, g, recipes,
                      closed_folded=closed_folded, open_folded=open_folded)
    report.write_open(os.path.join(out_dir, "open_cycles.md"), open_, stats, g, recipes, resources,
                      open_folded=open_folded)
    report.write_coverage(os.path.join(out_dir, "coverage.md"), stats)

    if args.prove:
        result = prove.run(g, resources, recipes)
        print(f"[prove] {result}")
        with open(os.path.join(out_dir, "prove.md"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# LP-проверка (no-arbitrage)\n\n")
            if not result.get("available"):
                fh.write(f"Недоступно: {result.get('error')}\n")
            elif result.get("no_dupes") is True:
                fh.write("Существует строго положительный вектор цен, при котором ни один рецепт "
                         "не прибылен → **дюпов нет** (в релаксированной модели).\n")
            elif result.get("no_dupes") is False:
                fh.write("Строго положительного вектора цен не существует → **дюпы есть**.\n")
            else:
                fh.write(f"LP не сошёлся: {result}\n")

    print(f"\nИтог: дюпов {len(closed)}, открытых циклов {len(open_)}; "
          f"свёрнуто вариантов с обвязкой 1:1: {len(closed_folded)} / {len(open_folded)}; "
          f"всего {time.time() - t_start:.1f} c")
    print(f"Отчёты: {out_dir}")
    for i, res in enumerate(closed[:10], 1):
        print(f"  {i:2d}. ×{float(res['profit']):.3f}  {res['seed'][1]}  "
              f"({len(res['chain'])} шагов, {','.join(sorted(res.get('flags', ())))})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())