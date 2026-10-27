# wb-exec

> 把边界明确的实现类任务外包给另一个 CLI 智能体执行，主控只做规划、监督与审查。

`wb-exec` 是一个 Agent Skill（技能包）。它让主控 Agent 把**编码 / 重构 / 补测**这类子任务派发给 **WorkBuddy 无头执行体（DeepSeek V4.1 Flash，推理档位 极致）**，跑完回收**结构化结论**与**真实 diff**。

适用场景：

- 任务边界清晰、可独立完成，文件范围能枚举
- 想用便宜的执行体跑量，主控只负责规划与审查
- 需要独立 worktree 隔离改动，事后自己读 diff 再决定是否整合

不适用：探索性任务、需求还在变、跨模块大规模重构、需要深度架构判断的场景——这些主控自己来。

---

## 核心特性

| 特性 | 说明 |
|---|---|
| **边界白名单** | 任务包里的 `owned_paths` 是允许修改的路径白名单；越界改动会被检出并记入 `result.violations`，而不是被默默接受 |
| **目录隔离** | 默认在 Git 仓库中自动建独立 worktree（`wb-exec/<id8>` 分支），不提交、不推送、不自动合并 |
| **真实 diff 优先** | 执行体的 `summary` 只是自述；`change.patch` 与 `changed_files_git` 来自真实 `git diff`，两者不一致时以 diff 为准 |
| **结构化回收** | 用 `--json-schema` 约束执行体输出，脚本再叠加 git diff 与越界检查，产出可机读的 `result.json` |
| **递归防护** | 子进程注入 `WB_SUBAGENT=1`；脚本检测到该变量即以退出码 3 拒绝派发，避免执行体再派发执行体 |
| **长任务不阻塞** | `dispatch` 后台派发立即返回 `job_id`；`status` 查进度、`collect` 回收，心跳与超时可观测 |
| **零依赖** | 仅用 Python 标准库，Python 3.10+ |

---

## 工作原理

```
主控 Agent ──写任务包 JSON──> wb_exec.py dispatch ──> WorkBuddy 无头子进程
                                                          │
   主控读 patch + 自己跑验收 <── collect（结构化结论+diff）<┘
```

1. 主控把任务包落盘成 JSON（`goal` / `owned_paths` / `acceptance` 等）
2. `dispatch`（后台）或 `run`（前台）发起调用；Git 仓库默认走独立 worktree
3. 执行体干活，输出 stream-json 事件；监视器每 5 秒写心跳
4. `collect` 解析结构化结论，叠加真实 diff 与越界检查后返回
5. **主控必须自己读 patch、跑一遍验收命令，再决定是否整合**（整合动作由主控做，`wb-exec` 不代劳）

---

## 环境要求

- **Python 3.10+**（仅标准库，无需 pip 安装）
- **WorkBuddy / CodeBuddy CLI**（脚本自动探测 `codebuddy` 可执行入口及本机 WorkBuddy 安装包内嵌入口；可用环境变量 `WB_CLI` 显式指定）
- **Git**（可选：用于 worktree 隔离与 diff 采集；非 Git 目录会退化为原地执行）
- 账号 / 套餐需允许使用 `deepseek-v4.1-flash`

先跑自检确认环境：

```bash
python scripts/wb_exec.py doctor
```

返回 JSON：`ok` / `cli` / `cli_version` / `git` / `is_git_repo` / `state_root`。`ok=false` 或 `cli_error` 非空时，停下来把原文交给用户，不要自行安装依赖或改配置。

---

## 安装

把本目录放到你的 Agent 的 skills 目录下，Agent 会读取 `SKILL.md` 的 frontmatter 注册本技能：

```bash
git clone https://github.com/Luminousyyh/skill-wb-exec.git ~/.agents/skills/wb-exec
```

> 本机示例路径为 `~/.agents/skills/wb-exec`；不同 Agent 的 skills 目录可能不同，请按你的 Agent 约定放置。

命令统一使用 **本 Skill 的绝对路径**，不要依赖终端当前目录：

```bash
python <SKILL_DIR>/scripts/wb_exec.py <子命令> ...
```

Windows 上若 `python` 不在 PATH，改用 `py -3` 或本机 Python 绝对路径。

---

## 快速开始

### 1. 写任务包（最重要的一步）

落盘成文件，例如 `.wb-exec/tasks/001.json`：

```json
{
  "goal": "为 /login 接口补上失败次数限流：同一账号 5 分钟内连续失败 5 次后锁定 10 分钟，并返回 429",
  "context": "限流中间件放在 src/auth/mw.py；已有 Redis 客户端封装在 src/common/redis.py，直接复用，不要新建连接池。",
  "inputs": ["src/auth/mw.py", "src/common/redis.py", "tests/test_auth.py"],
  "owned_paths": ["src/auth/", "tests/test_auth.py"],
  "acceptance": ["pytest -q tests/test_auth.py"],
  "notes": "不要引入新依赖；失败计数用 Redis INCR + EXPIRE，键名 auth:fail:<account>。"
}
```

