#!/usr/bin/env python3
"""桩执行体：复现 codebuddy 的 stream-json 回传形状，用于验证 wb-exec 的回传链路。

不发起任何模型调用。FAKE_VIOLATION=1 时故意越界写 README.md，用于验证越界检测。
"""

import json
import os
import sys
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    if "--version" in args:
        print("0.0.0-fake")
        return 0

    cwd = Path.cwd()
    target = cwd / "hello.py"
    if not target.exists():
        target.write_text('def greet():\n    return "hi"\n', encoding="utf-8")
    with target.open("a", encoding="utf-8") as fh:
        fh.write('\n\ndef farewell():\n    return "bye"\n')

    if os.environ.get("FAKE_VIOLATION"):
        (cwd / "README.md").write_text("# demo\ntampered\n", encoding="utf-8")

    payload = {
        "summary": "在 hello.py 里新增 farewell()，保留原有 greet()。",
        "changed_files": ["hello.py"],
        "tests": [{"cmd": 'python -c "import hello; print(hello.farewell())"',
                   "status": "pass", "detail": "bye"}],
        "blockers": [],
        "notes": "桩执行体产出，仅用于链路验证",
    }
    events = [
        {"type": "system", "subtype": "init", "cwd": str(cwd)},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "开始执行"}]}},
        {"type": "result", "subtype": "success", "structured_output": payload},
    ]
    for ev in events:
        print(json.dumps(ev, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
