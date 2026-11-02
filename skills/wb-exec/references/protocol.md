# wb-exec 协议

## 任务包（输入）

```json
{
  "goal": "一句话目标：做完之后什么变了",
  "context": "必要的背景：为什么要做、涉及哪个模块、已知坑",
  "inputs": ["src/auth/mw.py", "docs/auth.md"],
  "owned_paths": ["src/auth/", "tests/test_auth_mw.py"],
  "acceptance": ["pytest -q tests/test_auth_mw.py"],
  "notes": "额外约束，例如不要引入新依赖"
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `goal` | 是 | 目标陈述，不写实现步骤。脚本会把它渲染进执行体提示词 |
| `context` | 否 | 背景与已知约束 |
| `inputs` | 否 | 只读参考材料；路径会以 `--add-dir` 追加给执行体 |
| `owned_paths` | 否 | **允许修改的路径白名单**。为空时执行体只允许新建文件。路径分隔符统一用 `/` |
| `acceptance` | 否 | 可执行命令列表；执行体应自己跑一遍并把结果写进 `tests` |
| `notes` | 否 | 兜底约束 |

`owned_paths` 的匹配规则：完全相等，或以该项 + `/` 为前缀。
`src/auth/` 覆盖 `src/auth/mw.py`；`src/auth` 同样覆盖。不在白名单内的改动会被记进 `result.violations`。

## 结果（输出）

```json
{
  "ok": true,
  "job_id": "3f9c1a7b2e04",
  "status": "done",
  "exit_code": 0,
  "elapsed_s": 42.1,
  "result": {
    "summary": "2-4 句，不贴代码",
    "changed_files": ["src/auth/mw.py"],
    "tests": [{"cmd": "pytest -q", "status": "pass", "detail": "12 passed"}],
    "blockers": [],
    "notes": "",
    "artifacts": {"stdout": "...", "stderr": "...", "patch": "..."},
    "changed_files_git": ["src/auth/mw.py"],
    "violations": []
  }
}
```

- `ok` = 状态为 `done` **且** 结构化结论解析成功。`ok=true` 不代表代码正确，仍需主控读 patch、跑验收。
- `changed_files` 来自执行体自述；`changed_files_git` 来自真实 `git diff`。以 `changed_files_git` 为准。
- `violations` 只在有 `owned_paths` 时才可能非空。

## 状态机

`running` → (`done` | `failed` | `timeout` | `cancelled` | `stalled`)

- `stalled` 是派生状态：`exit.json` 未出现且心跳超过 90 秒。进程可能仍在跑，也可能已死；先 `collect` 看输出尾巴。
- `cancel` 写的是标志文件，监视器最多 0.5 秒后响应。

## 目录布局

```
~/.wb-exec/jobs/<job_id>/
  job.json        任务包快照、实际 argv、cwd、worktree、超时
  heartbeat.json  监视器每 5 秒更新
  stdout.jsonl    执行体原始输出（stream-json）
  stderr.log      执行体日志；模型/权限报错通常在这里
  change.patch    真实 diff（git 仓库且开启隔离时）
  result.json     解析后的结构化结论
  exit.json       状态、退出码、耗时
  cancel.request  取消标志
  wt/             独立 worktree（--worktree auto 且为 git 仓库时）
```

`~/.wb-exec` 可用环境变量 `WB_EXEC_HOME` 覆盖。

## 隔离语义

- `--worktree auto`（默认）+ Git 仓库：从派发时的 `HEAD` 新建 `wb-exec/<id8>` 分支与 `wt/` 工作树。**不提交、不推送、不自动合并**，不含用户未提交的改动。
- 非 Git 仓库：原地执行，`isolated=false`，没有 diff 采集。此时 `owned_paths` 就是唯一防线，必须写。
- 清理 worktree 是用户的决定，脚本不自动删除。

## 递归与权限

- 子进程环境固定注入 `WB_SUBAGENT=1` 与 `CROSS_CLI_COLLAB_WORKER=1`；脚本检测到 `WB_SUBAGENT` 即以退出码 3 拒绝派发。
- 子进程实际命令包含 `-p --output-format stream-json --json-schema <schema> -y --allowedTools Read,Write,Edit,Glob,Grep,Bash --add-dir <cwd> --session-id <uuid>`。
- `-y` 会跳过常规权限确认（HIGH/CRITICAL 仍可能询问），因此**目录隔离与 `owned_paths` 是真正的安全边界**，不是可选项。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 执行失败或结论解析失败（`collect` 返回 `ok=false`） |
| 3 | 递归被拒（当前进程已是执行体） |

## 执行体（固定）

每次派发都硬编码 `--model deepseek-v4.1-flash --effort max`（极致）。

- 唯一执行体：WorkBuddy / DeepSeek V4.1 Flash，档位 极致（max）。没有第二个模型，也没有档位选项。
- 模型 ID 取自本机产品配置的解析结果（`main.log` 的 `resolved ids` 含 `deepseek-v4.1-flash`）；产品展示名 "DeepSeek V4.1 Flash" 不能直接当 ID 用。
- CLI 不提供 `--model` / `--effort` 参数入口，传了会被 argparse 拒绝（`unrecognized arguments`），这是刻意的。
- 账号或套餐侧拒绝该模型时：CLI 非零退出，原因写进 `stderr.log`。此时**不做降级、不换模型**，把原始报错交给用户。
- 该设置只作用于被派发的执行体，不影响主控自身会话。

## 会话与续跑

脚本为每个作业生成固定 `--session-id`，记录在 `job.json` 的 `argv` 里。需要让执行体在原有上下文上继续时，用同一 `session-id` 加 `-r` 手工续跑；常规做法是新派一个更小的任务包，而不是续跑一个超时的大包。
