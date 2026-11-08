"""Поиск циклических конверсий: перебор простых циклов ограниченной длины + точная проверка."""
from __future__ import annotations

import time
from fractions import Fraction
from functools import lru_cache

from .verify import Verifier


class Candidate:
    __slots__ = ("ratio", "chain")

    def __init__(self, ratio, chain):
        self.ratio = ratio
        self.chain = chain


def _filtered_adjacency(g):
    """Рёбра только внутри своей SCC."""
    scc = g.scc_id
    fadj = []
    for node, edges in enumerate(g.adj):
        sid = scc[node]
        keep = [e for e in edges if e.dst < len(scc) and scc[e.dst] == sid]
        fadj.append(keep)
    return fadj


def find_candidates(g, max_len: int = 4, min_profit: float = 1.02, verbose: bool = True):
    """Все простые циклы длины <= max_len с произведением коэффициентов >= min_profit.

    Канонизация: стартовый узел цикла имеет минимальный индекс, поэтому каждый цикл
    перечисляется ровно один раз.
    """
    fadj = _filtered_adjacency(g)
    scc = g.scc_id
    size = g.scc_size
    cands = []
    t0 = time.time()
    expansions = 0

    # быстрая проверка замыкания на последнем шаге: dst -> Edge
    closemap = [{e.dst: e for e in edges} for edges in fadj]

    for node, edge in g.self_loops:
        if edge.ratio >= min_profit:
            cands.append(Candidate(edge.ratio,
                                   ((node, node, edge.ridx, edge.src_tag, edge.dst_tag),)))

    nodes = [n for n, edges in enumerate(fadj) if edges and size.get(scc[n], 0) > 1]
    last = max_len - 1

    for s in nodes:
        path = []
        visited = {s}
        close = closemap
        fadj_local = fadj

        def dfs(cur, prod, depth):
            nonlocal expansions
            if depth == last:
                e = close[cur].get(s)
                if e is not None:
                    r = prod * e.ratio
                    if r >= min_profit:
                        cands.append(Candidate(
                            r, tuple(path + [(cur, s, e.ridx, e.src_tag, e.dst_tag)])))
                return
            for e in fadj_local[cur]:
                d = e.dst
                expansions += 1
                if d == s:
                    r = prod * e.ratio
                    if r >= min_profit:
                        cands.append(Candidate(
                            r, tuple(path + [(cur, d, e.ridx, e.src_tag, e.dst_tag)])))
                elif d > s and d not in visited:
                    visited.add(d)
                    path.append((cur, d, e.ridx, e.src_tag, e.dst_tag))
                    dfs(d, prod * e.ratio, depth + 1)
                    path.pop()
                    visited.discard(d)

        dfs(s, 1.0, 0)

    if verbose:
        print(f"[search] циклов-кандидатов: {len(cands)}, раскрытий: {expansions}, "
              f"{time.time() - t0:.1f} c")
    return cands


def _rotate(chain, k):
    return chain[k:] + chain[:k]


def _chain_hops(item, ver: Verifier):
    """Шаги цикла с коэффициентами: [(u, v, ridx, qty_out/qty_in)] или None."""
    hops = []
    for u, v, ridx, _src_tag, _dst_tag in item["chain"]:
        qi = ver.qty_in(ridx, u)
        qo = ver.qty_out(ridx, v)
        if not qi or not qo:
            return None
        hops.append((u, v, ridx, qo / qi))
    return hops


def _prod(hops):
    p = Fraction(1)
    for h in hops:
        p *= h[3]
    return p


