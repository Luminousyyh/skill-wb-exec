#!/usr/bin/env python3
"""wb-exec: 把边界明确的任务派发给 WorkBuddy 无头子进程，并回收结构化结果。

设计要点：
- 只依赖标准库，Python 3.10+。
- 总是用 argv 数组调用 CLI，不经过 shell，避免 Windows 下 .cmd 包装与中文路径编码问题。
- 递归防护：子进程带 WB_SUBAGENT=1，脚本自身检测到该变量即拒绝派发。
- 隔离：Git 仓库自动建独立 worktree；改动越出 owned_paths 时只在结果里报警，不擅自还原。
- 结构化回收：--json-schema 约束执行体输出，再叠加真实的 git diff 与越界检查。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

SCHEMA_VERSION = 1
# Version of the collect envelope's shape. Bumped only when the shape changes,
# so a host written against an older release can tell what it is reading.
PROTOCOL_VERSION = 1
STATE_ROOT = Path(os.environ.get("WB_EXEC_HOME") or (Path.home() / ".wb-exec"))
DEFAULT_TIMEOUT = 1800
HEARTBEAT_STALE = 90
# Cap on a single job's captured output. The prompt asks the executor not to
# print long logs, but that is prose addressed to a model on a disk shared with
# the user's machine: a looping executor can otherwise fill it while the job
# looks healthy. Overridable for a deliberate, large run.
LOG_CAP_BYTES = int(os.environ.get("WB_EXEC_LOG_CAP") or 64 * 1024 * 1024)

EXECUTOR_MODEL = "deepseek-v4.1-flash"
EXECUTOR_EFFORT = "max"

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "做了什么，2-4 句，不要贴代码"},
        "changed_files": {"type": "array", "items": {"type": "string"}},
        "tests": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string"},
                    "status": {"type": "string", "enum": ["pass", "fail", "skipped"]},
                    "detail": {"type": "string"},
                },
                "required": ["cmd", "status"],
            },
        },
        "blockers": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
    "required": ["summary", "changed_files", "tests"],
}


class CliNotFound(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 执行体后端
# ---------------------------------------------------------------------------
# 每个后端只描述"怎么把一次任务变成一个 argv 调用"，其余流程（隔离、
# 心跳、结构化回收、越界检查）完全不区分后端。workbuddy 是默认值，
# 行为与旧版逐字节一致；其余后端为可选，需要用户自己指向可执行文件。

EXECUTOR_BACKENDS = ("workbuddy", "codex", "claude", "aider", "custom")
DEFAULT_EXECUTOR = "workbuddy"


def _backend_env() -> str:
    return (os.environ.get("WB_EXECUTOR") or DEFAULT_EXECUTOR).strip().lower()


def _custom_argv_template() -> list[str]:
    """WB_CLI 已经承担自定义入口；这里只读 WB_CLI_ARGV 作为额外参数模板。

    模板中的占位符在组装 argv 时展开：
      {prompt} {cwd} {schema} {session} {model} {effort}
    """
    raw = os.environ.get("WB_CLI_ARGV")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001
        return []
    return [str(x) for x in parsed] if isinstance(parsed, list) else []


def _utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 定位 CLI
# ---------------------------------------------------------------------------

def _candidate_entries() -> list[list[str]]:
    """返回若干候选 argv 前缀（不含子命令参数）。"""
    cands: list[list[str]] = []
    override = os.environ.get("WB_CLI")
    if override:
        raw = override.strip()
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list) and parsed:
                    cands.append([str(x) for x in parsed])
            except Exception:  # noqa: BLE001
                pass
        else:
            cands.append([raw])

    node_cands = []
    for name in ("node", "node.exe"):
        found = shutil.which(name)
        if found:
            node_cands.append(found)
            break
    managed = Path.home() / ".workbuddy" / "binaries" / "node"
    if managed.is_dir():
        for ver in sorted(managed.glob("versions/*"), reverse=True):
            for exe in ("node.exe", "node"):
                if (ver / exe).exists():
                    node_cands.append(str(ver / exe))
                    break

    for name in ("codebuddy", "cbc"):
        found = shutil.which(name)
        if found:
            package_entry = (Path(found).parent / "node_modules" / "@tencent-ai"
                             / "codebuddy-code" / "bin" / "codebuddy")
            if package_entry.is_file():
                for node in node_cands:
                    cands.append([node, str(package_entry)])
            cands.append([found])

    roots = []
    for env in ("LOCALAPPDATA", "ProgramFiles", "ProgramW6432"):
        base = os.environ.get(env)
        if base:
            roots.append(Path(base) / "Programs" / "WorkBuddy")
            roots.append(Path(base) / "WorkBuddy")
    roots.append(Path.home() / "AppData" / "Local" / "Programs" / "WorkBuddy")

    for root in roots:
        entry = root / "resources" / "app.asar.unpacked" / "cli" / "bin" / "codebuddy"
        if entry.exists():
            for node in node_cands:
                cands.append([node, str(entry)])
            cands.append([str(entry)])
    return cands


def find_cli(backend: str = DEFAULT_EXECUTOR) -> list[str]:
    candidates = _candidate_entries() if backend == "workbuddy" else _candidate_entries_for(backend)
    for cand in candidates:
        try:
            probe = subprocess.run(
                cand + ["--version"],
                capture_output=True,
                timeout=60,
                env={**os.environ, "CI": "1"},
            )
        except Exception:
            continue
        text = (probe.stdout or b"").decode("utf-8", "replace")
        if probe.returncode == 0 and any(ch.isdigit() for ch in text):
            return cand
    if backend == "workbuddy":
        raise CliNotFound(
            "找不到可用的 WorkBuddy CLI。请设置 WB_CLI 指向 codebuddy 入口，"
            "或确认 WorkBuddy 已安装且 CLI 可用。"
        )
    raise CliNotFound(
        f"找不到可用的 {backend} CLI。请设置 WB_CLI 指向它的可执行入口，"
        f"或用 --executor workbuddy 回到默认执行体。"
    )


def _candidate_entries_for(backend: str) -> list[list[str]]:
    """workbuddy 之外的入口探测：只认 WB_CLI，再从 PATH 猜常见名字。

    这些 CLI 的参数形态差异很大，所以只负责"找到可执行文件"，
    真正的参数拼装交给 build_executor_argv()。
    """
    cands: list[list[str]] = []
    override = os.environ.get("WB_CLI")
    if override:
        raw = override.strip()
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list) and parsed:
                    cands.append([str(x) for x in parsed])
            except Exception:  # noqa: BLE001
                pass
        else:
            cands.append([raw])
    for name in {
        "codex": ("codex",),
        "claude": ("claude", "claude-code"),
        "aider": ("aider",),
        "custom": (),
    }.get(backend, ()):
        found = shutil.which(name)
        if found:
            cands.append([found])
    return cands


def build_executor_argv(backend: str, cli: list[str], prompt: str, run_cwd: Path,
                        schema: str, refs: list[str]) -> list[str]:
    """按后端拼装 argv。

    workbuddy 保持与旧版完全一致的参数序列，其余后端遵循各自 CLI 的
    常见无头调用约定；custom 完全由 WB_CLI_ARGV 模板决定。
    """
    session = str(uuid.uuid4())
    if backend == "workbuddy":
        # The prompt is NOT placed on the command line for this backend. It goes
        # in on stdin, and `-p` acts as the "read the prompt from stdin" switch.
        # Measured against codebuddy 2.174.0: a multi-line argv prompt is
        # truncated to its first line ("Line one AAA.\nLine two BBB." arrives as
        # "AAA."), while the same text piped on stdin arrives intact, and a long
        # single-line argv prompt works fine. build_prompt() always returns
        # multi-line text, so on argv this backend silently received an empty
        # task body.
        argv = cli + [
            "-p",
            "--output-format", "stream-json",
            "--json-schema", schema,
            "-y",
            # `-y` is documented as "Bypass permission prompts (HIGH/CRITICAL
            # still ask)", which is not enough on its own: in a non-interactive
            # `-p` run there is no prompt to answer, so Write/Edit/Bash were
            # refused and every task reported blockers without touching a file.
            # --permission-mode is what actually opens the tools for this run.
            # Measured: with it a write lands on disk; without it the executor
            # returns "本会话被设置为只读" and changes nothing.
            "--permission-mode", "bypassPermissions",
            "--allowedTools", "Read,Write,Edit,Glob,Grep,Bash",
            "--add-dir", str(run_cwd),
            "--session-id", session,
        ]
        for ref in refs:
            argv.extend(["--add-dir", ref])
        # 执行体固定为 WorkBuddy / DeepSeek V4.1 Flash，推理档位固定 极致(max)。
        # 不提供其他模型或档位，也不做自动降级。
        argv.extend(["--model", EXECUTOR_MODEL, "--effort", EXECUTOR_EFFORT])
        return argv

    if backend == "codex":
        argv = cli + ["exec", "--json", "--skip-git-repo-check", "-C", str(run_cwd)]
        for ref in refs:
            argv.extend(["--add-dir", ref])
        argv.append(prompt)
        return argv

    if backend == "claude":
        argv = cli + [
            "-p", prompt,
            "--output-format", "stream-json",
            "--json-schema", schema,
            "--permission-mode", "acceptEdits",
            "--add-dir", str(run_cwd),
        ]
        for ref in refs:
            argv.extend(["--add-dir", ref])
        return argv

    if backend == "aider":
        argv = cli + ["--yes-always", "--no-auto-commits", "--message", prompt]
        for ref in refs:
            argv.extend(["--read", ref])
        return argv

    # custom：模板里出现的占位符按本次作业展开。
    substitutions = {
        "{prompt}": prompt,
        "{cwd}": str(run_cwd),
        "{schema}": schema,
        "{session}": session,
        "{model}": EXECUTOR_MODEL,
        "{effort}": EXECUTOR_EFFORT,
    }
    argv = list(cli)
    for part in _custom_argv_template():
        for key, value in substitutions.items():
            part = part.replace(key, value)
        argv.append(part)
    return argv


def cli_version(argv: list[str]) -> str:
    try:
        out = subprocess.run(argv + ["--version"], capture_output=True, timeout=60)
        lines = (out.stdout or b"").decode("utf-8", "replace").strip().splitlines()
        for line in lines:
            if any(ch.isdigit() for ch in line):
                return line.strip()
    except Exception as exc:  # noqa: BLE001
        return f"unknown ({exc})"
    return "unknown"


# ---------------------------------------------------------------------------
# 任务包
# ---------------------------------------------------------------------------

def load_task(path: Path) -> dict:
    task = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(task, dict):
        raise SystemExit("任务包必须是 JSON 对象")
    if not task.get("goal"):
        raise SystemExit("任务包缺少 goal 字段")
    task.setdefault("owned_paths", [])
    task.setdefault("inputs", [])
    task.setdefault("acceptance", [])
    task.setdefault("context", "")
    task.setdefault("notes", "")
    if not isinstance(task["owned_paths"], list):
        raise SystemExit("owned_paths 必须是数组")
    return task


def build_prompt(task: dict, cwd: Path) -> str:
    def bullets(items: list[str], empty: str = "（未指定）") -> str:
        return "\n".join(f"- {i}" for i in items) if items else empty

    return f"""你是执行体，不是规划者。只完成下面这一个任务包，不要扩大范围。

