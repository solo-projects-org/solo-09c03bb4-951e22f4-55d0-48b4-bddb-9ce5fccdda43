"""同秒组求解内存基准：观察"换人 / 得分 / 球员"增多时的峰值内存。

用法（在仓库根目录）：

    python -m scripts.bench_memory

基准构造"最坏情况"的时刻组：同秒 ``n`` 次换人、``n`` 次得分，全部本方得分都由
始终在场的 A1 完成（不触发得分者在场剪枝），状态网格完全铺开。``tracemalloc``
只统计 Python 对象分配，反映求解器自身的工作内存。

输出两张表：

1. 换人、得分、球员同步增长（真实比赛里三者往往一起变多）；
2. 事件数固定、只增加球员数，验证内存不再随"状态 × 球员"增长。
"""

from __future__ import annotations

import argparse
import gc
import sys
import time
import tracemalloc

from app.engine import ScoreEvent, SubEvent, _solve_group

STARTERS = ["A1", "A2", "A3", "A4", "A5"]


def build_group(n_sub: int, n_score: int, n_players: int):
    """构造同秒组：n_sub 次换人 + n_score 次得分，可额外填充无关球员。"""
    subs: list[SubEvent] = []
    on_court = ["A2", "A3", "A4", "A5"]
    bench: list[str] = []
    for k in range(n_sub):
        incoming = f"B{k + 1}"
        outgoing = on_court.pop(0)
        subs.append(SubEvent(1, 100, outgoing, incoming, k + 1))
        on_court.append(incoming)
        bench.append(incoming)
    scores = [
        ScoreEvent(1, 100, "own", 1, "A1", k + 1) for k in range(n_score)
    ]
    players = sorted(set(STARTERS) | set(bench))
    players += [f"Z{k}" for k in range(max(0, n_players - len(players)))]
    return subs, scores, players


def measure(n_sub: int, n_score: int, n_players: int):
    subs, scores, players = build_group(n_sub, n_score, n_players)
    gc.collect()
    tracemalloc.start()
    started = time.perf_counter()
    try:
        info, _ = _solve_group(
            subs, scores, [], frozenset(STARTERS), players
        )
        elapsed = time.perf_counter() - started
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    return peak, elapsed, info.legal_orders


def _print_row(n_sub, n_score, n_players, peak, elapsed, orders):
    print(
        f"{n_sub:>4} {n_score:>4} {n_players:>4} "
        f"{peak / 1024:>10.1f} KiB {peak / 1024 / 1024:>8.3f} MiB "
        f"{elapsed:>8.2f}s {orders:>10}"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="同秒组求解内存基准")
    parser.add_argument(
        "--scale",
        type=int,
        default=3,
        help="规模上限倍数（默认 3，即最大约 120/120 事件；越大越慢）",
    )
    args = parser.parse_args(argv)

    header = (
        f"{'换人':>4} {'得分':>4} {'球员':>4} {'峰值内存':>14} "
        f"{'折合':>12} {'耗时':>9} {'合法顺序':>10}"
    )
    print("表 1：换人、得分、球员同步增长")
    print(header)
    for n in (10, 20, 40, 80, 120):
        if n > 40 * args.scale:
            continue
        _print_row(n, n, 9 + n // 4, *measure(n, n, 9 + n // 4))

    print()
    print("表 2：事件数固定为 40 换人 + 40 得分，只增加球员数")
    print(header)
    for extra in (0, 40, 120, 360, 1080):
        _print_row(40, 40, 14 + extra, *measure(40, 40, 14 + extra))
    return 0


if __name__ == "__main__":
    sys.exit(main())