三条硬要求：

1. `goal` 一句话说清「做完之后什么变了」，**不写实现步骤**
2. `owned_paths` 必须能枚举；**留空意味着只允许新建文件**
3. `acceptance` 写成可执行命令，不要写「确保正确」

### 2. 确认将要执行的调用（可选）

```bash
python <SKILL_DIR>/scripts/wb_exec.py dispatch --task .wb-exec/tasks/001.json --cwd <repo> --dry-run
```

`--dry-run` 只打印 argv，不发起调用，用于核对路径与参数。

### 3. 派发

长任务（多半是长任务）用后台：

```bash
python <SKILL_DIR>/scripts/wb_exec.py dispatch --task .wb-exec/tasks/001.json --cwd <repo>
```

短任务可以前台上限等待：

```bash
python <SKILL_DIR>/scripts/wb_exec.py run --task .wb-exec/tasks/001.json --cwd <repo> --timeout 600
```

### 4. 监督

```bash
python <SKILL_DIR>/scripts/wb_exec.py status --job <JOB_ID>
```

- `running`：正常，隔 30–60 秒再看，不要秒级轮询
- `stalled`：心跳超过 90 秒。先 `collect` 看它停在哪里，再决定 `cancel` 后重派（缩小任务包）
- `cancel_requested`：取消请求已写入，等待进程退出

```bash
python <SKILL_DIR>/scripts/wb_exec.py cancel --job <JOB_ID>
```

### 5. 回收与审查

```bash
python <SKILL_DIR>/scripts/wb_exec.py collect --job <JOB_ID> --wait
```

返回结构（节选）：

```json
{
  "ok": true,
  "job_id": "3f9c1a7b2e04",
  "status": "done",
  "exit_code": 0,
  "elapsed_s": 42.1,
  "result": {
    "summary": "新增 token 校验中间件并补了 3 个用例",
    "changed_files": ["src/auth/mw.py", "tests/test_mw.py"],
    "tests": [{"cmd": "pytest -q tests/test_mw.py", "status": "pass", "detail": "12 passed"}],
    "blockers": [],
    "artifacts": {"patch": ".../change.patch", "stdout": ".../stdout.jsonl", "stderr": ".../stderr.log"},
    "changed_files_git": ["src/auth/mw.py"],
    "violations": []
  }
}
```

**审查纪律（缺一不可）**：

1. **必须自己读 patch 再判断**：`artifacts.patch` 是真实 diff，`summary` 只是执行体的自述；两者不一致时以 diff 为准
2. `violations` 非空 = 执行体越界改了不属于它的文件。不要默默接受，也不要自行还原，先报给用户
3. `tests` 里的 `pass` **不是独立验证**；整合后你在主工作区自己跑一遍验收命令
4. 整合由主控做：`git apply --check` → `git apply` → 跑验收 → 汇报。**不要执行 `git commit` / `git push`**，那要用户明确要求

---

## 命令参考

| 子命令 | 参数 | 作用 |
|---|---|---|
| `doctor` | `[--cwd]` | 自检：CLI、git、环境、状态目录 |
| `dispatch` | `--task` `[--cwd]` `[--timeout 1800]` `[--worktree auto\|off]` `[--dry-run]` | 后台派发，立即返回 `job_id` |
| `run` | 同上（无 `--dry-run`） | 前台执行并等待回收 |
| `status` | `--job` | 查询作业状态 |
| `collect` | `--job` `[--wait]` `[--wait-timeout 1800]` | 回收结果，`--wait` 阻塞至结束 |
| `cancel` | `--job` | 请求取消（写标志文件，监视器 ≤0.5 秒响应） |
| `jobs` | `[--limit 10]` | 列出最近作业 |

通用说明：

- `--timeout` 单位秒，默认 `1800`
- `--worktree auto`（默认）：Git 仓库自动建独立 worktree；`off`：原地执行
- **执行体是固定的**：WorkBuddy / DeepSeek V4.1 Flash，档位 极致（`max`），已硬编码进脚本。CLI **不提供** `--model` / `--effort`（传了会被 argparse 拒绝，这是刻意的）。账号拒绝该模型时直接失败，**不要换别的模型重试**，把 `stderr.log` 交给用户

---

## 任务包协议

| 字段 | 必填 | 说明 |
|---|---|---|
| `goal` | 是 | 目标陈述，不写实现步骤；脚本会渲染进执行体提示词 |
| `context` | 否 | 背景与已知约束 |
| `inputs` | 否 | 只读参考材料；路径以 `--add-dir` 追加给执行体 |
| `owned_paths` | 否 | **允许修改的路径白名单**；为空时只允许新建文件。分隔符统一 `/` |
| `acceptance` | 否 | 可执行命令列表；执行体应自己跑一遍并把结果写进 `tests` |
| `notes` | 否 | 兜底约束 |