## 目标
{task['goal']}

## 上下文
{task['context'] or '（无）'}

## 输入
{bullets([str(x) for x in task['inputs']])}

## 工作目录
{cwd}

## 允许修改的路径（不得越界）
{bullets([str(x) for x in task['owned_paths']], '（未限定：只允许新建文件，且不得改动已有文件）')}

## 完成标准
{bullets([str(x) for x in task['acceptance']])}

## 硬约束
1. 不得修改「允许修改的路径」之外的任何文件。确实必须时，停下并把原因写进 blockers。
2. 不得派发子智能体、不得调用其他 CLI、不得发起跨智能体协作（防止递归）。
3. 不得执行 git commit / git push / git reset --hard，不得删除工作区文件。
4. 不安装全局依赖，不改动系统配置。
5. 最后一条消息必须是符合给定 JSON Schema 的结构化结果；不要输出长日志、不要贴完整代码。

## 备注
{task['notes'] or '（无）'}
"""


# ---------------------------------------------------------------------------
# 作业存储
# ---------------------------------------------------------------------------

def job_dir(job_id: str) -> Path:
    return STATE_ROOT / "jobs" / job_id


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def job_status(job_id: str) -> dict:
    jd = job_dir(job_id)
    meta = read_json(jd / "job.json")
    if meta is None:
        raise SystemExit(f"作业不存在: {job_id}")
    if (jd / "exit.json").exists():
        exit_info = read_json(jd / "exit.json") or {}
        meta["status"] = exit_info.get("status", meta.get("status", "unknown"))
        meta["exit_code"] = exit_info.get("exit_code")
        meta["ended_at"] = exit_info.get("ended_at")
        meta["elapsed_s"] = exit_info.get("elapsed_s")
    else:
        hb = read_json(jd / "heartbeat.json") or {}
        beat = hb.get("ts", meta.get("started_at", 0))
        meta["status"] = "running" if time.time() - beat < HEARTBEAT_STALE else "stalled"
        meta["elapsed_s"] = round(time.time() - meta.get("started_at", time.time()), 1)
    if (jd / "cancel.request").exists():
        meta["cancel_requested"] = True
    return meta


# ---------------------------------------------------------------------------
# git 辅助
# ---------------------------------------------------------------------------

def git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def is_git_repo(path: Path) -> bool:
    if shutil.which("git") is None:
        return False
    try:
        return git(["rev-parse", "--is-inside-work-tree"], path).stdout.strip() == "true"
    except Exception:  # noqa: BLE001
        return False


def make_worktree(repo: Path, jd: Path, job_id: str) -> Path | None:
    wt = jd / "wt"
    branch = f"wb-exec/{job_id[:8]}"
    proc = git(["worktree", "add", "-b", branch, str(wt), "HEAD"], repo)
    if proc.returncode != 0:
        return None
    return wt


def capture_diff(wt: Path, jd: Path) -> dict:
    git(["add", "-A"], wt)
    diff = subprocess.run(
        ["git", "diff", "--cached", "--binary"], cwd=str(wt),
        capture_output=True,
    )
    patch = jd / "change.patch"
    patch.write_bytes(diff.stdout or b"")
    names = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=str(wt),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return {
        "patch": str(patch),
        "patch_bytes": len(diff.stdout or b""),
        "changed": [n for n in names.stdout.splitlines() if n.strip()],
    }


def violations(changed: list[str], owned: list[str]) -> list[str]:
    if not owned:
        return []
    patterns = [o.strip().rstrip("/") for o in owned if o.strip()]
    bad = []
    for name in changed:
        name = name.replace("\\", "/")
        if not any(name == p or name.startswith(p + "/") for p in patterns):
            bad.append(name)
    return bad


# ---------------------------------------------------------------------------
# 并发锁：同一批文件不允许被两个作业同时修改
# ---------------------------------------------------------------------------

LOCK_NAME = "dispatch.lock"


def _overlaps(a: list[str], b: list[str]) -> bool:
    """两条 owned_paths 是否可能写到同一个文件。

    Absent whitelists are treated as "could touch anything", because an empty
    list means "new files only" — and a new file could land anywhere.
    """
    def norm(p: str) -> str:
        return p.replace("\\", "/").strip().rstrip("/")

    left = [norm(x) for x in a if x and x.strip()]
    right = [norm(x) for x in b if x and x.strip()]
    if not left or not right:
        return True

    for x in left:
        for y in right:
            if x == y or x.startswith(y + "/") or y.startswith(x + "/"):
                return True
    return False


class LockBusy(RuntimeError):
    def __init__(self, holders: list[dict]):
        self.holders = holders
        super().__init__("owned_paths 与正在运行的作业重叠")


def _lock_path() -> Path:
    return STATE_ROOT / LOCK_NAME


def _acquire_claim(owned_paths: list[str], job_id: str) -> list[dict]:
    """Add a claim, or raise LockBusy naming the overlapping jobs."""
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    guard = path.with_suffix(".guard")
    for _ in range(50):
        try:
            fd = os.open(str(guard), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            time.sleep(0.1)
    else:
        raise LockBusy([{"job_id": "?", "owned_paths": ["<lock contention>"]}])

    try:
        claims = []
        if path.is_file():
            try:
                claims = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                # A lock that exists but cannot be read is a damaged lock, not
                # an empty one. Coercing it to [] admitted every job and then
                # rewrote the file with only the new claim, silently discarding
                # the live holds: the failure mode of the safety file was
                # "safety off, nothing said". Refusing is the safe direction.
                raise LockBusy([{
                    "job_id": "<corrupt-lock>",
                    "owned_paths": [f"{path}"],
                    "detail": f"dispatch.lock 无法解析，拒绝派发以免覆盖既有占用：{exc}",
                }]) from exc
        if not isinstance(claims, list):
            raise LockBusy([{
                "job_id": "<corrupt-lock>",
                "owned_paths": [f"{path}"],
                "detail": "dispatch.lock 顶层不是数组，拒绝派发以免覆盖既有占用",
            }])

        live, blocked = [], []
        for claim in claims:
            other = claim.get("job_id", "")
            other_dir = STATE_ROOT / "jobs" / other
            # Finished or vanished jobs must not block forever.
            if (other_dir / "exit.json").is_file() or not (other_dir / "heartbeat.json").is_file():
                continue
            live.append(claim)
            if _overlaps(owned_paths, claim.get("owned_paths") or []):
                blocked.append(claim)

        if blocked:
            raise LockBusy(blocked)

        live.append({"job_id": job_id, "owned_paths": [p for p in owned_paths if p],
                     "pid": os.getpid(), "since": time.time()})
        path.write_text(json.dumps(live, ensure_ascii=False, indent=2), encoding="utf-8")
        return live
    finally:
        try:
            os.unlink(guard)
        except OSError:
            pass


def _release_claim(job_id: str) -> None:
    path = _lock_path()
    try:
        claims = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return
    if not isinstance(claims, list):
        return
    remaining = [c for c in claims if c.get("job_id") != job_id]
    try:
        path.write_text(json.dumps(remaining, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _needs_human(status: str, result: dict, bad: list[str]) -> dict:
    """Whether a person has to act, and why.

    Deliberately derived from structured fields only. Guessing from the summary's
    wording would make the answer depend on how the executor happened to phrase
    things, which is exactly the failure mode this field exists to avoid.
    """
    reasons = []
    if status == "timeout":
        reasons.append("timeout")
    elif status == "cancelled":
        reasons.append("cancelled")
    elif status == "failed":
        reasons.append("failed")
    if bad:
        reasons.append("out_of_scope_writes")
    if result.get("blockers"):
        reasons.append("blocked")
    failed_tests = [t for t in (result.get("tests") or [])
                    if isinstance(t, dict) and t.get("status") == "fail"]
    if failed_tests:
        reasons.append("failing_tests")

    return {"required": bool(reasons), "reasons": reasons}


def _overlapping_jobs(owned_paths: list[str]) -> list[dict]:
    """Live jobs whose owned_paths would collide with this one (dry-run report).

    A damaged lock is reported as a blocker rather than as "no overlap", so a
    dry run never promises a clean slot that dispatch would then refuse.
    """
    path = _lock_path()
    if not path.is_file():
        return []
    try:
        claims = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return [{"job_id": "<corrupt-lock>", "owned_paths": [f"{path}"],
                 "detail": f"dispatch.lock 无法解析：{exc}"}]
    if not isinstance(claims, list):
        return [{"job_id": "<corrupt-lock>", "owned_paths": [f"{path}"],
                 "detail": "dispatch.lock 顶层不是数组"}]

    live = []
    for claim in claims:
        other = claim.get("job_id", "")
        other_dir = STATE_ROOT / "jobs" / other
        if (other_dir / "exit.json").is_file() or not (other_dir / "heartbeat.json").is_file():
            continue
        if _overlaps(owned_paths, claim.get("owned_paths") or []):
            live.append({"job_id": other, "owned_paths": claim.get("owned_paths") or []})
    return live


# ---------------------------------------------------------------------------
# 后台监视器
# ---------------------------------------------------------------------------

def _monitor(job_id: str) -> int:
    """Run one job, then always drop its path claim.

    The claim is taken in _assemble, before the worktree exists, and has to
    survive until the job is over — including the abnormal paths, otherwise a
    crash would leave the paths locked until the heartbeat went stale.
    """
    try:
        return _monitor_body(job_id)
    finally:
        _release_claim(job_id)


def _monitor_body(job_id: str) -> int:
    jd = job_dir(job_id)
    meta = read_json(jd / "job.json") or {}
    argv = meta["argv"]
    env = {**os.environ, **meta.get("env", {})}
    env["WB_SUBAGENT"] = "1"
    env["CROSS_CLI_COLLAB_WORKER"] = "1"
    timeout = meta.get("timeout", DEFAULT_TIMEOUT)
    started = time.time()

    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(5):
            write_json(jd / "heartbeat.json", {"ts": time.time()})

    write_json(jd / "heartbeat.json", {"ts": started})
    t = threading.Thread(target=beat, daemon=True)
    t.start()

    out_path = jd / "stdout.jsonl"
    err_path = jd / "stderr.log"
    status = "failed"
    code = None
    log_cap_hit = False
    # The workbuddy backend takes its prompt on stdin rather than on argv, so a
    # multi-line task body survives (argv truncates it to the first line).
    prompt_on_stdin = meta.get("executor") == "workbuddy"
    with out_path.open("wb") as out, err_path.open("wb") as err:
        proc = subprocess.Popen(
            argv, cwd=meta["cwd"], env=env, stdout=out, stderr=err,
            stdin=subprocess.PIPE if prompt_on_stdin else subprocess.DEVNULL,
        )
        if prompt_on_stdin:
            try:
                proc.stdin.write((meta.get("prompt") or "").encode("utf-8"))
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                # The child exited before reading its prompt; the exit code and
                # stderr below are the real signal, so do not mask them here.
                pass
        while proc.poll() is None:
            if (jd / "cancel.request").exists():
                proc.kill()
                status = "cancelled"
                break
            if time.time() - started > timeout:
                proc.kill()
                status = "timeout"
                break
            # A runaway executor printing in a loop would fill the user's disk
            # while the job still looks healthy. Cap it here, in the loop we
            # already run, and name the reason so the truncation is never
            # mistaken for a completed run. The limit is read from the job
            # record per iteration so it can be overridden per run.
            try:
                cap = int(meta.get("log_cap") or LOG_CAP_BYTES)
                if (out_path.stat().st_size + err_path.stat().st_size) > cap:
                    proc.kill()
                    status = "failed"
                    log_cap_hit = cap
                    break
            except (OSError, TypeError, ValueError):
                pass
            time.sleep(0.5)
        code = proc.wait()
    stop.set()

    if log_cap_hit:
        status = "failed"

    if status not in ("cancelled", "timeout"):
        status = "done" if code == 0 else "failed"

    result = {"ok": False, "raw_tail": ""}
    text = out_path.read_text(encoding="utf-8", errors="replace")
    result = extract_result(text)
    result["artifacts"] = {"stdout": str(out_path), "stderr": str(err_path)}
    if log_cap_hit:
        # Name the cap as the cause: a truncated transcript must never read as
        # a finished run.
        result["ok"] = False
        result.setdefault("blockers", []).append(
            f"输出超过 {log_cap_hit} 字节上限，作业被中止；提高 WB_EXEC_LOG_CAP "
            f"或缩小任务后重跑"
        )

    wt = meta.get("worktree")
    if wt and Path(wt).exists():
        try:
            diff = capture_diff(Path(wt), jd)
            result["artifacts"]["patch"] = diff["patch"]
            result["artifacts"]["patch_bytes"] = diff["patch_bytes"]
            result["changed_files_git"] = diff["changed"]
            bad = violations(diff["changed"], meta.get("owned_paths", []))
            if bad:
                status = "done" if status == "done" else status
        except Exception as exc:  # noqa: BLE001
            bad = []
            result.setdefault("blockers", []).append(f"diff 采集失败: {exc}")
    else:
        # No worktree means no diff was captured, so nothing can be out of scope.
        bad = []

    # Always present, even when empty: consumers read result["violations"]
    # without checking for the key, and protocol.md documents the field as a
    # list. Omitting it on a clean run made the documented shape a lie.
    result["violations"] = bad

    # "A person must decide something" is not the same as "the run failed", and
    # it cannot be inferred from the free-text summary. Record it structurally so
    # the host can route on it without parsing prose.
    result["needs_human"] = _needs_human(status, result, bad)

    write_json(jd / "result.json", result)
    write_json(jd / "exit.json", {
        "status": status,
        "exit_code": code,
        "ended_at": time.time(),
        "elapsed_s": round(time.time() - started, 1),
    })
    return 0


def extract_result(text: str) -> dict:
    """从 stream-json / json / text 输出里取出执行体结论。"""
    if not text.strip():
        return {"ok": False, "blockers": ["执行体没有输出"]}

    objs: list[dict] = []
    whole = text.strip()
    if whole.startswith("{"):
        try:
            parsed = json.loads(whole)
            if isinstance(parsed, dict):
                objs.append(parsed)
        except Exception:  # noqa: BLE001
            pass
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(("{", "[")):
            continue
        try:
            obj = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(obj, dict):
            objs.append(obj)

    payload = None
    for obj in reversed(objs):
        if obj.get("type") == "result" and obj.get("is_error"):
            errors = obj.get("errors")
            if not isinstance(errors, list) or not errors:
                errors = [str(obj.get("result") or obj.get("message") or "执行体返回错误")]
            return {"ok": False, "blockers": [str(e) for e in errors],
                    "raw_tail": text[-2000:]}
        for key in ("structured_output", "structuredOutput"):
            if isinstance(obj.get(key), dict):
                payload = obj[key]
                break
        if payload:
            break
        if obj.get("type") == "result":
            for key in ("result", "message", "content"):
                val = obj.get(key)
                if isinstance(val, dict):
                    payload = val
                    break
                if isinstance(val, str) and val.strip():
                    payload = _maybe_json(val)
                    break
        if payload:
            break
        msg = obj.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), list):
            texts = [c.get("text", "") for c in msg["content"] if isinstance(c, dict)]
            joined = "".join(texts).strip()
            if joined:
                payload = _maybe_json(joined)

    if isinstance(payload, dict):
        return {"ok": True, **payload}
    return {"ok": False, "raw_tail": text[-2000:], "blockers": ["无法解析结构化结论"]}


def _maybe_json(s: str):
    s = s.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    if s.startswith("{"):
        try:
            return json.loads(s)
        except Exception:  # noqa: BLE001
            return s
    return s


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------

def _assemble(args) -> tuple[list[str], dict, Path]:
    task = load_task(Path(args.task))
    cwd = Path(args.cwd or os.getcwd()).resolve()
    if not cwd.exists():
        raise SystemExit(f"工作目录不存在: {cwd}")

    refs = [str(Path(x).resolve()) for x in task["inputs"] if Path(x).exists()]
    worktree = None
    run_cwd = cwd
    jd = None

    job_id = uuid.uuid4().hex[:12]
    jd = job_dir(job_id)

    # A dry run must not leave anything behind: no job directory, no worktree,
    # nothing for `jobs` to list later. Resolve the argv-only parts first and
    # skip every side effect.
    if getattr(args, "dry_run", False):
        prompt = build_prompt(task, cwd)
        backend = getattr(args, "executor", None) or _backend_env()
        cli = find_cli(backend)
        schema = json.dumps(RESULT_SCHEMA, ensure_ascii=False, separators=(",", ":"))
        argv = build_executor_argv(backend, cli, prompt, cwd, schema, refs)
        blockers = _overlapping_jobs(task["owned_paths"])
        meta = {"job_id": job_id, "cwd": str(cwd), "worktree": None,
                "executor": backend, "dry_run": True,
                "would_block": blockers}
        return argv, meta, jd

    # Claim the paths before creating a worktree, so a refusal costs nothing.
    # The claim outlives this call: _monitor releases it when the job ends.
    _acquire_claim(task["owned_paths"], job_id)

    jd.mkdir(parents=True, exist_ok=True)

    if args.worktree == "auto" and is_git_repo(cwd):
        worktree = make_worktree(cwd, jd, job_id)
        if worktree is not None:
            run_cwd = worktree

    prompt = build_prompt(task, run_cwd)
    backend = getattr(args, "executor", None) or _backend_env()
    cli = find_cli(backend)
    schema = json.dumps(RESULT_SCHEMA, ensure_ascii=False, separators=(",", ":"))

    argv = build_executor_argv(backend, cli, prompt, run_cwd, schema, refs)

    env = {}
    if any("app.asar.unpacked" in part for part in cli):
        workbuddy_home = Path.home() / ".workbuddy"
        env = {
            "CODEBUDDY_FORCE_LITE_WB_BUNDLE": "1",
            "CODEBUDDY_CONFIG_DIR": str(workbuddy_home),
        }
        catalog = workbuddy_home / "cache" / "acc-product-config-v3.json"
        if catalog.is_file():
            env["ACC_PRODUCT_CONFIG_PATH"] = str(catalog)

    meta = {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "task_file": str(Path(args.task).resolve()),
        "goal": task["goal"],
        "cwd": str(run_cwd),
        "repo": str(cwd),
        "worktree": str(worktree) if worktree else None,
        "isolated": worktree is not None,
        "owned_paths": task["owned_paths"],
        "executor": backend,
        "model": EXECUTOR_MODEL if backend == "workbuddy" else None,
        "effort": EXECUTOR_EFFORT if backend == "workbuddy" else None,
        "model_note": ("固定执行体：WorkBuddy / DeepSeek V4.1 Flash，档位 极致(max)"
                       if backend == "workbuddy"
                       else f"后端 {backend}：模型与档位由该 CLI 自己的配置决定"),
        "argv": argv,
        # The workbuddy backend delivers the prompt on stdin, not on argv; the
        # monitor reads it back from here. Kept in the job record so a run can
        # be reproduced exactly, which argv alone no longer would.
        "prompt": prompt,
        # Snapshot the log cap at dispatch time so a run is reproducible and so
        # the value can be overridden for one run without a global env var.
        "log_cap": LOG_CAP_BYTES,
        "env": env,
        "timeout": int(args.timeout),
        "created_at": time.time(),
        "started_at": time.time(),
    }
    write_json(jd / "job.json", meta)
    return argv, meta, jd


def cmd_dispatch(args) -> int:
    if os.environ.get("WB_SUBAGENT"):
        print(json.dumps({"ok": False, "error": "RECURSION_BLOCKED",
                          "message": "当前进程已是被派发的执行体，禁止再次派发。"},
                         ensure_ascii=False))
        return 3
    try:
        argv, meta, jd = _assemble(args)
    except LockBusy as exc:
        print(json.dumps({"ok": False, "error": "PATHS_LOCKED",
                          "message": "owned_paths 与正在运行的作业重叠，本次未派发。",
                          "holders": exc.holders},
                         ensure_ascii=False, indent=2))
        return 4
    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "job_id": meta["job_id"],
                          "cwd": meta["cwd"], "worktree": meta["worktree"],
                          # Say up front whether this dispatch would be refused,
                          # so a dry run is a real rehearsal and not just argv echo.
                          "would_block": meta.get("would_block") or [],
                          "argv": argv, "job_dir": str(jd)},
                         ensure_ascii=False, indent=2))
        return 0

    monitor_argv = [sys.executable, str(Path(__file__).resolve()), "_monitor", "--job", meta["job_id"]]
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(
        monitor_argv, cwd=str(Path(__file__).resolve().parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, close_fds=True,
        creationflags=flags,
        start_new_session=(os.name != "nt"),
    )
    print(json.dumps({"ok": True, "job_id": meta["job_id"], "status": "running",
                      "job_dir": str(jd), "worktree": meta["worktree"]},
                     ensure_ascii=False))
    return 0


def cmd_run(args) -> int:
    if os.environ.get("WB_SUBAGENT"):
        print(json.dumps({"ok": False, "error": "RECURSION_BLOCKED",
                          "message": "当前进程已是被派发的执行体，禁止再次派发。"},
                         ensure_ascii=False))
        return 3
    try:
        _, meta, jd = _assemble(args)
    except LockBusy as exc:
        print(json.dumps({"ok": False, "error": "PATHS_LOCKED",
                          "message": "owned_paths 与正在运行的作业重叠，本次未执行。",
                          "holders": exc.holders},
                         ensure_ascii=False, indent=2))
        return 4
    _monitor(meta["job_id"])
    return _emit_collect(meta["job_id"], wait=False)


def _emit_collect(job_id: str, wait: bool, deadline: float | None = None) -> int:
    jd = job_dir(job_id)
    while True:
        st = job_status(job_id)
        if st["status"] not in ("running", "stalled"):
            break
        if not wait or (deadline and time.time() > deadline):
            print(json.dumps({"protocol": PROTOCOL_VERSION, **st},
                             ensure_ascii=False, indent=2))
            return 0
        time.sleep(0.5)
    result = read_json(jd / "result.json") or {"ok": False}
    meta = read_json(jd / "job.json") or {}
    out = {"protocol": PROTOCOL_VERSION,
           "ok": st["status"] == "done" and bool(result.get("ok")),
           "job_id": job_id, "status": st["status"],
           "exit_code": st.get("exit_code"), "elapsed_s": st.get("elapsed_s"),
           # Whether isolation actually happened decides whether a diff exists at
           # all. Surfacing it here means a caller can tell "clean run" from
           # "ran in place, so no diff was captured" without opening job.json.
           "isolated": bool(meta.get("isolated")),
           "executor": meta.get("executor"),
           "worktree": meta.get("worktree"),
           "result": result, "job_dir": str(jd)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["ok"] else 1


def cmd_status(args) -> int:
    print(json.dumps(job_status(args.job), ensure_ascii=False, indent=2))
    return 0


def cmd_collect(args) -> int:
    deadline = time.time() + args.wait_timeout if args.wait else None
    return _emit_collect(args.job, wait=args.wait, deadline=deadline)


def cmd_cancel(args) -> int:
    jd = job_dir(args.job)
    if not jd.exists():
        raise SystemExit(f"作业不存在: {args.job}")
    (jd / "cancel.request").write_text(str(time.time()), encoding="utf-8")
    print(json.dumps({"ok": True, "job_id": args.job, "cancel_requested": True},
                     ensure_ascii=False))
    return 0


def cmd_jobs(args) -> int:
    root = STATE_ROOT / "jobs"
    rows = []
    if root.is_dir():
        for d in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[: args.limit]:
            st = read_json(d / "job.json") or {}
            rows.append({"job_id": st.get("job_id", d.name), "goal": (st.get("goal") or "")[:60],
                         "status": job_status(d.name).get("status") if (d / "job.json").exists() else "?"})
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def cmd_doctor(args) -> int:
    info: dict = {"ok": True, "schema_version": SCHEMA_VERSION,
                  "python": sys.version.split()[0],
                  "state_root": str(STATE_ROOT),
                  "git": shutil.which("git") or None,
                  "subagent_env": bool(os.environ.get("WB_SUBAGENT"))}
    backend = getattr(args, "executor", None) or _backend_env()
    info["executor"] = backend
    try:
        cli = find_cli(backend)
        info["cli"] = cli
        info["cli_version"] = cli_version(cli)
        info["cli_source"] = "WB_CLI" if os.environ.get("WB_CLI") else "auto"
    except CliNotFound as exc:
        info["ok"] = False
        info["cli_error"] = str(exc)
    cwd = Path(args.cwd or os.getcwd())
    info["cwd"] = str(cwd)
    info["is_git_repo"] = is_git_repo(cwd)
    info["available_executors"] = list(EXECUTOR_BACKENDS)
    info["executor_settings"] = (
        {"model": EXECUTOR_MODEL, "effort": EXECUTOR_EFFORT, "note": "固定，不可切换"}
        if backend == "workbuddy"
        else {"note": f"后端 {backend}：参数由该 CLI 决定，不注入 model/effort"}
    )
    info["defaults"] = {"worktree": "auto", "timeout_s": DEFAULT_TIMEOUT,
                        "executor": DEFAULT_EXECUTOR}
    info["notes"] = [
        "isolated=true 时任务在 .wb-exec/jobs/<id>/wt 的独立 worktree 中执行",
        "默认执行体 workbuddy 固定为 deepseek-v4.1-flash / 极致(max)：不提供其他模型、不做自动降级",
        "用 --executor 或 WB_EXECUTOR 切换后端；custom 需同时提供 WB_CLI 与 WB_CLI_ARGV 模板",
        "从 WorkBuddy 会话内部再调 codebuddy 会与本次会话共用守护进程，可能阻塞；应在外部终端触发",
    ]
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0 if info["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wb_exec", description="把任务派发给 WorkBuddy 无头执行体")
    sub = p.add_subparsers(dest="cmd", required=True)

    def task_opts(sp, with_run: bool = False):
        sp.add_argument("--task", required=True, help="任务包 JSON 路径")
        sp.add_argument("--cwd", help="工作目录（默认当前目录）")
        sp.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="秒，默认 1800")
        sp.add_argument("--worktree", choices=["auto", "off"], default="auto",
                        help="auto: Git 仓库自动建独立 worktree")
        sp.add_argument("--executor", choices=list(EXECUTOR_BACKENDS), default=None,
                        help=f"执行体后端，默认 {DEFAULT_EXECUTOR}（也可用 WB_EXECUTOR）")

    d = sub.add_parser("dispatch", help="后台派发，立即返回 job_id")
    task_opts(d)
    d.add_argument("--dry-run", action="store_true", help="只打印将要执行的 argv，不发起调用")
    d.set_defaults(func=cmd_dispatch)

    r = sub.add_parser("run", help="前台执行并等待回收")
    task_opts(r)
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("status", help="查询作业状态")
    s.add_argument("--job", required=True)
    s.set_defaults(func=cmd_status)

    c = sub.add_parser("collect", help="回收作业结果")
    c.add_argument("--job", required=True)
    c.add_argument("--wait", action="store_true")
    c.add_argument("--wait-timeout", type=int, default=DEFAULT_TIMEOUT)
    c.set_defaults(func=cmd_collect)

    x = sub.add_parser("cancel", help="请求取消作业")
    x.add_argument("--job", required=True)
    x.set_defaults(func=cmd_cancel)

    j = sub.add_parser("jobs", help="列出最近作业")
    j.add_argument("--limit", type=int, default=10)
    j.set_defaults(func=cmd_jobs)

    doc = sub.add_parser("doctor", help="自检：CLI、git、环境")
    doc.add_argument("--cwd")
    doc.add_argument("--executor", choices=list(EXECUTOR_BACKENDS), default=None,
                     help=f"检查哪个后端，默认 {DEFAULT_EXECUTOR}（也可用 WB_EXECUTOR）")
    doc.set_defaults(func=cmd_doctor)

    m = sub.add_parser("_monitor", help=argparse.SUPPRESS)
    m.add_argument("--job", required=True)
    m.set_defaults(func=lambda a: _monitor(a.job))
    return p


def main() -> int:
    _utf8_stdout()
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
