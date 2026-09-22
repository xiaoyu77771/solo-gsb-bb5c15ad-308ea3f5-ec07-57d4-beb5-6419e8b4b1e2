import random

from app.matching import Event, lcs_grid_match, monotone_match, two_pass_match


def _events(seq):
    return [Event(index=i, label=str(x), t=float(i)) for i, x in enumerate(seq)]


def test_sparse_match_equals_grid_lcs_random():
    rng = random.Random(7)
    for _ in range(200):
        ref = _events([rng.choice("ABC") for _ in range(rng.randint(1, 12))])
        tgt = _events([rng.choice("ABC") for _ in range(rng.randint(1, 12))])
        assert len(monotone_match(ref, tgt)) == len(lcs_grid_match(ref, tgt))


def test_sparse_match_chain_is_monotone_and_same_label():
    rng = random.Random(11)
    ref = _events([rng.choice("AB") for _ in range(50)])
    tgt = _events([rng.choice("AB") for _ in range(50)])
    pairs = monotone_match(ref, tgt)
    assert pairs == sorted(pairs)
    for i, j in pairs:
        assert ref[i].label == tgt[j].label
    assert [i for i, _ in pairs] == sorted(set(i for i, _ in pairs))
    assert [j for _, j in pairs] == sorted(set(j for _, j in pairs))


def test_two_pass_breaks_periodic_label_ambiguity():
    # 目标整体慢 100 秒（大常数偏移），且同一标签周期性出现。
    # 纯 LCS 无法区分周期重复，两遍匹配应选时间一致的配对。
    ref = [Event(i, "PING", t=float(i * 60)) for i in range(6)]
    tgt = [Event(i, "PING", t=float(i * 60) - 100.0) for i in range(6)]
    pairs = two_pass_match(ref, tgt)
    assert pairs == [(i, i) for i in range(6)]


def test_missing_and_inserted_events():
    ref = _events(["A", "B", "C", "D"])
    # 缺失 B、C；插入 X、Y
    tgt = _events(["X", "A", "Y", "D"])
    pairs = monotone_match(ref, tgt)
    assert pairs == [(0, 1), (3, 3)]
