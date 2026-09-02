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
import re
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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


def _local_chat(prompt: str, *, image_path: Path | str | None = None, timeout: int,
                 max_tokens: int = 1024) -> str | None:
    """POST 到本機 OpenAI 相容 /chat/completions。無 SB_LLM_BASE_URL 則回 None（跳過）。

    - 對 Gemma 推理型模型傳 ``chat_template_kwargs={"enable_thinking": false}``。
    - 若最終 ``content`` 空白，退而取 ``reasoning_content``（避免推理模型吐空）。
    - ``max_tokens`` 預設 1024 是給短輸出（關鍵字、單一分類）用的；內容豐富、輸出量大的呼叫端
      （例如逐 chunk 抽取多條結構化陳述）必須自己傳更高的值，否則輸出會在 JSON 講到一半被硬切斷——
      這不是「模型亂回」或格式問題，見 F3（2026-08-19，litnet-抽取稀疏的定案根因）：4 個一開始
      被判定成 no_json_in_reply 的 chunk，唯一差別只是 max_tokens 從 1024 調到 4096，跟有沒有套
      grammar/schema 約束無關。
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
        "max_tokens": max_tokens,
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


def llm_text(prompt: str, *, timeout: int = 90, max_tokens: int = 1024) -> str | None:
    """純文字 LLM 呼叫。本機 Gemma → Claude → Gemini（已死）。全部失敗回 None。

    ``max_tokens`` 只餵給本機後端（CLI 後端不支援這個參數，本來就沒有硬性上限）。預設 1024
    是給短輸出用的；輸出量可能很大的呼叫端（例如一次要抽多條結構化陳述）應該明確傳更高的值。
    """
    out = _local_chat(prompt, timeout=timeout, max_tokens=max_tokens)
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


# ---------------------------------------------------------------------------
# 視覺結構化問答（VLM → JSON）
# ---------------------------------------------------------------------------
#
# 「送一張圖 + 要一份 JSON 回來」原本在 figures.py 手抄了兩遍（figure 分析、
# 頁面 bbox 偵測），各自處理 base64/media-type、code-fence 包裹的 JSON、
# token usage、以及「全部吞掉回空值」的錯誤處理。空值與「模型真的說沒有」
# 無法區分——有 figure 紀錄不等於真的看過那張圖。
#
# 這道縫把它收成一個介面，並讓失敗**可區分**：
#   None            → VLM 沒有給出答案（後端掛了 / 回覆解析不出 JSON）
#   VisionAnswer    → 模型答了；data 可能是空 list（真的沒東西），那是有效答案

_VISION_MODEL = os.environ.get("SB_VISION_MODEL", "claude-haiku-4-5-20251001")

_IMAGE_MEDIA_TYPES = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "webp": "image/webp",
}


@dataclass(frozen=True)
class VisionAnswer:
    """一次成功的視覺問答。``data`` 是解析後的 JSON（dict 或 list）。

    ``usage`` 是 {"input": n, "output": n}；只有 Anthropic SDK 後端報得出實際
    token 數，CLI 後端一律回 0（呼叫端據此累加成本，0 代表「沒量到」而非免費）。
    """

    data: Any
    usage: dict
    backend: str


def _media_type(path: Path) -> str:
    return _IMAGE_MEDIA_TYPES.get(path.suffix.lower().lstrip("."), "image/png")


def _extract_json(raw: str, expect: str) -> Any | None:
    """從可能被 markdown code fence 包住的回覆裡撈出 JSON。解析不出回 None。"""
    pattern = r"\[.*\]" if expect == "array" else r"\{.*\}"
    m = re.search(pattern, raw, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except (json.JSONDecodeError, ValueError):
        return None


def _anthropic_vision(
    prompt: str, image_path: Path, *, model: str, max_tokens: int
) -> tuple[str, dict] | None:
    """Anthropic SDK 後端。無套件 / 無金鑰 / 呼叫失敗一律回 None（往下備援）。"""
    try:
        import anthropic
    except ImportError:
        return None
    try:
        client = anthropic.Anthropic()
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            stream=False,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": _media_type(image_path),
                        "data": base64.b64encode(image_path.read_bytes()).decode(),
                    }},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        usage = {
            "input": getattr(message.usage, "input_tokens", 0),
            "output": getattr(message.usage, "output_tokens", 0),
        }
        return message.content[0].text.strip(), usage
    except Exception as e:
        print(f"[llm_cli] anthropic vision failed: {e}", file=sys.stderr)
        return None


def vision_json(
    prompt: str,
    image_path: Path | str,
    *,
    expect: str = "object",
    model: str | None = None,
    max_tokens: int = 1024,
    timeout: int = 120,
) -> VisionAnswer | None:
    """問 VLM 一張圖並要求 JSON 回覆。Anthropic SDK → CLI 備援。

    Args:
        prompt: 提示詞，應明確要求只輸出 JSON。
        image_path: 圖檔路徑。
        expect: ``"object"`` 撈 ``{...}``、``"array"`` 撈 ``[...]``。
        model: 覆寫模型（預設 ``SB_VISION_MODEL``）。
        max_tokens / timeout: 分別給 SDK 與 CLI 後端。

    Returns:
        ``VisionAnswer``，或 **None 代表沒有得到答案**——後端全掛，或回覆裡撈不出
        合法 JSON。空的 ``data``（``[]`` / ``{}``）是有效答案，代表模型看過且說沒有。
    """
    p = Path(image_path)
    if not p.exists():
        return None

    sdk = _anthropic_vision(prompt, p, model=model or _VISION_MODEL, max_tokens=max_tokens)
    if sdk is not None:
        data = _extract_json(sdk[0], expect)
        if data is not None:
            return VisionAnswer(data=data, usage=sdk[1], backend="anthropic")
        print("[llm_cli] anthropic vision reply had no parsable JSON", file=sys.stderr)

    raw = llm_image(prompt, p, timeout=timeout)
    if raw:
        data = _extract_json(raw, expect)
        if data is not None:
            return VisionAnswer(data=data, usage={"input": 0, "output": 0}, backend="cli")
        print("[llm_cli] cli vision reply had no parsable JSON", file=sys.stderr)
    return None
