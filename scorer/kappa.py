"""κ 一致性与 capture–recapture recall 区间（契约 12 §4.3 申报纪律的统计工具）。

三个函数，对应度量篇的三条统计要求：
1. cohens_kappa      两名标注员的独立性一致性（G2: κ ≥ 0.6）
2. recall_estimate   scorer 检出集 × 标注参考集 的 capture–recapture：
                     Chapman 修正估计 + Seber 近似 95% CI——一切 CSVR 必附的 recall 区间
3. 只报点值视为无效（§4.3）：本模块输出永远带区间字段
"""

from __future__ import annotations

import math


def cohens_kappa(labels_a: dict, labels_b: dict) -> dict:
    """Cohen's κ。输入：{item_id: 类别}，类别形如 "T"/"C"/"M"/"I"/"none"。

    只对两名标注员都标注的条目计算；类别集取两者并集。
    """
    common = sorted(set(labels_a) & set(labels_b))
    n = len(common)
    if n == 0:
        return {"n": 0, "kappa": None, "p0": None, "pe": None,
                "note": "无共同标注条目"}
    cats = sorted({labels_a[i] for i in common} | {labels_b[i] for i in common})

    obs = {(c, c): 0 for c in cats}
    row = {c: 0 for c in cats}
    col = {c: 0 for c in cats}
    for i in common:
        a, b = labels_a[i], labels_b[i]
        row[a] += 1
        col[b] += 1
        if a == b:
            obs[(a, b)] += 1

    p0 = sum(obs[(c, c)] for c in cats) / n
    pe = sum(row[c] * col[c] for c in cats) / (n * n)
    kappa = (p0 - pe) / (1 - pe) if pe < 1 else 1.0
    return {"n": n, "categories": cats, "p0": round(p0, 4),
            "pe": round(pe, 4), "kappa": round(kappa, 4)}


def _fmt_ci(lo, hi):
    if lo is None:
        return None
    return [round(lo, 4), round(hi, 4)]


def recall_estimate(detected: set, reference: set, z: float = 1.96) -> dict:
    """scorer 检出集（首次捕获）× 标注参考集（重捕）→ recall 区间。

    - Chapman 无偏修正：N̂ = (n+1)(m+1)/(k+1) − 1（k=0 时退化为不可估，如实报告）
    - Seber 近似方差：Var(N̂) = (n+1)(m+1)(n−k)(m−k) / ((k+1)²(k+2))
    - recall = n / N̂，附 95% Wald 区间（样本小、k 小时区间偏宽——如实报，不美化）
    """
    n, m = len(detected), len(reference)
    k = len(detected & reference)
    if k == 0:
        return {"n_detected": n, "n_reference": m, "n_overlap": 0,
                "n_estimated": None, "recall": None, "recall_ci95": None,
                "note": "零重叠：capture–recapture 不可估；按 §4.3 该 CSVR 结论无效，需扩大标注量"}
    n_est = (n + 1) * (m + 1) / (k + 1) - 1
    var = (n + 1) * (m + 1) * (n - k) * (m - k) / ((k + 1) ** 2 * (k + 2))
    sd = math.sqrt(max(0.0, var))
    recall = n / n_est
    recall_lo = n / (n_est + z * sd)
    recall_hi = n / max(1e-12, n_est - z * sd)
    return {"n_detected": n, "n_reference": m, "n_overlap": k,
            "n_estimated": round(n_est, 2),
            "recall": round(recall, 4),
            "recall_ci95": _fmt_ci(recall_lo, recall_hi),
            "n_estimated_ci95": [round(max(k, n_est - z * sd), 2),
                                 round(n_est + z * sd, 2)]}
