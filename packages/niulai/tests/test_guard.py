"""运行时熔断测试。

这一组测试证明的是"prompt 里的纪律被 runtime 强制执行了"。
它们全都无网络、无 LLM —— 熔断是纯状态机，就该能这样测。
"""

from __future__ import annotations

from niulai.agent.guard import ToolGuard, normalize_arguments
from niulai.llm import ToolCall


def _call(name: str = "stock_quote", args: str = '{"symbol":"600519"}', cid: str = "c1") -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=args)


def test_首次调用放行():
    guard = ToolGuard()
    assert guard.check(_call()) is None


def test_同参数重复调用被拦下():
    guard = ToolGuard()
    first = _call()
    assert guard.check(first) is None
    guard.record(first, ok=True)

    breaker = guard.check(_call(cid="c2"))
    assert breaker is not None
    assert breaker.reason == "duplicate_call"
    # 回灌内容必须告诉他"接下来怎么办"，光说"不行"模型会继续撞
    assert "已经执行过了" in breaker.message
    assert "改变参数" in breaker.message


def test_参数顺序不同视为同一次调用():
    """`{"a":1,"b":2}` 和 `{"b":2,"a":1}` 是同一个请求。

    不做规范化，守卫会被模型随手换的键顺序绕过 —— 那等于没有守卫。
    """
    guard = ToolGuard()
    assert guard.check(_call(args='{"symbol":"600519","period":"daily"}')) is None
    breaker = guard.check(_call(args='{"period":"daily","symbol":"600519"}', cid="c2"))
    assert breaker is not None
    assert breaker.reason == "duplicate_call"


def test_不同参数不算重复():
    guard = ToolGuard()
    assert guard.check(_call(args='{"symbol":"600519"}')) is None
    assert guard.check(_call(args='{"symbol":"000858"}', cid="c2")) is None


def test_连续失败到达阈值后熔断():
    guard = ToolGuard(max_consecutive_failures=2)
    for index in range(2):
        call = _call(args=f'{{"symbol":"bad{index}"}}', cid=f"c{index}")
        assert guard.check(call) is None
        guard.record(call, ok=False)

    breaker = guard.check(_call(args='{"symbol":"bad9"}', cid="c9"))
    assert breaker is not None
    assert breaker.reason == "consecutive_failures"
    assert "连续失败 2 次" in breaker.message
    assert "改用其他工具" in breaker.message


def test_中途成功会清零连续失败():
    """查 5 只股票、中间失败 2 次的正常流程不能被误伤。"""
    guard = ToolGuard(max_consecutive_failures=2)

    a = _call(args='{"symbol":"a"}', cid="c1")
    guard.check(a)
    guard.record(a, ok=False)

    b = _call(args='{"symbol":"b"}', cid="c2")
    guard.check(b)
    guard.record(b, ok=True)  # 成功 → 清零

    c = _call(args='{"symbol":"c"}', cid="c3")
    guard.check(c)
    guard.record(c, ok=False)

    # 只有 1 次连续失败，不该熔断
    assert guard.check(_call(args='{"symbol":"d"}', cid="c4")) is None


def test_被拦下的调用不会被登记为已执行():
    """拦下 ≠ 执行过。否则模型改好参数后仍然被自己的失败记录挡住。"""
    guard = ToolGuard(max_consecutive_failures=1)
    bad = _call(args='{"symbol":"x"}', cid="c1")
    guard.check(bad)
    guard.record(bad, ok=False)

    blocked = _call(args='{"symbol":"y"}', cid="c2")
    assert guard.check(blocked) is not None  # 熔断拦下

    # 熔断状态下再调还是拦（因为 fail 计数没清零），但这是"连续失败"这条规则，
    # 不是"重复调用"那条 —— 两者原因必须能区分
    again = guard.check(_call(args='{"symbol":"y"}', cid="c3"))
    assert again is not None
    assert again.reason == "consecutive_failures"


def test_断线重连时可以把调用撤回():
    guard = ToolGuard()
    call = _call()
    guard.check(call)
    guard.forget(call)
    assert guard.check(_call(cid="c2")) is None


def test_坏参数也能被认成同一个():
    assert normalize_arguments('{"a": 1}') == normalize_arguments('{ "a" : 1 }')
    assert normalize_arguments("{not json}") == "{not json}"
    assert normalize_arguments("") == ""


def test_拦截记录可追溯():
    guard = ToolGuard()
    guard.check(_call())
    guard.record(_call(), ok=True)
    guard.check(_call(cid="c2"))

    assert len(guard.breaks) == 1
    assert guard.breaks[0].name == "stock_quote"
    assert guard.breaks[0].reason == "duplicate_call"
