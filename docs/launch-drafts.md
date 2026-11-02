# Launch drafts

Ready-to-post copy for each channel. Everything here is a claim about the code in
this repository; if you change the code, change the numbers in here too.

Canonical links to reuse:

- Repo: <https://github.com/Luminousyyh/skill-wb-exec>
- Release: <https://github.com/Luminousyyh/skill-wb-exec/releases/tag/v1.2.1>
- Figure 1: <https://raw.githubusercontent.com/Luminousyyh/skill-wb-exec/main/docs/figures/fig1-architecture.png>
- Figure 4: <https://raw.githubusercontent.com/Luminousyyh/skill-wb-exec/main/docs/figures/fig4-isolation.png>

---

## Hacker News — Show HN

**Title:** Show HN: wb-exec – outsource bounded coding tasks to a headless CLI agent

**Body:**

I kept hitting the same failure mode: I'd hand a big agent a well-scoped task,
it would wander outside the files I cared about, and I'd spend as long reviewing
the mess as I would have spent writing the change.

So I split the roles. The host agent plans and reviews; a separate headless CLI
process does the implementation inside a disposable worktree. `wb-exec` is the
glue plus the paperwork.

What it actually enforces, which is the part I care about:

- **A writable-path whitelist.** `owned_paths` in the task package. Anything
  touched outside it lands in `result.violations` instead of being quietly
  accepted. An empty list means "new files only".
- **A git worktree, not your working tree.** Branch `wb-exec/<id8>`, created from
  `HEAD` at dispatch. No commit, no push, no auto-merge, and none of your
  uncommitted changes come along.
- **The diff is the truth.** The executor's `summary` is its own account of what
  it did. `changed_files_git` comes from a real `git diff`. When they disagree,
  the diff wins — that is the whole reason both exist.
- **No recursion.** The child gets `WB_SUBAGENT=1`; the script refuses to
  dispatch when it sees that. An executor cannot spawn another executor.

The design decision people push back on: `wb-exec` never integrates for you. It
stops at "here is the patch, here are the violations". You run `git apply
--check`, `git apply`, then your own acceptance commands. I would rather look at
a diff than trust a summary.

Default executor is WorkBuddy/CodeBuddy pinned to `deepseek-v4.1-flash` at effort
`max`. v1.1.0 made that pluggable — `--executor codex|claude|aider|custom` — but
I left the default byte-identical, because changing someone's request under them
is how you lose trust.

Python 3.10+, standard library only, no build step. MIT.

It is packaged as a DeepSeek Harness plugin bundle (`dsh.bundle`), and the skill
half works standalone if you just want the script.

Honest limits: this is for tasks with a clear boundary. It is not an
architecture tool and it will not help you decide *what* to build. The executor
has to be installed; without it the plugin loads but does nothing, and `doctor`
says so rather than failing mysteriously.

<https://github.com/Luminousyyh/skill-wb-exec>

---

## Reddit — r/LocalLLaMA

**Title:** I gave my coding agent a disposable worktree and a file whitelist — and review time dropped more than generation time did

**Body:**

Sharing a pattern rather than a product, because the pattern is the useful part.

**The problem.** A capable agent with a vague boundary is worse than a weak agent
with a tight one. My failures were never "it couldn't write the code". They were
"it also rewrote three files I didn't ask about, and now I have to audit all of
it".

**The setup.** Two roles, two processes:

1. The host plans, writes a JSON task package, and reviews afterwards.
2. A headless CLI agent implements it in a `git worktree` on a throwaway branch.

The task package is deliberately boring: `goal`, `context`, `owned_paths`,
`acceptance`. The two rules that did the most work:

- `owned_paths` is a whitelist, and out-of-scope writes are *reported*, not
  reverted behind my back. Silent reverts would hide the fact that the model
  misunderstood the task.
- `acceptance` holds executable commands, not prose. "Make sure it works" is not
  a test.

**What surprised me.** The time saving is smaller than the *review* saving. I
still read every diff. But I read a diff that is small, scoped, and accompanied
by a list of anything that strayed — instead of a 40-file mystery.

**What I got wrong first.** I originally let it merge on success. That was the
bug. Now the pipeline stops at "patch ready" and a human decides. Slower, and I
would not go back.

