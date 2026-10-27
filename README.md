# wb-exec

把边界明确的编码/重构/补测任务派发给 WorkBuddy 无头执行体（DeepSeek V4.1 Flash）执行，回收结构化结论与真实 diff 的 Kimi Code Skill。

适用场景：任务可独立完成、文件范围可枚举、主控（Kimi Code）只想做规划与审查而不亲自写代码时。

## 目录结构

```
wb-exec/
├── SKILL.md                  # Skill 主说明：自检、任务包、派发、监督、回收、边界
├── assets/
│   └── task.example.json     # 任务包示例
├── references/
│   └── protocol.md           # 任务包字段协议
├── scripts/
│   └── wb_exec.py            # 派发/监督/回收脚本（doctor/dispatch/run/status/cancel/collect）
└── tests/
    ├── fake_cli.py           # 测试用假 CLI
    └── test_wb_exec.py       # 单元测试
```

## 用法

由 Kimi Code 等主控 Agent 加载本 Skill 后，按其指示调用脚本：

```bash
python scripts/wb_exec.py doctor                                    # 会话级自检
python scripts/wb_exec.py dispatch --task <task.json> --cwd <repo>  # 后台派发
python scripts/wb_exec.py status  --job <JOB_ID>                    # 监督
python scripts/wb_exec.py collect --job <JOB_ID> --wait             # 回收结果
```

任务包字段协议见 [references/protocol.md](references/protocol.md)，示例见 [assets/task.example.json](assets/task.example.json)。完整纪律（审查、边界、失败处理）见 [SKILL.md](SKILL.md)。
