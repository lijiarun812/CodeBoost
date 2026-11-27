"""在「代理不稳」的机器上跑 effi_repro 的启动器（只加网络韧性，不改任何测量逻辑）。

为什么需要它：
- 本机走 Clash TUN（DNS 把接口域名解析到 198.18.0.x 假 IP，实际连到 127.0.0.1:7897），
  长连接偶发「握手成功但迟迟不回包」，`requests` 默认 timeout=300 会卡住十分钟以上，
  表面上像死进程。
- 这里只做两件事：
    1) 把 socket 超时压到 (连接 10s / 读 90s)，卡住就快速失败并重试；
    2) 给 `LLMClient.chat` 外面再套一层 4 次重试（内部原本已有 6 次）。
- **不碰**计时/内存/判据：`measure()`、`new_metrics()`、接受判据全部原样调用。

用法（参数与 effi_repro.py 完全一致）：
    python tools/run_effi_resilient.py --num 20 --epochs 3 --accept-rule paper \
        --model deepseek-v4-flash-0731 --out results/repro_dual
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

TIMEOUT = (10.0, 90.0)
_orig_post = requests.post


def _post(*args, **kwargs):
    kwargs["timeout"] = TIMEOUT
    return _orig_post(*args, **kwargs)


requests.post = _post  # 必须在 import effi_repro 之前生效（它 import requests 后直接调用）

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "effilearner"))

import effi_repro  # noqa: E402

_orig_chat = effi_repro.LLMClient.chat


def _chat(self, *args, **kwargs):
    last = None
    for attempt in range(4):
        try:
            return _orig_chat(self, *args, **kwargs)
        except Exception as e:  # noqa: BLE001
            last = e
            print("   [网络] chat 失败(%d/4)：%s" % (attempt + 1, str(e)[:160]), flush=True)
            time.sleep(3 * (attempt + 1))
    raise last


effi_repro.LLMClient.chat = _chat

if __name__ == "__main__":
    sys.argv = ["effi_repro.py"] + sys.argv[1:]
    effi_repro.main()