`owned_paths` 匹配规则：**完全相等，或以该项 + `/` 为前缀**。`src/auth/` 覆盖 `src/auth/mw.py`；`src/auth` 同样覆盖。不在白名单内的改动会被记进 `result.violations`。

完整字段与状态机见 [references/protocol.md](references/protocol.md)，可直接套用的模板见 [assets/task.example.json](assets/task.example.json)。

---

## 状态与失败处理

状态机：`running` → `done` \| `failed` \| `timeout` \| `cancelled` \| `stalled`

`stalled` 是派生状态（`exit.json` 未出现且心跳超过 90 秒），进程可能仍在跑，也可能已死——先 `collect` 看输出尾巴。

| 现象 | 处置 |
|---|---|
| `exit_code != 0` 且 `blockers` 有内容 | 把 blocker 原样带回规划，缩小或拆分任务包后重派**一次** |
| `ok=false, blockers=["无法解析结构化结论"]` | 读 `artifacts.stdout` 尾巴，多为执行体没按 schema 输出；在 `notes` 里加「最后一条消息必须是 JSON」 |
| `status=timeout` | 任务包太大。拆成 2–3 个 `owned_paths` 不重叠的子包 |
| `status=stalled` | 先 `collect`，再 `cancel`；换 `--worktree auto` 重派 |
| `cli_error` | 停下报给用户，不要安装依赖 |

退出码：

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 执行失败或结论解析失败（`collect` 返回 `ok=false`） |
| 3 | 递归被拒（当前进程已是执行体） |

---

## 安全边界（违反会造成事故）

- **目录隔离与 `owned_paths` 是真正的安全边界，不是可选项**：子进程命令包含 `-y`，会跳过常规权限确认（HIGH/CRITICAL 仍可能询问），因此必须靠工作目录隔离 + 白名单约束
- **不要递归**：执行体已被禁止派发；主控也不要在 worker 运行期间再派发一个改同一批文件的任务
- **不要在 WorkBuddy / CodeBuddy 会话内部触发 `dispatch`**：子进程会与主会话的守护进程撞车，表现为零输出静默挂起（实测 45–300 秒无任何 stdout/stderr）。必须在 Kimi Code 或普通终端里调用本脚本
- **不要并行改同一文件**：多个 job 的 `owned_paths` 不得重叠，重叠就串行
- **不要回传全量日志**给用户：只给 `summary` + 关键 `tests` + `violations`，日志留在 `artifacts` 路径里按需查看
- **不要用 `-y` 手工重跑**：脚本已处理权限参数；绕过脚本等于绕过隔离与越界检查
- **不要替用户清理 worktree**：`collect` 后目录保留在原处供复核，清理是用户的决定
- **非 Git 目录原地执行**（`isolated=false`，无 diff 采集）时，`owned_paths` 是唯一防线，**必须写**

---

## 状态目录布局

```
~/.wb-exec/jobs/<job_id>/
  job.json        任务包快照、实际 argv、cwd、worktree、超时
  heartbeat.json  监视器每 5 秒更新
  stdout.jsonl    执行体原始输出（stream-json）
  stderr.log      执行体日志；模型 / 权限报错通常在这里
  change.patch    真实 diff（Git 仓库且开启隔离时）
  result.json     解析后的结构化结论
  exit.json       状态、退出码、耗时
  cancel.request  取消标志
  wt/             独立 worktree（--worktree auto 且为 Git 仓库时）
```

状态根目录可用环境变量 `WB_EXEC_HOME` 覆盖。

**隔离语义**：`--worktree auto`（默认）+ Git 仓库 → 从派发时的 `HEAD` 新建 `wb-exec/<id8>` 分支与 `wt/` 工作树；**不提交、不推送、不自动合并**，也**不含用户未提交的改动**。清理 worktree 是用户的决定，脚本不自动删除。

---

## 开发与测试

测试用假 CLI 打桩，**不需要安装 WorkBuddy 即可离线运行**：

```bash
python -m unittest discover -s tests -v
# 或
python tests/test_wb_exec.py
```

覆盖点：WorkBuddy 环境变量注入、独立 CLI 入口优先于内嵌入口、原生错误结果不被误报为成功、纯文本回复不算结构化成功。

## 项目结构

```
wb-exec/
├── SKILL.md                  # Skill 主说明：自检、任务包、派发、监督、回收、边界
├── assets/
│   └── task.example.json     # 任务包模板
├── references/
│   └── protocol.md           # 任务包字段、结果结构、状态机、隔离与退出码
├── scripts/
│   └── wb_exec.py            # 全部子命令实现（仅标准库）
└── tests/
    ├── fake_cli.py           # 打桩用的假 CLI
    └── test_wb_exec.py       # 单元测试
```

---

## License

本项目**尚未指定开源许可证**。在明确选择许可证之前，默认保留所有权利（all rights reserved）。如需他人可自由使用，建议补充 MIT 或 Apache-2.0。