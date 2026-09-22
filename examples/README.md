# examples

## `demo.jsonl`（待补）

一次**真实运行**的完整轨迹，用来做两件事：

1. README 里的 GIF 用它录制；
2. 别人 clone 下来**不填任何 API Key** 就能看 agent 跑一遍：

```bash
niulai replay examples/demo.jsonl
```

生成方式（需要自己的 Key）：

```bash
niulai ask "茅台现在多少钱" --json > /dev/null     # trace 会自动落到数据目录
# 再把数据目录里的 trace 复制过来
```

注意：trace 里**不含 API Key**（只记模型名与用量），但会包含你的提问原文，
提交前自己过一眼。
