"""eval 运行器：把 tasks.py 里的每条任务跑一遍真 LLM，逐条断言，出报告。

与 pytest 完全分离 —— pytest 验证内核机制（mock、无网络、秒级），这里
验证端到端行为（真模型、要 Key、按需跑）。跑一次的成本是任务数 × 每任务
几轮对话的 token，属于"改 prompt / schema 前后各跑一次"的标尺。

断言只对 RunResult 做机械检查（transcript、turns、stop_reason 里都有），
模型"表现好"被拆成一个个可判定的侧面，而不是一个笼统的分数 —— 分数没法
归因，断言可以。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from niulai.cli import _assemble
from niulai.config import Settings
from niulai.errors import ConfigError
from niulai.events import EventEmitter, new_run_id
from niulai.trace import TraceWriter

if __package__ in (None, ""):
    # 以脚本方式运行（python evals/run.py / IDE 运行按钮）时，
    # Python 不知道本文件属于 evals 包，相对导入会失败。
    # 把仓库根塞进 sys.path 后按绝对包名导入，两种跑法都能用。
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from evals.tasks import TASKS, Task
else:
    from .tasks import TASKS, Task

# ---------------------------------------------------------------- 断言


@dataclass(slots=True)
class Observation:
    """从 RunResult 里提取的、断言要用的事实。"""

    calls: list[tuple[str, str]]  # (tool_name, 原始参数 JSON)
    answer: str
    turns: int
    stop_reason: str
    saw_artifact: bool
    data_flake: bool  # 有工具结果回灌了"所有数据源都失败了" —— 数据源被限流


def observe(result) -> Observation:
    calls = [
        (call.name, call.arguments)
        for m in result.messages
        if m.role == "assistant" and m.tool_calls
        for call in m.tool_calls
    ]
    saw_artifact = any(
        m.role == "tool" and m.content and "artifacts/" in m.content
        for m in result.messages
    )
    data_flake = any(
        m.role == "tool" and m.content and "所有数据源都失败了" in m.content
        for m in result.messages
    )
    return Observation(
        calls=calls,
        answer=result.text or "",
        turns=result.turns,
        stop_reason=result.stop_reason,
        saw_artifact=saw_artifact,
        data_flake=data_flake,
    )


@dataclass(slots=True)
class Verdict:
    ok: bool = True
    skip: bool = False  # 数据源限流等外部原因无法判定 —— 不算通过也不算回归
    failures: list[str] = field(default_factory=list)

    def check(self, cond: bool, message: str) -> None:
        if not cond:
            self.failures.append(message)
            self.ok = False


def judge(task: Task, obs: Observation) -> Verdict:
    v = Verdict()
    called = [name for name, _ in obs.calls]

    for name in task.must_call:
        v.check(name in called, f"应调用 {name}，实际调用序列: {called or '无'}")
    for name in task.must_not_call:
        v.check(name not in called, f"不应调用 {name}，却调用了 {obs.calls.count(name)} 次")

    for tool, needle in task.arg_contains:
        hit = any(n == tool and needle in args for n, args in obs.calls)
        v.check(hit, f"{tool} 的参数里应含 {needle!r}")

    v.check(obs.turns <= task.max_turns, f"轮数 {obs.turns} 超过上限 {task.max_turns}")

    if task.require_answer:
        v.check(
            obs.stop_reason in ("done", "max_turns"),
            f"运行未正常收敛（stop_reason={obs.stop_reason}）",
        )
        v.check(bool(obs.answer.strip()), "没有最终文本回答")

    for phrase in task.forbid_in_answer:
        v.check(phrase not in obs.answer, f"回答里出现了禁用表述 {phrase!r}")

    if task.expect_artifact:
        if obs.saw_artifact:
            pass
        elif obs.data_flake:
            # 数据源限流 → 模型根本没拿到数据，落盘链路无从发生。
            # 这是外部依赖抖动，不是 agent 回归，判 SKIP 让标尺只量 agent。
            v.skip = True
            v.failures.append("数据源被限流，没取到 K 线 —— 落盘链路无法判定（SKIP）")
        else:
            v.check(False, "预期走落盘链路，但上下文里没有 artifacts/ 引用")

    return v


# ---------------------------------------------------------------- 运行


async def run_task(task: Task) -> tuple[Verdict, int, int, float]:
    """跑一条任务，返回（判定结果, 轮数, tokens, 耗时秒）。异常也归一成失败的 Verdict。"""
    start = time.monotonic()
    asm = None
    try:
        settings = Settings.load()
        asm = _assemble(settings)
        emitter = EventEmitter(new_run_id())
        emitter.subscribe(TraceWriter(emitter.run_id))  # 照常落 trace，便于事后归因
        result = await asm.runtime.run(task.task, emitter=emitter)
        verdict = judge(task, observe(result))
        return verdict, result.turns, result.usage.total_tokens, time.monotonic() - start
    except Exception as exc:  # noqa: BLE001 —— 单条任务失败不拖垮整批
        v = Verdict(ok=False)
        v.failures.append(f"运行异常: {type(exc).__name__}: {exc}")
        return v, 0, 0, time.monotonic() - start
    finally:
        # 每条任务一套装配（本 eval 不追求连接复用），用完必须收，
        # 否则跑 10 条任务就漏 10 个 httpx 连接池
        if asm is not None:
            await asm.llm.aclose()
            await asm.service.aclose()


async def main_async(filter_: str, verbose: bool) -> int:
    tasks = [t for t in TASKS if filter_ in t.id]
    if not tasks:
        print(f"没有 id 含 {filter_!r} 的任务")
        return 2

    print(f"共 {len(tasks)} 条任务。断言是确定性的，但模型表现是概率的 ——\n"
          "偶发失败先重跑一次再下结论；同一任务反复失败才是回归。\n")

    passed = skipped = failed = 0
    total_tokens = 0
    done = 0
    try:
        for task in tasks:
            verdict, turns, tokens, seconds = await run_task(task)
            total_tokens += tokens
            done += 1
            mark = "PASS" if verdict.ok else ("SKIP" if verdict.skip else "FAIL")
            print(f"{mark}  {task.id:<24} {turns} 轮  {tokens:>6,} tk  {seconds:4.1f}s")
            for failure in verdict.failures:
                print(f"     ✗ {failure}")
            if verdict.skip:
                skipped += 1
            elif verdict.ok:
                passed += 1
            else:
                failed += 1
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Ctrl-C 不裸奔：已跑完的结果在上面，汇总后退出
        print(f"\n已中断（Ctrl-C）：完成 {done}/{len(tasks)}，其余未跑")
        return 130

    summary = f"\n{passed}/{len(tasks)} 通过"
    if skipped:
        summary += f" · {skipped} SKIP（数据源抖动，不算分）"
    if failed:
        summary += f" · {failed} FAIL"
    summary += f" · 共 {total_tokens:,} tokens"
    print(summary)
    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="evals", description="Niulai 任务级 eval")
    parser.add_argument("filter", nargs="?", default="", help="只跑 id 含此子串的任务")
    parser.add_argument("--list", action="store_true", help="列出任务清单")
    parser.add_argument("--verbose", action="store_true", help="通过的任务也显示说明")
    args = parser.parse_args()

    if args.list:
        for t in TASKS:
            print(f"{t.id:<24} {t.note}")
        return 0

    try:
        return asyncio.run(main_async(args.filter, args.verbose))
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
