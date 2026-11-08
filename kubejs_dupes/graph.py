"""Построение графа ресурсов: разворачивание тегов, рёбра конверсий, SCC."""
from __future__ import annotations

import collections
import sys
from fractions import Fraction


class Edge:
    __slots__ = ("dst", "ratio", "ridx", "src_tag", "dst_tag")

    def __init__(self, dst, ratio, ridx, src_tag, dst_tag):
        self.dst = dst
        self.ratio = ratio
        self.ridx = ridx
        self.src_tag = src_tag
        self.dst_tag = dst_tag

    def __repr__(self):  # pragma: no cover
        return f"<Edge ->{self.dst} x{self.ratio:.4g} r{self.ridx}>"


class Graph:
    def __init__(self):
        self.key_of = []                 # int -> (space, id)
        self.index_of = {}               # (space, id) -> int
        self.adj = []                    # list[list[Edge]]
        self.rev = []                    # list[list[(src, ridx)]]
        self.self_loops = []             # [(node, Edge)]
        self.scc_id = []                 # int -> компонента
        self.scc_size = collections.Counter()
        self.recipes = []
        self.res = None
        self.collapsed = 0
        self.tag_members_resolved = {}

    def key(self, node: int):
        return self.key_of[node]

    # ---------- helpers ----------
    def node(self, key) -> int:
        idx = self.index_of.get(key)
        if idx is None:
            idx = len(self.key_of)
            self.index_of[key] = idx
            self.key_of.append(key)
            self.adj.append([])
            self.rev.append([])
        return idx

    def add_edge(self, src, dst, ratio, ridx, src_tag=False, dst_tag=False):
        if ratio <= 0:
            return
        self.adj[src].append(Edge(dst, float(ratio), ridx, src_tag, dst_tag))
        self.rev[dst].append((src, ridx))


def build(recipes, resources, max_edges_per_node: int = 0, tag_mode: str = "node",
          tag_member_limit: int = 32, verbose: bool = True) -> Graph:
    g = Graph()
    g.recipes = recipes
    g.res = resources

    # Разворачивание тегов. Крупные "категорийные" теги (minecraft:wooden_doors — 1169 членов,
    # c:tools — 976) не разворачиваем: они превращают граф в один сплошной SCC из 1:1 связей
    # и не дают профита. Для них берём канонического члена (это не влияет на поиск дюпов,
    # т.к. подстановка внутри таких тегов — 1:1).
    def members(space, tag):
        key = (space, tag)
        cached = g.tag_members_resolved.get(key)
        if cached is not None:
            return cached
        mem = resources.members(space, tag)
        if tag_mode == "primary" or len(mem) > tag_member_limit:
            one = resources.primary(space, tag)
            mem = frozenset([one]) if one else frozenset()
        g.tag_members_resolved[key] = mem
        return mem

    def expand(space, ident, qty, acc):
        """Копим количество по УЗЛУ, а не по ключу рецепта: рецепт может брать один и тот
        же предмет дважды — напрямую и через тег (8x #c:tiny_dusts/coal + 1x coal_tiny_dust).
        Иначе ребро получает 1/1 вместо 1/9 и рисует несуществующий дюп.
        """
        if ident.startswith("#"):
            flagged = True
            idents = sorted(members(space, ident[1:]))
        else:
            flagged = False
            idents = [ident]
        for m in idents:
            n = g.node((space, m))
            prev = acc.get(n)
            if prev is None:
                acc[n] = [qty, flagged]
            else:
                prev[0] += qty
                prev[1] = prev[1] or flagged

    # сначала заводим все узлы, чтобы индексы были стабильны
    for rec in recipes:
        for space, ident in list(rec.ins) + list(rec.outs):
            if ident.startswith("#"):
                for m in sorted(members(space, ident[1:])):
                    g.node((space, m))
            else:
                g.node((space, ident))

    best = {}          # (u, v) -> ((score), ratio, ridx, u_tag, v_tag)
    expanded_pairs = 0
    for ridx, rec in enumerate(recipes):
        ins_acc = {}
        for (space, ident), qty in rec.ins.items():
            expand(space, ident, Fraction(qty), ins_acc)
        outs_acc = {}
        for (space, ident), qty in rec.outs.items():
            expand(space, ident, Fraction(qty), outs_acc)
        expanded_pairs += len(ins_acc) * len(outs_acc)
        cheap = -len(rec.ins)          # при равном коэффициенте предпочитаем рецепт с меньшим числом входов
        for u, (qi, ut) in ins_acc.items():
            if qi <= 0:
                continue
            for v, (qo, vt) in outs_acc.items():
                ratio = Fraction(qo) / Fraction(qi)
                key = (u, v)
                prev = best.get(key)
                if prev is None or (ratio, cheap) > prev[0]:
                    best[key] = ((ratio, cheap), ratio, ridx, ut, vt)

    for (u, v), (_score, ratio, ridx, ut, vt) in best.items():
        if u == v:
            g.self_loops.append((u, Edge(v, ratio, ridx, ut, vt)))
        else:
            g.add_edge(u, v, ratio, ridx, ut, vt)

    # ограничение ветвления: оставляем лучшие по коэффициенту рёбра
    if max_edges_per_node:
        for node, edges in enumerate(g.adj):
            if len(edges) > max_edges_per_node:
                edges.sort(key=lambda e: e.ratio, reverse=True)
                del edges[max_edges_per_node:]

    if verbose:
        arcs = sum(len(e) for e in g.adj)
        print(f"[graph] узлов {len(g.key_of)}, дуг {arcs} (пар до коллапса {expanded_pairs}, "
              f"уникальных {len(best)}), self-loop {len(g.self_loops)}")

    _tarjan(g)
    if verbose:
        big = [n for n, c in g.scc_size.items() if n > 1]
        incyc = sum(c for n, c in g.scc_size.items() if n > 1)
        print(f"[graph] SCC>1: {len(big)}, ресурсов в циклах: {incyc}, крупнейшая: "
              f"{max(big) if big else 0}")
    return g