def _unroll(V, A):
    """Разбирается ли V на шаги A, где шаг A развёрнут в путь между теми же узлами
    с тем же коэффициентом (или перед шагом вставлена петля 1:1).

    -> (лишних шагов, [(начало, длина, вид)]) или None. Вид: same/unroll/loop.
    """
    n, m = len(V), len(A)

    @lru_cache(maxsize=None)
    def go(i, j):
        if j == m:
            if i == n:
                return (0, ())
            p = Fraction(1)                       # хвостовые петли 1:1
            for t in range(1, n - i + 1):
                p *= V[i + t - 1][3]
                if V[i + t - 1][1] == V[i][0] and p == 1:
                    sub = go(i + t, j)
                    if sub is not None:
                        return (sub[0] + t, ((i, t, "loop"),) + sub[1])
            return None
        best = None
        if V[i][0] == A[j][0] and V[i][1] == A[j][1]:
            sub = go(i + 1, j + 1)
            if sub is not None:
                best = (sub[0], ((i, 1, "same"),) + sub[1])
        p = Fraction(1)
        for t in range(1, n - i + 1):
            p *= V[i + t - 1][3]
            if (t >= 2 and V[i][0] == A[j][0] and V[i + t - 1][1] == A[j][1]
                    and p == A[j][3]):
                sub = go(i + t, j + 1)
                if sub is not None:
                    cost = sub[0] + t - 1
                    if best is None or cost < best[0]:
                        best = (cost, ((i, t, "unroll"),) + sub[1])
            if V[i + t - 1][1] == V[i][0] and p == 1:
                sub = go(i + t, j)
                if sub is not None:
                    cost = sub[0] + t
                    if best is None or cost < best[0]:
                        best = (cost, ((i, t, "loop"),) + sub[1])
        return best

    res = go(0, 0)
    return None if res is None else res


def collapse_redundant(items, ver=None, glue=True):
    """Свернуть «удлинённые» варианты одного и того же дюпа.

    1. **Обвязка**: набор рецептов короткого цикла строго вложен в длинный при точно равном
       профите (unpacker/packer, 9 tiny ↔ 1 dust, плачущий обсидиан по тегу и т.п.).
    2. **Развёрнутый шаг**: сид и профит те же, а шаг ядра заменён путём между теми же узлами
       с тем же произведением коэффициентов (`plates`: ingot→plate→dust вместо ingot→dust).
       Наборы рецептов при этом не вложены, поэтому пункт 1 такое не ловит. Нужен `ver`.
    3. **Склейка двух дюпов**: набор длинного = набор A ∪ набор B и профит = профит A × профит B.

    -> (ядра в исходном порядке, [{"item", "core", "kind", "extra_ridx", ...}])
    """
    sets = [frozenset(step[2] for step in it["chain"]) for it in items]
    index = {}
    for i, s in enumerate(sets):
        for r in s:
            index.setdefault(r, []).append(i)

    parent = {}     # id(item) -> ядро
    folded = []

    def fold(item, core, kind, **extra):
        parent[id(item)] = core
        folded.append({"item": item, "core": core, "kind": kind,
                       "extra_ridx": sorted(set(step[2] for step in item["chain"]) -
                                            set(step[2] for step in core["chain"])),
                       **extra})

    # 1) обвязка: набор ядра строго вложен
    for j, s in enumerate(sets):
        cand = set()
        for r in s:
            cand.update(index[r])
        best = None
        for i in cand:
            if i == j or len(sets[i]) >= len(s):
                continue
            if not sets[i] < s:
                continue
            if items[i]["profit"] != items[j]["profit"]:
                continue
            if best is None or (len(sets[i]), i) < (len(sets[best]), best):
                best = i
        if best is not None:
            fold(items[j], items[best], "wrap")

    # 2) развёрнутый шаг: тот же сид, тот же профит
    if ver is not None:
        live = [it for it in items if id(it) not in parent]
        hops = {}
        for it in live:
            h = _chain_hops(it, ver)
            if h is not None:
                hops[id(it)] = h
        groups = {}
        for pos, it in enumerate(live):
            groups.setdefault((it["seed"], it["profit"]), []).append(pos)
        for idxs in groups.values():
            if len(idxs) < 2:
                continue
            kept = []
            for pos in sorted(idxs, key=lambda p: (len(live[p]["chain"]), p)):
                it = live[pos]
                V = hops.get(id(it))
                hit = None
                if V is not None:
                    for kpos in kept:            # kept — по возрастанию длины
                        base = live[kpos]
                        A = hops.get(id(base))
                        if A is None or len(V) <= len(A):
                            continue
                        res = _unroll(V, A)
                        if res is not None and any(kind != "same" for _, _, kind in res[1]):
                            hit = (base, res)
                            break
                if hit is None:
                    kept.append(pos)
                else:
                    fold(it, hit[0], "unroll", ops=hit[1][1])

    # 3) склейка двух дюпов (только для замкнутых: нужен точный профит-множитель)
    if glue and ver is not None:
        live = [it for it in items if id(it) not in parent]
        live_sets = {id(it): frozenset(step[2] for step in it["chain"]) for it in live}
        by_set = {}
        for it in live:
            by_set.setdefault(live_sets[id(it)], it)
        idx2 = {}
        for it in live:
            for r in live_sets[id(it)]:
                idx2.setdefault(r, []).append(it)
        for it in live:
            s = live_sets[id(it)]
            if len(s) < 3 or id(it) in parent:
                continue
            cands, seen_ids = [], set()
            for r in s:
                for a in idx2[r]:
                    if id(a) not in seen_ids:
                        seen_ids.add(id(a))
                        cands.append(a)
            found = None
            for a in cands:
                sa = live_sets[id(a)]
                if a is it or not sa or len(sa) >= len(s) or not sa < s:
                    continue
                rest = by_set.get(s - sa)
                if rest is None or rest is it or rest is a:
                    continue
                if a["profit"] * rest["profit"] != it["profit"]:
                    continue
                found = (a, rest)
                break
            if found is not None:
                parent[id(it)] = found
                folded.append({"item": it, "core": found[0], "glue": found[1], "kind": "glue",
                               "extra_ridx": sorted(s - live_sets[id(found[0])])})

    def root(it):
        seen = set()
        while id(it) in parent and id(it) not in seen:
            seen.add(id(it))
            nxt = parent[id(it)]
            if isinstance(nxt, tuple):
                nxt = nxt[0]
            it = nxt
        return it

    for f in folded:                 # ядро само могло быть свёрнуто дальше
        f["core"] = root(f["core"])
        if "glue" in f:
            f["glue"] = root(f["glue"])
    cores = [it for it in items if id(it) not in parent]
    return cores, folded


