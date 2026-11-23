"""正负值区间计算引擎。

一场比赛的事件按 (节次, 剩余秒数) 确定的比赛时刻排序；同一时刻（同节同秒）
的多条换人 / 得分记录在两份 CSV 内各自保持原始行序，但两表之间可以交错。
引擎在所有满足规则的交错顺序上，用动态规划求每名本方球员正负值的可达最小值
和最大值。

规则：
* 某时刻之前的换人必须先于该时刻之后的任何事件（时刻之间顺序固定）。
* 同一时刻内，换人之间、得分之间保持各自表内行序；换人与得分可交错。
* 某笔换人执行时，下场球员必须在场、上场球员必须在场外，否则数据本身不合法。
* 本方得分执行时，得分者必须在场（对方得分不记录得分者）。
* 核实记录给同一时刻内的一笔换人、一笔得分附加确定的先后约束。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class SubEvent:
    """一笔换人。row_no 是换人 CSV 内从 1 开始的行号（不含表头）。"""

    period: int
    remaining: int
    outgoing: str
    incoming: str
    row_no: int


@dataclass(frozen=True)
class ScoreEvent:
    """一笔得分。row_no 是得分 CSV 内从 1 开始的行号（不含表头）。"""

    period: int
    remaining: int
    team: str  # 'own' 或 'opp'
    points: int
    scorer: Optional[str]
    row_no: int


@dataclass(frozen=True)
class OrderEdge:
    """一条同节同秒的先后核实：first 行先于 second 行。

    first/second 为 (表名, 行号)，表名为 'sub' 或 'score'。
    """

    period: int
    remaining: int
    first_table: str
    first_row: int
    second_table: str
    second_row: int


@dataclass
class GroupInfo:
    """同一时刻分组的说明信息，供页面展示。"""

    period: int
    remaining: int
    sub_rows: list[int] = field(default_factory=list)
    score_rows: list[int] = field(default_factory=list)
    legal_orders: int = 0
    ambiguous: bool = False


@dataclass
class InfeasibleError(Exception):
    """不存在满足全部规则与核实约束的合法交错顺序。

    ``period`` / ``remaining`` 指出出问题的节次与比赛时刻；``blocking`` 列出
    造成阻塞的原表记录（``{"table": "sub"|"score", "row": 行号}``），便于页面
    直接定位到 CSV 行。
    """

    reason: str
    period: Optional[int] = None
    remaining: Optional[int] = None
    blocking: list[dict] = field(default_factory=list)

    def __str__(self) -> str:  # pragma: no cover - 仅为调试
        return self.reason

    def as_detail(self) -> dict:
        """结构化的错误明细（供 API 返回，兼容旧的字符串 detail）。"""
        return {
            "reason": self.reason,
            "period": self.period,
            "remaining": self.remaining,
            "blocking": list(self.blocking),
        }


WAY_CAP = 10_000_000


def _group_events(subs: list[SubEvent], scores: list[ScoreEvent]):
    """按比赛时刻把两表事件分组，时刻顺序固定；组内保留两表各自行序。"""
    buckets: dict[tuple[int, int], dict[str, list]] = defaultdict(
        lambda: {"subs": [], "scores": []}
    )
    for ev in subs:
        buckets[(ev.period, ev.remaining)]["subs"].append(ev)
    for ev in scores:
        buckets[(ev.period, ev.remaining)]["scores"].append(ev)
    # 剩余秒数越大比赛时刻越早。
    keys = sorted(buckets.keys(), key=lambda k: (k[0], -k[1]))
    return [(k, buckets[k]["subs"], buckets[k]["scores"]) for k in keys]


def _local_edges(
    subs: list[SubEvent],
    scores: list[ScoreEvent],
    edges: list[OrderEdge],
) -> list[tuple[str, int, str, int]]:
    """把全局行号的核实约束翻译为组内局部下标，统一成两种形式：

    * ('sub', i, 'score', j)：换人 i 必须先于得分 j；
    * ('score', j, 'sub', i)：得分 j 必须先于换人 i。

    传入的 edges 已按 (节次, 剩余秒) 过滤到本组；这里再依据行号做一次防御性
    过滤，避免其他时刻的约束被错误套用到本组。
    """
    sub_index = {ev.row_no: i for i, ev in enumerate(subs)}
    score_index = {ev.row_no: j for j, ev in enumerate(scores)}
    local: list[tuple[str, int, str, int]] = []
    for e in edges:
        if e.first_table == "sub":
            if e.first_row not in sub_index or e.second_row not in score_index:
                continue
            local.append(
                ("sub", sub_index[e.first_row], "score", score_index[e.second_row])
            )
        else:
            if e.first_row not in score_index or e.second_row not in sub_index:
                continue
            local.append(
                ("score", score_index[e.first_row], "sub", sub_index[e.second_row])
            )
    return local


def _contradicting_edges(local_edges) -> Optional[tuple]:
    """同一对换人与得分若被双向约束，直接冲突。"""
    directed = set(local_edges)
    for edge in local_edges:
        ta, ia, tb, ib = edge
        if (tb, ib, ta, ia) in directed:
            return edge
    return None


def _requirements(
    n_sub: int, n_score: int, local_edges
) -> tuple[list[int], list[int]]:
    """把组内先后约束压缩成每个事件的前置数量。

    * ``score_need[i]``：执行第 i 笔换人前至少要完成的得分数；
    * ``sub_need[j]``：执行第 j 笔得分前至少要完成的换人数。

    同一时刻内换人与得分各自保持行序，所以"某笔得分先于换人 i"等价于
    "换人 i 之前至少完成 a+1 笔得分"，取所有约束的最大值即可。
    """
    score_need = [0] * n_sub
    sub_need = [0] * n_score
    for ta, ia, tb, ib in local_edges:
        if ta == "score" and tb == "sub":
            if ia + 1 > score_need[ib]:
                score_need[ib] = ia + 1
        elif ta == "sub" and tb == "score":
            if ia + 1 > sub_need[ib]:
                sub_need[ib] = ia + 1
    return score_need, sub_need


def _transition_reasons(
    i: int,
    j: int,
    subs: list[SubEvent],
    scores: list[ScoreEvent],
    score_need: list[int],
    sub_need: list[int],
    courts: list[frozenset[str]],
) -> tuple[bool, bool, list[tuple[str, list[dict]]]]:
    """返回 (换人边是否可走, 得分边是否可走, 被阻塞原因)。

    原因以 ``(说明文字, 阻塞记录列表)`` 返回；每条阻塞记录为
    ``{"table", "row"}``，直接对应原表行号。只检查与先后核实、得分者在场有关
    的动态条件；换人本身的上下场合法性已由静态检查保证。
    """
    n_sub, n_score = len(subs), len(scores)
    reasons: list[tuple[str, list[dict]]] = []
    sub_ok = False
    score_ok = False
    if i < n_sub:
        if j < score_need[i]:
            a = score_need[i] - 1
            reasons.append(
                (
                    f"核实要求得分 CSV 第 {scores[a].row_no} 行先于 "
                    f"换人 CSV 第 {subs[i].row_no} 行，但该得分此前尚未发生",
                    [
                        {"table": "score", "row": scores[a].row_no},
                        {"table": "sub", "row": subs[i].row_no},
                    ],
                )
            )
        else:
            sub_ok = True
    if j < n_score:
        sc = scores[j]
        if i < sub_need[j]:
            a = sub_need[j] - 1
            reasons.append(
                (
                    f"核实要求换人 CSV 第 {subs[a].row_no} 行先于 "
                    f"得分 CSV 第 {sc.row_no} 行，但该换人此前尚未发生",
                    [
                        {"table": "sub", "row": subs[a].row_no},
                        {"table": "score", "row": sc.row_no},
                    ],
                )
            )
        elif sc.team == "own" and sc.scorer not in courts[i]:
            reasons.append(
                (
                    f"本方得分者 {sc.scorer}（得分 CSV 第 {sc.row_no} 行）"
                    f"在该时刻任何已构造的合法顺序中都无法处于场上",
                    [{"table": "score", "row": sc.row_no}],
                )
            )
        else:
            score_ok = True
    return sub_ok, score_ok, reasons


def _layer_dp(
    n_sub: int,
    n_score: int,
    value_fn,
    merge_fn,
    add_fn,
    init_value,
    cap: Optional[int] = None,
):
    """沿 (已执行换人数 i, 已执行得分数 j) 逐层滚动。

    ``value_fn(i, j)`` 返回 ``(换人边增量, 得分边增量)``，不可走的一边为
    ``None``；``add_fn(value, delta)`` 把增量累加到状态值上；``merge_fn(old,
    new)`` 合并同一目标状态的多个来源；``cap`` 给定时结果截断到该上限（用于数
    顺序数）。

    每层用紧凑数组按 ``i`` 连续存放，只保留当前层与下一层；状态数上限是
    ``min(n_sub, n_score) + 1``，因此工作内存与换人数/得分数成线性关系。
    返回 ``{i: value}``（仅含可达状态）。
    """
    lo = 0
    values: list[object] = [init_value]
    for total in range(n_sub + n_score):
        next_lo = max(0, total + 1 - n_score)
        next_hi = min(n_sub, total + 1)
        nxt: list[object] = [None] * (next_hi - next_lo + 1)
        for k, value in enumerate(values):
            if value is None:
                continue
            i = lo + k
            j = total - i
            delta_sub, delta_score = value_fn(i, j)
            if delta_sub is not None:
                new = add_fn(value, delta_sub)
                if cap is not None and new > cap:
                    new = cap
                idx = i + 1 - next_lo
                old = nxt[idx]
                nxt[idx] = new if old is None else merge_fn(old, new)
            if delta_score is not None:
                new = add_fn(value, delta_score)
                if cap is not None and new > cap:
                    new = cap
                idx = i - next_lo
                old = nxt[idx]
                nxt[idx] = new if old is None else merge_fn(old, new)
        if all(v is None for v in nxt):
            return {}
        lo = next_lo
        values = nxt
    return {lo + k: v for k, v in enumerate(values) if v is not None}


def _count_ways(
    subs: list[SubEvent],
    scores: list[ScoreEvent],
    score_need: list[int],
    sub_need: list[int],
    courts: list[frozenset[str]],
) -> tuple[int, Optional[tuple[int, str, list[dict]]]]:
    """统计合法交错顺序数（上限截断），并返回最早的阻塞层信息。"""
    n_sub, n_score = len(subs), len(scores)
    lo = 0
    counts: list[int] = [1]
    next_lo = 0
    for total in range(n_sub + n_score):
        next_lo = max(0, total + 1 - n_score)
        next_hi = min(n_sub, total + 1)
        nxt: list[int] = [0] * (next_hi - next_lo + 1)
        for k, count in enumerate(counts):
            if not count:
                continue
            i = lo + k
            j = total - i
            if i < n_sub and j >= score_need[i]:
                idx = i + 1 - next_lo
                nxt[idx] = min(WAY_CAP, nxt[idx] + count)
            if j < n_score:
                sc = scores[j]
                if i >= sub_need[j] and (
                    sc.team != "own" or sc.scorer in courts[i]
                ):
                    idx = i - next_lo
                    nxt[idx] = min(WAY_CAP, nxt[idx] + count)
        if not any(nxt):
            # 该层所有可达状态都推进不了：收集原因供报错使用。
            texts: list[str] = []
            blocking: list[dict] = []
            for k, count in enumerate(counts):
                if not count:
                    continue
                i = lo + k
                _, _, rs = _transition_reasons(
                    i, total - i, subs, scores, score_need, sub_need, courts
                )
                for text, recs in rs:
                    if text not in texts:
                        texts.append(text)
                    for rec in recs:
                        if rec not in blocking:
                            blocking.append(rec)
            detail = "；".join(texts) or "换人与得分的先后约束相互矛盾"
            return 0, (total, detail, blocking)
        lo = next_lo
        counts = nxt
    final = counts[n_sub - lo] if 0 <= n_sub - lo < len(counts) else 0
    return final, None


def _merge_range(old, new):
    return (min(old[0], new[0]), max(old[1], new[1]))


def _add_range(value, delta):
    return (value[0] + delta, value[1] + delta)


def _player_range(
    active: list[bool],
    scores: list[ScoreEvent],
    weighted: list[int],
    score_need: list[int],
    sub_need: list[int],
    courts: list[frozenset[str]],
) -> tuple[int, int]:
    """给定球员的在场轨迹，求他在组内正负值的可达区间。

    ``active[i]`` 表示执行完前 ``i`` 笔换人后他是否在场。工作内存为一层
    ``{i: 区间}``，规模上限是 ``min(换人数, 得分数) + 1``，与球员数无关；
    同组内多名球员共享同一条轨迹时只需算一次。整组都在场/都不在场直接取值。
    """
    if not any(active):
        return (0, 0)
    if all(active):
        total = sum(weighted)
        return (total, total)

    n_sub, n_score = len(score_need), len(sub_need)

    def value_fn(i, j):
        sub_edge = 0 if (i < n_sub and j >= score_need[i]) else None
        score_edge: object = None
        if j < n_score and i >= sub_need[j]:
            sc = scores[j]
            if sc.team != "own" or sc.scorer in courts[i]:
                score_edge = weighted[j] if active[i] else 0
        return sub_edge, score_edge

    final = _layer_dp(
        n_sub, n_score, value_fn, _merge_range, _add_range, (0, 0)
    )
    return final.get(n_sub, (0, 0))


def _solve_group(
    subs: list[SubEvent],
    scores: list[ScoreEvent],
    edges: list[OrderEdge],
    court: frozenset[str],
    players: list[str],
):
    """求解单个时刻分组。

    返回 (组信息, {球员: (最小增量, 最大增量)})；无解时抛 InfeasibleError。

    换人本身的合法性与交错顺序无关，先做静态检查。组内求解分三步，任何一步
    的工作内存都只与组内换人数、得分、核实及球员总数成线性关系：

    1. 用 ``(i, j)`` 网格的滚动层统计合法交错顺序数（截断到 WAY_CAP）；
    2. 对每名球员单独跑一次同样的滚动 DP，求其累计增量的最小/最大值。
       同一状态里不再存放全部球员的向量，避免"状态数 × 球员数"的内存；
    3. 无解时沿最早的阻塞层收集换人/得分原表行号与原因。
    """
    period, remaining = (
        (subs[0].period, subs[0].remaining)
        if subs
        else (scores[0].period, scores[0].remaining)
    )
    n_sub, n_score = len(subs), len(scores)
    local_edges = _local_edges(subs, scores, edges)

    contradiction = _contradicting_edges(local_edges)
    if contradiction is not None:
        ta, ia, tb, ib = contradiction
        row_a = subs[ia].row_no if ta == "sub" else scores[ia].row_no
        row_b = subs[ib].row_no if tb == "sub" else scores[ib].row_no
        label_a = "换人" if ta == "sub" else "得分"
        label_b = "换人" if tb == "sub" else "得分"
        raise InfeasibleError(
            f"第 {period} 节剩余 {remaining} 秒：{label_a} CSV 第 {row_a} 行与"
            f"{label_b} CSV 第 {row_b} 行被同时登记了互斥的先后顺序",
            period,
            remaining,
            [
                {"table": ta, "row": row_a},
                {"table": tb, "row": row_b},
            ],
        )

    # 静态检查换人：换人之间顺序固定，逐笔执行时上下场状态必须合法。
    cur = set(court)
    for ev in subs:
        if ev.outgoing not in cur:
            raise InfeasibleError(
                f"第 {period} 节剩余 {remaining} 秒：下场球员 {ev.outgoing}"
                f"（换人 CSV 第 {ev.row_no} 行）此时不在场上",
                period,
                remaining,
                [{"table": "sub", "row": ev.row_no}],
            )
        if ev.incoming in cur:
            raise InfeasibleError(
                f"第 {period} 节剩余 {remaining} 秒：上场球员 {ev.incoming}"
                f"（换人 CSV 第 {ev.row_no} 行）此时已在场上",
                period,
                remaining,
                [{"table": "sub", "row": ev.row_no}],
            )
        cur.discard(ev.outgoing)
        cur.add(ev.incoming)

    # courts[i]：执行完前 i 笔换人后的场上阵容（阵容只由 i 决定）。
    courts: list[frozenset[str]] = [frozenset(court)]
    cur = set(court)
    for ev in subs:
        cur.discard(ev.outgoing)
        cur.add(ev.incoming)
        courts.append(frozenset(cur))

    score_need, sub_need = _requirements(n_sub, n_score, local_edges)
    weighted = [
        (sc.points if sc.team == "own" else -sc.points) for sc in scores
    ]

    legal_orders, blocked = _count_ways(
        subs, scores, score_need, sub_need, courts
    )
    if blocked is not None:
        layer, detail, blocking = blocked
        raise InfeasibleError(
            f"第 {period} 节剩余 {remaining} 秒：已合法推进 {layer} 个事件后，"
            f"其余换人/得分无法继续，{detail}",
            period,
            remaining,
            blocking,
        )

    # 每名球员的在场轨迹只由 (球员, i) 决定；轨迹相同的球员共享一次计算，
    # 结果再回填给本组所有相关球员。按"是否首发 + 上下场事件序列"分组，避免
    # 为每名球员物化整条轨迹——所有签名条目总数不超过 2×换人数。
    changes_by_player: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for k, ev in enumerate(subs):
        changes_by_player[ev.outgoing].append((k, -1))
        changes_by_player[ev.incoming].append((k, 1))
    deltas: dict[str, tuple[int, int]] = {}
    by_pattern: dict[tuple, list[str]] = defaultdict(list)
    for p in players:
        by_pattern[(p in court, tuple(changes_by_player.get(p, ())))].append(p)
    for group_players in by_pattern.values():
        representative = group_players[0]
        active = [representative in courts[0]]
        on_court = active[0]
        for ev in subs:
            if ev.outgoing == representative:
                on_court = False
            elif ev.incoming == representative:
                on_court = True
            active.append(on_court)
        value = _player_range(
            active, scores, weighted, score_need, sub_need, courts
        )
        for p in group_players:
            deltas[p] = value
    info = GroupInfo(
        period=period,
        remaining=remaining,
        sub_rows=[ev.row_no for ev in subs],
        score_rows=[ev.row_no for ev in scores],
        legal_orders=legal_orders,
        ambiguous=(n_sub > 0 and n_score > 0 and legal_orders > 1),
    )
    return info, deltas


def compute(
    starters: list[str],
    subs: list[SubEvent],
    scores: list[ScoreEvent],
    edges: Optional[list[OrderEdge]] = None,
):
    """计算全场正负值区间。

    返回 {"feasible", "ranges", "groups", "total_legal_orders", "players"}；
    无解时抛 InfeasibleError。
    """
    edges = edges or []
    starters = list(starters)
    if len(starters) != 5 or len(set(starters)) != 5:
        raise ValueError("首发必须是 5 名不同球员")

    players = sorted(
        set(starters)
        | {ev.outgoing for ev in subs}
        | {ev.incoming for ev in subs}
        | {ev.scorer for ev in scores if ev.scorer}
    )

    groups = _group_events(subs, scores)
    edges_by_group: dict[tuple[int, int], list[OrderEdge]] = defaultdict(list)
    for e in edges:
        edges_by_group[(e.period, e.remaining)].append(e)

    court = frozenset(starters)
    total_min = {p: 0 for p in players}
    total_max = {p: 0 for p in players}
    group_infos: list[GroupInfo] = []
    total_orders = 1

    for (period, remaining), g_subs, g_scores in groups:
        info, deltas = _solve_group(
            g_subs,
            g_scores,
            edges_by_group.get((period, remaining), []),
            court,
            players,
        )
        group_infos.append(info)
        for p in players:
            lo, hi = deltas[p]
            total_min[p] += lo
            total_max[p] += hi
        if info.legal_orders >= WAY_CAP or total_orders >= WAY_CAP:
            total_orders = WAY_CAP
        else:
            total_orders *= info.legal_orders
        # 分组结束后阵容固定（组内换人全部完成，与交错方式无关）。
        for ev in g_subs:
            court = frozenset((set(court) - {ev.outgoing}) | {ev.incoming})

    return {
        "feasible": True,
        "ranges": {
            p: {"min": total_min[p], "max": total_max[p]} for p in players
        },
        "groups": group_infos,
        "total_legal_orders": total_orders,
        "players": players,
    }
