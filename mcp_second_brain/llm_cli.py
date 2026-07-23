"""Second Brain 統一 LLM CLI helper。

後端優先序（可用 env 切換）：
  1. **本機 OpenAI 相容端點**（若設 ``SB_LLM_BASE_URL``，如 llama-server 上的 Gemma4）
     — 省 Claude token、全本機。
  2. **Claude Code CLI**（``claude --print -p``，訂閱授權、無需 key）。
  3. Gemini CLI（免費 tier 已於 2026-06 停用，僅保留為最後備援）。

env：
  - ``SB_LLM_BASE_URL``  例如 ``http://localhost:11434/v1``（未設則跳過本機後端）
  - ``SB_LLM_MODEL``     預設 ``gemma``
  - ``SB_LLM_NO_THINK``  預設 ``1``（Gemma 推理型模型需關 thinking，否則 content 空白）

本機後端純用 urllib（stdlib），不引入新依賴。任一後端失敗即往下一個備援，全失敗回 None。

launchd 批次環境 PATH 常很精簡，``shutil.which`` 可能回 None，故退回硬路徑
``/usr/local/bin``（同 cnyes_archiver / finance-kit news_sentiment_analyzer 的做法）。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path


def _resolve(cli: str) -> str | None:
    found = shutil.which(cli)
    if found:
        return found
    hard = f"/usr/local/bin/{cli}"
    return hard if os.path.exists(hard) else None


_CLAUDE_CLI = _resolve("claude")
_GEMINI_CLI = _resolve("gemini")

# --- 本機 OpenAI 相容後端（Gemma4 等）---------------------------------------
_LOCAL_BASE = os.environ.get("SB_LLM_BASE_URL", "").rstrip("/")
_LOCAL_MODEL = os.environ.get("SB_LLM_MODEL", "gemma")
_LOCAL_NO_THINK = os.environ.get("SB_LLM_NO_THINK", "1") not in ("0", "false", "False", "")


def _local_chat(prompt: str, *, image_path: Path | str | None = None, timeout: int) -> str | None:
    """POST 到本機 OpenAI 相容 /chat/completions。無 SB_LLM_BASE_URL 則回 None（跳過）。

    - 對 Gemma 推理型模型傳 ``chat_template_kwargs={"enable_thinking": false}``。
    - 若最終 ``content`` 空白，退而取 ``reasoning_content``（避免推理模型吐空）。
    """
    if not _LOCAL_BASE:
        return None
    if image_path is not None:
        p = Path(image_path)
        if not p.exists():
            return None
        b64 = base64.b64encode(p.read_bytes()).decode()
        content = [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            {"type": "text", "text": prompt},
        ]
    else:
        content = [{"type": "text", "text": prompt}]

    payload = {
        "model": _LOCAL_MODEL,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.2,
        "max_tokens": 1024,
    }
    if _LOCAL_NO_THINK:
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    try:
        req = urllib.request.Request(
            f"{_LOCAL_BASE}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
        msg = d["choices"][0]["message"]
        out = (msg.get("content") or "").strip()
        if not out:
            out = (msg.get("reasoning_content") or "").strip()
        return out or None
    except Exception:
        return None


def _run(cmd: list[str], *, stdin: str | None = None, timeout: int, cwd: str | None = None,
         env: dict | None = None) -> str | None:
    try:
        result = subprocess.run(
            cmd, input=stdin, capture_output=True, text=True,
            timeout=timeout, cwd=cwd, env=env,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def llm_text(prompt: str, *, timeout: int = 90) -> str | None:
    """純文字 LLM 呼叫。本機 Gemma → Claude → Gemini（已死）。全部失敗回 None。"""
    out = _local_chat(prompt, timeout=timeout)
    if out:
        return out
    if _CLAUDE_CLI:
        out = _run([_CLAUDE_CLI, "--print", "-p", prompt],
                   timeout=timeout, cwd=str(Path.home()))
        if out:
            return out
    if _GEMINI_CLI:
        env = os.environ.copy()
        env["GEMINI_CLI_TRUST_WORKSPACE"] = "false"
        out = _run([_GEMINI_CLI, "-p", prompt],
                   timeout=timeout, cwd=str(Path.home()), env=env)
        if out:
            return out
    return None


def llm_image(prompt: str, image_path: Path | str, *, timeout: int = 120) -> str | None:
    """多模態 LLM 呼叫（讀圖）。本機 Gemma（vision）→ Claude CLI（@path）。失敗回 None。"""
    p = Path(image_path)
    if not p.exists():
        return None
    out = _local_chat(prompt, image_path=p, timeout=timeout)
    if out:
        return out
    if _CLAUDE_CLI:
        full_prompt = f"{prompt}\n\n@{p.name}"
        out = _run([_CLAUDE_CLI, "--print", "-p", full_prompt],
                   timeout=timeout, cwd=str(p.parent))
        if out:
            return out
    return None
