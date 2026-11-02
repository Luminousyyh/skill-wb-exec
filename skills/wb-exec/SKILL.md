---
name: wb-exec
description: 把边界明确的编码/重构/补测任务派发给 WorkBuddy 无头执行体（DeepSeek V4.1 Flash）执行，回收结构化结论与真实 diff。当任务可独立完成、文件范围可枚举、主控只想做规划与审查而不亲自写代码时使用。
whenToUse: 需要把"实现类"子任务外包给另一个 CLI 智能体时；想用便宜的执行体跑量、由自己（Kimi Code）负责规划与审查时；需要独立 worktree 隔离改动时。
---

# wb-exec

你是主控，负责规划、监督与审查。**实现这件事由 WorkBuddy 子进程做，你不亲自写业务代码。**

所有命令都用本 skill 的绝对目录，不要依赖终端当前目录：

```
python <SKILL_DIR>/scripts/wb_exec.py <子命令> ...
```

Windows 上若 `python` 不在 PATH，改用 `py -3` 或本机 Python 绝对路径。

## 0. 先自检（每个会话最多一次）

```
python <SKILL_DIR>/scripts/wb_exec.py doctor
```

返回 JSON：`ok` / `cli`（实际可执行入口）/ `cli_version` / `git` / `is_git_repo` / `state_root`。
`ok=false` 或 `cli_error` 有值 → 停下来，把原文报给用户，不要自己安装或改配置。

## 1. 写任务包（这是你最重要的工作）

任务包是 JSON，字段含义见 `references/protocol.md`，示例见 `assets/task.example.json`。三条硬要求：

1. `goal` 一句话说清"做完之后什么变了"，不写实现步骤。
2. `owned_paths` 必须能枚举。给目录就写 `src/auth/`，给文件就写全路径；**留空意味着执行体只允许新建文件**。
3. `acceptance` 写成可执行命令（`pytest -q tests/test_auth.py`），不要写"确保正确"。

任务包先落盘成文件（例如 `.wb-exec/tasks/001.json`），再派发。不要把任务包塞进命令行参数。

## 2. 派发

长任务（多半是长任务）用后台：

```
python <SKILL_DIR>/scripts/wb_exec.py dispatch --task .wb-exec/tasks/001.json --cwd <repo>
```

短任务可以前台一次拿结果：

```
python <SKILL_DIR>/scripts/wb_exec.py run --task .wb-exec/tasks/001.json --cwd <repo> --timeout 600
```

常用开关：
- `--dry-run`：只打印将要执行的 argv，用于确认路径与参数，不发起调用。
- `--worktree off`：关闭隔离（仅当目录不是 Git 仓库，或你明确接受原地改动）。
- `--timeout`：秒，默认 1800。

**执行体是固定的**：WorkBuddy / DeepSeek V4.1 Flash，推理档位 极致（`max`）。脚本已写死在参数里，不提供 `--model` 或 `--effort`，不需要也不允许另行指定。账号或套餐侧拒绝该模型时直接失败，**不要换别的模型重试**——把 `stderr.log` 报给用户。

## 3. 监督

```
python <SKILL_DIR>/scripts/wb_exec.py status --job <JOB_ID>
```

- `running`：正常，隔 30–60 秒再看，不要秒级轮询。
- `stalled`：心跳超过 90 秒。先 `collect` 看它停在哪里，再决定 `cancel` 后重派（缩小任务包）还是换档重跑。
- `cancel_requested`：取消请求已写入，等待进程退出。

取消：

```
python <SKILL_DIR>/scripts/wb_exec.py cancel --job <JOB_ID>
```

## 4. 回收与审查

```
python <SKILL_DIR>/scripts/wb_exec.py collect --job <JOB_ID> --wait
```

返回结构（节选）：

```json
{
  "ok": true,
  "status": "done",
  "result": {
    "summary": "新增 token 校验中间件并补了 3 个用例",
    "changed_files": ["src/auth/mw.py", "tests/test_mw.py"],
    "tests": [{"cmd": "pytest -q tests/test_mw.py", "status": "pass"}],
    "blockers": []
  },
  "result.artifacts": {"patch": ".../change.patch", "stdout": ".../stdout.jsonl"},
  "result.violations": ["src/billing/x.py"]
}
```

审查纪律：

1. **必须自己读 patch 再判断**：`artifacts.patch` 是真实 diff，`summary` 只是执行体的自述。两者不一致时以 diff 为准。
2. `violations` 非空 = 执行体越界改了不属于它的文件。不要默默接受，也不要自行还原，先报给用户。
3. `tests` 里的 "pass" 不是独立验证。整合后你在主工作区自己跑一遍验收命令。
4. 整合由你做：`git apply --check` → `git apply` → 跑验收 → 汇报。不要执行 `git commit`/`push`，那要用户明确要求。

## 5. 边界（违反会造成事故）

- **不要递归**：执行体已被禁止派发；你自己也不要在 worker 运行期间再派发一个改同一批文件的任务。
- **不要在 WorkBuddy/CodeBuddy 会话内部触发 dispatch**：子进程会与主会话的守护进程撞车，表现为零输出静默挂起（实测 45–300 秒无任何 stdout/stderr）。必须在 Kimi Code 或普通终端里调用本脚本。
- **不要并行改同一文件**：多个 job 的 `owned_paths` 不得重叠，重叠就串行。
- **不要回传全量日志**给用户：只给 `summary` + 关键 `tests` + `violations`。日志留在 `artifacts` 路径里按需查看。
- **不要用 `-y` 手工重跑**：脚本已处理权限参数；绕过脚本等于绕过隔离与越界检查。
- **不要替用户清理 worktree**：`collect` 后目录保留在原处供复核，清理是用户的决定。

## 失败处理

| 现象 | 处置 |
|---|---|
| `exit_code != 0` 且 `blockers` 有内容 | 把 blocker 原样带回规划，缩小或拆分任务包后重派一次 |
| `ok=false, blockers=["无法解析结构化结论"]` | 读 `artifacts.stdout` 尾巴，多为执行体没按 schema 输出；在任务包 `notes` 里加一句"最后一条消息必须是 JSON" |
| `status=timeout` | 任务包太大。拆成 2–3 个 `owned_paths` 不重叠的子包 |
| `status=stalled` | 先 collect，再 cancel；换 `--worktree auto` 重派 |
| `cli_error` | 停下报给用户，不要安装依赖 |
