"""LP-проверка «дюпов нет» (--prove).

Идея (no-arbitrage / теорема Фаркаса): множество дюпов непусто тогда и только тогда,
когда НЕ существует строго положительного вектора цен y, при котором ни один рецепт
не прибылен:

    exists n >= 0, A n >= 0, A n != 0   <=>   not exists y > 0 : A^T y <= 0

Поэтому: ищем y >= 1, для всех рецептов sum(выходы*y) - sum(входы*y) <= 0.
  * LP нашёл y  -> дюпов нет (в релаксированной модели);
  * LP не нашёл -> дюпы существуют (и наш поиск должен что-то найти).

Теги моделируются узлом-ценой: y_tag связан с членами равенством (1:1 в обе стороны).
Это РЕЛАКСАЦИЯ (разрешает переклейку членов), поэтому ответ «дюпов нет» остаётся
верным и для точной модели, а ответ «дюпы есть» — консервативный.
"""
from __future__ import annotations

import time


def run(graph, resources, recipes, verbose: bool = True):
    try:
        import numpy as np
        from scipy.optimize import linprog
        from scipy.sparse import coo_matrix
    except Exception as exc:  # pragma: no cover
        return {"available": False, "error": f"scipy/numpy недоступны: {exc}"}

    t0 = time.time()
    index = {}

    def var(key):
        idx = index.get(key)
        if idx is None:
            idx = len(index)
            index[key] = idx
        return idx

    rows, cols, vals = [], [], []
    row = 0
    for rec in recipes:
        touched = False
        for (space, ident), q in rec.ins.items():
            j = var(("tag" if ident.startswith("#") else "res", space, ident))
            rows.append(row); cols.append(j); vals.append(-float(q)); touched = True
        for (space, ident), q in rec.outs.items():
            j = var(("tag" if ident.startswith("#") else "res", space, ident))
            rows.append(row); cols.append(j); vals.append(float(q)); touched = True
        if touched:
            row += 1

    # tag <-> member: цены равны (релаксация)
    tag_pairs = 0
    for (kind, space, ident), tag_idx in list(index.items()):
        if kind != "tag":
            continue
        for member in resources.members(space, ident[1:]):
            j = var(("res", space, member))
            rows += [row, row + 1]; cols += [j, tag_idx]; vals += [1.0, 1.0]
            rows += [row, row + 1]; cols += [tag_idx, j]; vals += [-1.0, -1.0]
            row += 2
            tag_pairs += 1

    n_vars = len(index)
    a = coo_matrix((vals, (rows, cols)), shape=(row, n_vars)).tocsr()
    if verbose:
        print(f"[prove] LP: {row} ограничений, {n_vars} переменных, {a.nnz} ненулевых "
              f"(связок тег-член {tag_pairs}), построено за {time.time() - t0:.1f} c")

    res = linprog(c=np.zeros(n_vars), A_ub=a, b_ub=np.zeros(row),
                  bounds=[(1, None)] * n_vars, method="highs")
    elapsed = time.time() - t0
    if res.status == 0:
        return {"available": True, "no_dupes": True, "seconds": round(elapsed, 1),
                "vars": n_vars, "rows": row}
    if res.status == 2:
        return {"available": True, "no_dupes": False, "seconds": round(elapsed, 1),
                "vars": n_vars, "rows": row}
    return {"available": True, "no_dupes": None, "status": res.status,
            "message": res.message, "seconds": round(elapsed, 1)}