def node_quantities(g: Graph, rec):
    """({node: qty} входов, {node: qty} выходов) с той же трактовкой тегов, что в build().

    Нужна после построения графа: например, чтобы найти альтернативные рецепты той же пары
    ресурсов с тем же коэффициентом.
    """
    def side(d):
        acc = {}
        for (space, ident), qty in d.items():
            if ident.startswith("#"):
                key = (space, ident[1:])
                mem = g.tag_members_resolved.get(key)
                if mem is None and g.res is not None:
                    mem = frozenset(g.res.members(space, ident[1:]))
                if mem is None:
                    continue
                for m in mem:
                    n = g.index_of.get((space, m))
                    if n is None:
                        continue
                    acc[n] = acc.get(n, 0) + Fraction(qty)
            else:
                n = g.index_of.get((space, ident))
                if n is None:
                    continue
                acc[n] = acc.get(n, 0) + Fraction(qty)
        return acc
    return side(rec.ins), side(rec.outs)


def _tarjan(g: Graph) -> None:
    """Итеративный Тарьян."""
    n = len(g.key_of)
    index = [-1] * n
    low = [0] * n
    on_stack = bytearray(n)
    g.scc_id = [-1] * n
    stack = []
    counter = 0
    comp = 0
    sys.setrecursionlimit(max(10000, n + 100))

    for root in range(n):
        if index[root] != -1 or not g.adj[root]:
            continue
        work = [[root, 0]]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack[root] = 1
        while work:
            frame = work[-1]
            v, i = frame
            advanced = False
            while i < len(g.adj[v]):
                w = g.adj[v][i].dst
                i += 1
                frame[1] = i
                if index[w] == -1:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack[w] = 1
                    work.append([w, 0])
                    advanced = True
                    break
                elif on_stack[w]:
                    if index[w] < low[v]:
                        low[v] = index[w]
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                if low[v] < low[parent]:
                    low[parent] = low[v]
            if low[v] == index[v]:
                while True:
                    w = stack.pop()
                    on_stack[w] = 0
                    g.scc_id[w] = comp
                    g.scc_size[comp] += 1
                    if w == v:
                        break
                comp += 1