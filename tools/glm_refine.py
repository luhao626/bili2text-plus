#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Standalone Markdown polisher for Bili2Text using Zhipu GLM (OpenAI-compatible).

Why this exists:
    Bili2Text's "AI修订" button only works for a folder processed in the current
    app session (last_folder_name is in-memory). This script lets you polish any
    existing outputs/*.md without touching the app, the EXE, or the originals.

Safety:
    * never overwrites the input file
    * writes a sibling file named <name>_refined.md (or a timestamped variant)
    * rejects non-Zhipu endpoints so you never silently call MiniMax

Usage:
    python glm_refine.py                     # interactive pick from outputs/
    python glm_refine.py path\to\file.md     # one file
    python glm_refine.py a.md b.md           # several files
    python glm_refine.py --test              # tiny connectivity test
"""

import argparse
import datetime as dt
import hashlib
import os
import re
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DEFAULT_BASE = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_MODEL = "glm-4.7-flash"
DEFAULT_MAX_TOKENS = 16000
CHUNK_CHARS = 5000  # conservative: keeps every request well inside limits

SYSTEM_PROMPT = (
    "你是一个严格的中文转写润色助手。"
    "请仅对给定文本进行错别字、标点、断句和轻微语气调整，"
    "保持原始语序与信息完整，不新增总结、点评、延伸内容，"
    "不改写成新的风格，也不要删除有效信息。"
    "必须原样保留专业术语、产品名、人名、机构名、数字和英文缩写。"
    "如果文本分块提供，也要保持上下文衔接。"
    "输出与输入同一语种，并保持 Markdown 中的段落结构。"
)

TEST_SAMPLE = (
    "今天我们来讲一下这个模型的推理速度。其实呢，它并不是单纯的快，"
    "而是把前面几步的缓存给复用掉了，所以在长文本上面会比较明显。"
)


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def outputs_dir() -> Path:
    return project_root() / "outputs"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_config(args) -> tuple:
    key = (
        os.getenv("GLM_API_KEY")
        or os.getenv("ZHIPUAI_API_KEY")
        or os.getenv("MINIMAX_APIKEY")
        or os.getenv("MINIMAX_API_KEY")
        or ""
    ).strip()
    base = (args.base or os.getenv("MINIMAX_API_BASE") or DEFAULT_BASE).strip()
    model = (args.model or os.getenv("MINIMAX_MODEL") or DEFAULT_MODEL).strip()

    if not key:
        raise SystemExit(
            "[错误] 未找到 API Key。请先双击 GLM-Setup.cmd 配置，"
            "或设置用户环境变量 MINIMAX_APIKEY。"
        )
    if "bigmodel.cn" not in base:
        raise SystemExit(
            f"[错误] 当前 Base URL 不是智谱端点：{base}\n"
            "       为避免误调用其它服务，脚本已停止。"
        )
    return key, base, model


def make_client(key: str, base: str):
    from openai import OpenAI

    return OpenAI(api_key=key, base_url=base, timeout=300.0, max_retries=0)


def strip_thinking(text: str) -> str:
    text = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>\s*", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"^\s*<think(?:ing)?>.*$", "", text, flags=re.DOTALL | re.IGNORECASE)
    return text.strip()


def call_glm(client, model: str, text: str, topic: str, max_tokens: int) -> tuple:
    """One chat completion with retry on rate limit / transient server errors.

    glm-4.7-flash has thinking enabled by default, which would burn tokens on
    visible reasoning instead of the polished text. We disable it explicitly;
    if the endpoint rejects the parameter we retry without it.
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"主题提示：{topic}\n\n"
                "以下是转写原文，请仅进行必要的纠错和轻量润色，不要总结：\n"
                f"{text}"
            ),
        },
    ]
    thinking_variants = [{"type": "disabled"}, None]
    last_error = None
    for attempt in range(1, 4):
        extra = {"thinking": thinking_variants[0]} if thinking_variants[0] else None
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.3,
                max_tokens=max_tokens,
                **({"extra_body": extra} if extra else {}),
            )
            message = response.choices[0].message
            content = (message.content or "").strip()
            if not content:
                content = (getattr(message, "reasoning_content", None) or "").strip()
                if content:
                    raise RuntimeError(
                        "模型只返回了思考内容，未输出正文（thinking 未能关闭）"
                    )
            content = strip_thinking(content)
            if not content:
                raise RuntimeError("模型返回空内容")
            return content, getattr(response, "usage", None), getattr(response, "model", None)
        except Exception as error:  # noqa: BLE001 - surface a readable message
            message_text = str(error)
            status = getattr(error, "status_code", None)
            if thinking_variants and "thinking" in message_text.lower():
                # Endpoint rejected the thinking parameter: drop it and retry once.
                thinking_variants = [None]
                continue
            last_error = error
            if status in (429, 500, 502, 503, 504) and attempt < 3:
                wait = 5 * attempt
                print(f"  [重试] 第 {attempt} 次失败（HTTP {status}），{wait}s 后重试…")
                time.sleep(wait)
                continue
            break
    raise RuntimeError(f"调用失败：{last_error}")


def split_chunks(text: str, limit: int = CHUNK_CHARS) -> list:
    """Split on paragraph boundaries; fall back to sentence boundaries."""
    paragraphs = text.split("\n")
    chunks, current = [], ""
    for paragraph in paragraphs:
        if len(paragraph) > limit:
            if current:
                chunks.append(current)
                current = ""
            buffer = ""
            for sentence in re.split(r"(?<=[。！？；!?;])", paragraph):
                if len(buffer) + len(sentence) > limit and buffer:
                    chunks.append(buffer)
                    buffer = ""
                buffer += sentence
            if buffer:
                chunks.append(buffer)
            continue
        if len(current) + len(paragraph) + 1 > limit and current:
            chunks.append(current)
            current = ""
        current = f"{current}\n{paragraph}" if current else paragraph
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk.strip()]


def unique_output_path(source: Path) -> Path:
    candidate = source.with_name(f"{source.stem}_refined.md")
    if not candidate.exists():
        return candidate
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return source.with_name(f"{source.stem}_refined_{stamp}.md")


def refine_file(client, model: str, source: Path, max_tokens: int) -> Path:
    before_hash = sha256(source)
    print(f"\n[处理] {source}")
    raw = source.read_text(encoding="utf-8")
    body = "\n".join(line for line in raw.split("\n") if not line.startswith("#")).strip()
    if not body:
        raise RuntimeError("文件没有可润色的正文")

    chunks = split_chunks(body)
    print(f"  正文 {len(body)} 字，分 {len(chunks)} 段发送")

    refined_parts, served_model = [], None
    for index, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            print(f"  第 {index}/{len(chunks)} 段（{len(chunk)} 字）…")
        refined, usage, response_model = call_glm(
            client, model, chunk, source.stem, max_tokens
        )
        refined_parts.append(refined)
        served_model = response_model or served_model
        if usage is not None:
            print(
                f"    tokens: in={getattr(usage, 'prompt_tokens', '?')} "
                f"out={getattr(usage, 'completion_tokens', '?')}"
            )

    result = "\n\n".join(refined_parts).strip()
    target = unique_output_path(source)
    target.write_text(
        "# 转写结果（GLM 润色版）\n\n"
        f"> 模型：{served_model or model}\n>\n"
        f"> 原始文件：{source.name}\n>\n"
        f"> 生成时间：{dt.datetime.now():%Y-%m-%d %H:%M:%S}\n\n"
        f"{result}\n",
        encoding="utf-8",
    )

    after_hash = sha256(source)
    if before_hash != after_hash:
        raise RuntimeError("原始文件校验失败：内容发生了变化，请立即检查！")
    print(f"  [完成] {target}")
    print(f"  原文未改动（SHA256 一致，{before_hash[:12]}…）")
    return target


def choose_files_interactively() -> list:
    folder = outputs_dir()
    if not folder.is_dir():
        raise SystemExit(f"[错误] 找不到 outputs 目录：{folder}")
    candidates = sorted(
        [p for p in folder.glob("*.md") if not p.stem.endswith("_refined")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise SystemExit("[错误] outputs 目录里没有可润色的 .md 文件。")

    print("\n可润色的原始转写文件：")
    for index, path in enumerate(candidates, 1):
        size = path.stat().st_size
        print(f"  {index}. {path.name}  ({size} 字节)")

    print("\n输入序号（多个用逗号分隔），直接回车 = 最新一个，输入 a = 全部：")
    try:
        answer = input("> ").strip().lower()
    except EOFError:
        answer = ""
    if not answer:
        return [candidates[0]]
    if answer in ("a", "all", "*"):
        return candidates
    picked = []
    for token in re.split(r"[,，\s]+", answer):
        if token.isdigit() and 1 <= int(token) <= len(candidates):
            picked.append(candidates[int(token) - 1])
    if not picked:
        raise SystemExit("[错误] 没有有效的序号。")
    return picked


def run_test(client, base: str, model: str, max_tokens: int) -> int:
    print(f"端点：{base}")
    print(f"模型：{model}")
    started = time.time()
    text, usage, served = call_glm(client, model, TEST_SAMPLE, "模型推理速度", min(max_tokens, 2000))
    elapsed = time.time() - started
    print(f"响应模型：{served or model}")
    print(f"耗时：{elapsed:.1f}s")
    if usage is not None:
        print(
            f"tokens: in={getattr(usage, 'prompt_tokens', '?')} "
            f"out={getattr(usage, 'completion_tokens', '?')}"
        )
    print("\n--- 返回内容 ---")
    print(text)
    print("--- 结束 ---")
    target = Path(__file__).resolve().parent / "glm_connection_test.md"
    target.write_text(
        "# GLM 连接测试\n\n"
        f"- 端点：{base}\n- 模型：{served or model}\n"
        f"- 时间：{dt.datetime.now():%Y-%m-%d %H:%M:%S}\n\n{text}\n",
        encoding="utf-8",
    )
    print(f"\n测试结果已保存：{target}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="用智谱 GLM 润色 Bili2Text 的 Markdown")
    parser.add_argument("files", nargs="*", help="要润色的 .md 文件；留空则交互选择")
    parser.add_argument("--model", help="覆盖模型名（默认读取 MINIMAX_MODEL）")
    parser.add_argument("--base", help="覆盖 Base URL（必须是智谱端点）")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--test", action="store_true", help="只做连通性测试")
    args = parser.parse_args()

    key, base, model = resolve_config(args)
    print(f"智谱 GLM 润色 | 模型 {model} | 端点 {base}")
    client = make_client(key, base)

    if args.test:
        return run_test(client, base, model, args.max_tokens)

    sources = [Path(item).expanduser() for item in args.files] if args.files else choose_files_interactively()
    for source in sources:
        if not source.exists() or source.suffix.lower() != ".md":
            print(f"[跳过] 不是有效的 .md 文件：{source}")
            continue
        if source.stem.endswith("_refined"):
            print(f"[跳过] 已是润色文件：{source}")
            continue
        refine_file(client, model, source, args.max_tokens)

    print("\n全部完成。原始文件均未修改。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已取消。")
        raise SystemExit(130)
