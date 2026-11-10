"""一键验收脚本（M-1~M-5 里程碑的可复核证据生成器）。

用法（项目根目录，venv 激活后）:
    python acceptance.py            # 标准验收（约 1 分钟）
    python acceptance.py --full     # 加跑 10^4 轨迹（约 +25s）

输出：逐项 ✓/✗ + 验收报告（acceptance_report.txt），全部证据可被第三人复核。
判定标准与 15-交付文档 §05 里程碑一致。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, evidence: str) -> None:
    RESULTS.append((name, ok, evidence))
    print(f"  {'✓' if ok else '✗'} {name}")


def run(cmd: list) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable] + cmd, cwd=REPO,
                          capture_output=True, text=True)


def last_line(text: str, keyword: str) -> str:
    lines = [l for l in text.splitlines() if keyword in l]
    return lines[-1].strip() if lines else text.strip()[-160:]


def main() -> int:
    full = "--full" in sys.argv
    print("=" * 62)
    print(" AI 密网 · 一期验收（自动部分）")
    print("=" * 62)

    # ---- A. 测试套件 ------------------------------------------------
    print("\n[A] 单元/契约测试套件")
    r = run(["-m", "pytest", "tests", "-q", "--no-header", "-p", "no:cacheprovider"])
    check("A1 测试套件全绿（单元/契约/集成）", r.returncode == 0,
          last_line(r.stdout, "passed"))

    # ---- B. G0 判据：回放逐字节一致 ----------------------------------
    print("\n[B] G0 判据：确定性回放")
    r = run(["-m", "replay.harness", "verify",
             "--world", "tests/fixtures/world_seed.demo.json",
             "--events", "tests/fixtures/events.demo.jsonl"])
    check("B1 回放逐字节一致（两次 SHA-256 相同）",
          "VERIFY PASS" in r.stdout, last_line(r.stdout, "VERIFY"))

    # ---- C. G0 判据：随机轨迹不变量 ----------------------------------
    n = "10000" if full else "2000"
    print(f"\n[C] G0 判据：随机轨迹不变量（{n} 条）")
    r = run(["-m", "replay.acceptance", "--trajectories", n])
    check(f"C1 {n} 轨迹四不变量 0 违反 + 回放一致",
          "ACCEPTANCE PASS" in r.stdout, last_line(r.stdout, "acceptance:"))

    # ---- D. G1 判据：pivot 演示 --------------------------------------
    print("\n[D] G1 判据：跨入口 pivot")
    r = run(["-m", "experiments.pivot_demo"])
    check("D1 SSH→LLM pivot：同 attacker_id / 世界一致 / CSVR 非零",
          "G1 DEMO PASS" in r.stdout, last_line(r.stdout, "G1 DEMO"))

    # ---- E. 跨角色工作流 ---------------------------------------------
    print("\n[E] 跨角色工作流（Dev→Test→Ops 工单流转）")
    r = run(["-m", "pytest",
             "tests/test_agent_runtime.py::test_workflow_demo_passes",
             "-q", "--no-header", "-p", "no:cacheprovider"])
    check("E1 工单流转闭环（correlation 全程可追溯，终态 closed）",
          r.returncode == 0, last_line(r.stdout, "passed"))

    # ---- F. 三来源采集 ------------------------------------------------
    print("\n[F] 统一采集：三类来源")
    r = run(["-m", "pytest",
             "tests/test_business.py::test_three_source_coverage",
             "-q", "--no-header", "-p", "no:cacheprovider"])
    check("F1 攻击者/Agent/业务 三类来源统一入 event 管道",
          r.returncode == 0, last_line(r.stdout, "passed"))

    # ---- G. 审计一致性 -------------------------------------------------
    print("\n[G] 审计一致性（契约 12 §13.3）")
    r = run(["-m", "pytest",
             "tests/test_business.py::test_audit_format_identical_across_users",
             "-q", "--no-header", "-p", "no:cacheprovider"])
    check("G1 业务审计格式对所有用户一致（无 agent 标记）",
          r.returncode == 0, "盲比对测试通过")

    # ------------------------------------------------ 报告 --------------
    ok_all = all(ok for _, ok, _ in RESULTS)
    print("\n" + "=" * 62)
    for name, ok, ev in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        print(f"        证据: {ev}")
    print("=" * 62)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"验收结论: {passed}/{len(RESULTS)} 项通过 -> "
          f"{'一期交付合格（自动部分）' if ok_all else '存在未通过项，见上方 ✗'}")
    print("说明: 人工抽查项（Docker 栈 / 真实 SSH / 浏览器 UI）见 README 验收章节。")

    report = "\n".join(f"{'PASS' if ok else 'FAIL'}  {name}\n    证据: {ev}"
                       for name, ok, ev in RESULTS)
    (REPO / "acceptance_report.txt").write_text(
        f"AI 密网一期验收报告（自动部分）\n{'=' * 40}\n{report}\n", encoding="utf-8")
    print("报告已写入 acceptance_report.txt")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
