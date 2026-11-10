"""M2 核心统计：Cohen's κ 与 capture–recapture recall 区间（契约 12 §4.3）。"""

from scorer.kappa import cohens_kappa, recall_estimate


def test_kappa_perfect_agreement():
    labels = {f"i{k}": "T" for k in range(10)}
    labels.update({f"j{k}": "none" for k in range(10)})
    r = cohens_kappa(labels, dict(labels))
    assert r["n"] == 20 and r["kappa"] == 1.0


def test_kappa_known_partial_disagreement():
    """部分分歧构造：x4 一处分歧（p0=0.8, pe=0.56）→ κ≈0.55 ∈ (0,1)。"""
    a = {"x1": "T", "x2": "T", "x3": "T", "x4": "T", "x5": "none"}
    b = {"x1": "T", "x2": "T", "x3": "T", "x4": "none", "x5": "none"}
    r = cohens_kappa(a, b)
    assert r["n"] == 5
    assert abs(r["p0"] - 0.8) < 1e-9 and abs(r["pe"] - 0.56) < 1e-9
    assert abs(r["kappa"] - 0.5455) < 1e-3


def test_kappa_no_overlap():
    r = cohens_kappa({"a": "T"}, {"b": "T"})
    assert r["kappa"] is None and r["n"] == 0


def test_recall_with_overlap():
    """scorer 检出 40，标注确认 30，重叠 25 → recall 有区间且 (0,1]。"""
    detected = {f"d{k}" for k in range(40)}
    reference = {f"d{k}" for k in range(25)} | {f"r{k}" for k in range(5)}
    r = recall_estimate(detected, reference)
    assert r["n_overlap"] == 25
    assert 0 < r["recall"] <= 1
    assert r["recall_ci95"] and r["recall_ci95"][0] <= r["recall"] <= r["recall_ci95"][1]


def test_recall_zero_overlap_invalid():
    """零重叠 → 不可估，按 §4.3 该结论无效（必须如实返回 None）。"""
    r = recall_estimate({"a", "b"}, {"c", "d"})
    assert r["recall"] is None and r["n_overlap"] == 0
    assert "无效" in r["note"]