def verify_candidates(g, resources, cands, min_profit: float = 1.02,
                      max_verify: int = 50000, verbose: bool = True):
    """-> (closed, open_)"""
    ver = Verifier(g, resources)
    closed, open_ = [], []
    seen = set()
    t0 = time.time()
    verified = 0

    cands.sort(key=lambda c: c.ratio, reverse=True)
    for cand in cands[:max_verify]:
        key = frozenset(step[2] for step in cand.chain)
        if key in seen:
            continue
        seen.add(key)
        verified += 1
        n = len(cand.chain)
        best = None
        best_open = None

        def score(res):
            key = res["seed"]
            node = g.index_of.get(key)
            indeg = len(g.rev[node]) if node is not None else 0
            return (res["profit"], key[0] == "item", indeg)

        # профит замкнутого цикла одинаков с любой точки старта,
        # но точку старта надо перебрать: незамкнутый цикл (open) зависит от неё
        for k in range(n):
            chain = _rotate(cand.chain, k)
            res = ver.verify(chain)
            profit = res.get("profit")
            if profit is None or profit < Fraction(str(min_profit)):
                continue
            res["chain"] = chain
            if res.get("ok"):
                if best is None or score(res) > score(best):
                    best = res
            elif "deficits" in res and (best_open is None or score(res) > score(best_open)):
                best_open = res
        if best is not None:
            best["ratio_hint"] = cand.ratio
            flags = set(best.get("flags", set()))
            for step in best["chain"]:
                if step[3] or step[4]:
                    flags.add("via_tag")
            best["flags"] = flags
            closed.append(best)
        elif best_open is not None:
            best_open["ratio_hint"] = cand.ratio
            open_.append(best_open)

    if verbose:
        print(f"[verify] проверено {verified} кандидатов: дюпов {len(closed)}, "
              f"открытых {len(open_)}, {time.time() - t0:.1f} c")
    return closed, open_