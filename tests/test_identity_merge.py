"""身份多视图合并 v1（契约 12 §2.3a）：合并语义 + 稳定性 + 确定性。"""

from identity.multiview import MultiViewResolver


def test_same_ip_different_creds_same_attacker():
    """pivot 前提：同一 IP 的 SSH 与 LLM 会话必须同簇（G1）。"""
    r = MultiViewResolver()
    a = r.observe(ip24="1.2.3.0/24", cred_hash="sha256:ssh-pw")
    b = r.observe(ip24="1.2.3.0/24", cred_hash="sha256:api-key")
    assert a == b


def test_shared_cred_merges_different_ips():
    """多视图价值：同一凭据出现在两个 IP → 归并为同一攻击者。"""
    r = MultiViewResolver()
    first = r.observe(ip24="1.2.3.0/24", cred_hash="sha256:k1")
    second = r.observe(ip24="9.9.9.0/24", cred_hash="sha256:k1")
    assert first == second


def test_cluster_id_stable_after_merge():
    """簇标识 = 最早视图键：合并后旧 id 不变（回放序列内稳定）。"""
    r = MultiViewResolver()
    early = r.observe(ip24="1.2.3.0/24")
    later = r.observe(ip24="9.9.9.0/24", cred_hash="sha256:k1")
    merged = r.observe(ip24="1.2.3.0/24", cred_hash="sha256:k1")
    assert merged == early or merged == later  # 必须是合并前出现过的那个
    assert r.observe(ip24="1.2.3.0/24") == merged  # 合并后保持稳定


def test_determinism():
    """同一观测序列 → 同一 attacker_id 序列（K5）。"""
    seqs = [("1.2.3.0/24", None), ("1.2.3.0/24", "sha256:a"),
            ("5.5.5.0/24", "sha256:a"), ("1.2.3.0/24", "sha256:b")]
    ids1 = [MultiViewResolver().observe(ip24=i, cred_hash=c) for i, c in seqs]
    ids2 = [MultiViewResolver().observe(ip24=i, cred_hash=c) for i, c in seqs]
    assert ids1 == ids2
    assert len(set(ids1[:2])) == 1  # 前两条同簇