Code if you want it: <https://github.com/Luminousyyh/skill-wb-exec> — Python stdlib
only, MIT, and the executor backend is pluggable (WorkBuddy by default, also
codex/claude/aider/custom). The diagrams in the README are the real design, not
decoration.

Happy to talk about where this breaks — it breaks on exploratory work, and I
have not found a good fix for that.

---

## X / Twitter — thread

**1/**
Most agent failures I hit aren't "couldn't write the code".
They're "wrote the code, plus three files I never asked about".

So I stopped letting one agent do both jobs. 🧵

**2/**
New split:
• host agent plans + reviews
• headless CLI agent implements, in a disposable git worktree

The host never merges. It stops at "here's the patch".

**3/**
Three guards do the real work:

`owned_paths` — a writable-path whitelist. Out-of-scope writes are *reported*, never silently reverted.

`git worktree` on a throwaway branch — your tree is untouched.

Real `git diff` — not the agent's summary of what it did.

**4/**
The rule I keep having to defend: when the summary and the diff disagree, **the diff wins**.

That's why both are in the result. An agent grading its own homework is not a check.

**5/**
v1.1.0 made the executor pluggable: `--executor codex|claude|aider|custom`.

Default stayed byte-identical. Changing someone's request under them is how you lose trust in a tool.

**6/**
Python 3.10+, stdlib only, no build step, MIT.
Also ships as a DeepSeek Harness plugin bundle.

<https://github.com/Luminousyyh/skill-wb-exec>

Honest limit: great for bounded tasks, useless for "figure out what to build".

---

## V2EX — 分享创造

**标题：** 给编码 Agent 加了一个一次性 worktree 和文件白名单，审查时间比生成时间省得多

**正文：**

分享一个模式，不是推产品，因为有用的部分是模式本身。

**问题。** 能力强的 Agent 配上模糊的边界，比能力弱但边界清晰的更糟。我遇到的失败几乎从来不是"它写不出代码"，而是"它顺手改了三个我没让它动的文件，现在我得把全部改动重新审一遍"。

**做法。** 两个角色、两个进程：

1. 主控负责规划，写一个 JSON 任务包，事后审查。
2. 无头 CLI Agent 在一个一次性 `git worktree` 里实现。

任务包刻意做得很无聊：`goal` / `context` / `owned_paths` / `acceptance`。真正起作用的是两条：

- `owned_paths` 是白名单，越界改动会被**报出来**，而不是背着我自动还原 —— 静默还原会掩盖"模型理解错了任务"这个事实。
- `acceptance` 放可执行命令，不写散文。"确保正确"不是测试。

**意外的地方。** 省下来的时间主要在**审查**，不在生成。我照样读每一行 diff，但读的是一个范围清晰、附带越界清单的小 diff，而不是四十个文件的谜案。

**一开始做错的地方。** 最初我让它在成功时自动合并。那就是 bug。现在流水线停在"补丁就绪"，由人决定。慢一点，但我不打算改回去。

代码在这儿：<https://github.com/Luminousyyh/skill-wb-exec> —— 纯 Python 标准库、MIT、执行体可插拔（默认 WorkBuddy，也能接 codex/claude/aider/自定义）。README 里那几张图是真实设计，不是装饰。

边界也说清楚：探索性任务上它不好用，这一点我还没找到好办法。

---

## 掘金 / 知乎 — 技术实现向

**标题：** 把编码任务外包给另一个 AI 时，我如何保证它不乱改文件

**正文：**

（正文用上面 V2EX 版扩写，补三段：`owned_paths` 的前缀匹配规则与 `violations` 的产生方式；worktree 从派发时的 `HEAD` 建立、不含未提交改动；`WB_SUBAGENT=1` 递归防护的动机。配 Figure 4。）

---

## dev.to

**Title:** Guardrails for delegating code changes to another agent

**Body:** (English long form of the V2EX piece, with Figure 1 and Figure 4 embedded
via their raw URLs, and a section on why the default backend stayed
byte-identical across a refactor.)

---

## 发布前检查

- [ ] 仓库是公开的，README 第一屏能看懂是做什么的
- [ ] Release v1.2.1 存在且安装命令可用
- [ ] 四张图在 README 里能正常渲染
- [ ] 描述里的数字与代码一致（44 个测试、5 个后端、429 KB 包）
- [ ] 不要在任何平台写"求 star"
