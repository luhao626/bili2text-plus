import whisper
import os
import re
import ssl
import certifi
import sys
import requests
import json
import time
from dotenv import load_dotenv
from openai import OpenAI

import llm_config

# 加载.env文件
load_dotenv()

# 修复 SSL 证书验证问题 - 使用 certifi 提供的证书
try:
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    ssl._create_default_https_context = lambda: ssl_context
except Exception:
    # 如果 certifi 不可用，临时禁用验证（不推荐，但可以工作）
    ssl._create_default_https_context = ssl._create_unverified_context

# 禁用 tqdm 进度条以避免 GUI 环境中的线程问题
os.environ['TQDM_DISABLE'] = '1'

whisper_model = None
USE_OPENAI_API = bool(os.getenv("OPENAI_API_KEY")) and str(os.getenv("USE_OPENAI_WHISPER", "0")).lower() not in ("0", "false", "no")
OPENAI_MODEL = os.getenv("OPENAI_WHISPER_MODEL", "whisper-1")

# AI 润色配置：优先 LLM_*，兼容旧的 MINIMAX_*（见 llm_config.py）
LLM_SETTINGS = llm_config.load_settings()
# 兼容旧变量名
MINIMAX_API_KEY = LLM_SETTINGS.api_key
MINIMAX_API_BASE = LLM_SETTINGS.base_url
MINIMAX_MODEL = LLM_SETTINGS.model

# 停止事件（由外部设置）
stop_event = None

# Markdown 输出目录（可在界面上修改）
OUTPUT_DIR = "outputs"

def set_output_dir(path):
    """设置 Markdown 输出目录，返回规范化后的绝对路径。"""
    global OUTPUT_DIR
    if path:
        OUTPUT_DIR = str(path)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    return os.path.abspath(OUTPUT_DIR)

def set_stop_event(event):
    """设置停止事件，用于中断长时间运行的任务"""
    global stop_event
    stop_event = event

def reload_llm_settings(override=None):
    """重新读取 AI 配置（UI 修改配置后调用），返回最新设置。"""
    global LLM_SETTINGS, MINIMAX_API_KEY, MINIMAX_API_BASE, MINIMAX_MODEL
    global minimax_client, llm_client
    LLM_SETTINGS = llm_config.load_settings(override)
    MINIMAX_API_KEY = LLM_SETTINGS.api_key
    MINIMAX_API_BASE = LLM_SETTINGS.base_url
    MINIMAX_MODEL = LLM_SETTINGS.model
    llm_client = llm_config.build_client(LLM_SETTINGS)
    minimax_client = llm_client  # 旧变量名保留
    return LLM_SETTINGS

def test_llm_connection(override=None):
    """测试当前 AI 配置是否可用，返回结构化结果。"""
    settings = llm_config.load_settings(override) if override else LLM_SETTINGS
    return llm_config.test_connection(settings)

# 初始化 AI 客户端
llm_client = llm_config.build_client(LLM_SETTINGS)
minimax_client = llm_client  # 兼容旧代码

def is_cuda_available():
    return whisper.torch.cuda.is_available()

def load_whisper(model="tiny"):
    global whisper_model
    if USE_OPENAI_API:
        print("检测到 OPENAI_API_KEY，启用云端 Whisper 转写，跳过本地模型加载。")
        return
    # 彻底禁用 tqdm 以避免 GUI 环境中的线程问题
    import tqdm
    import tqdm._monitor
    
    # 禁用 tqdm 监控线程
    original_tqdm = tqdm.tqdm
    original_monitor = getattr(tqdm._monitor, 'TMonitor', None)
    
    # 创建一个禁用版本的 tqdm
    def disabled_tqdm(*args, **kwargs):
        kwargs['disable'] = True
        return original_tqdm(*args, **kwargs)
    
    # 替换 tqdm
    tqdm.tqdm = disabled_tqdm
    
    # 尝试禁用监控线程
    try:
        if hasattr(tqdm._monitor, 'TMonitor'):
            class DisabledMonitor:
                def __init__(self, *args, **kwargs):
                    pass
                def start(self):
                    pass
                def stop(self):
                    pass
            tqdm._monitor.TMonitor = DisabledMonitor
    except:
        pass
    
    try:
        whisper_model = whisper.load_model(model, device="cuda" if is_cuda_available() else "cpu")
        print("Whisper模型："+model)
    finally:
        # 恢复原始 tqdm
        tqdm.tqdm = original_tqdm
        if original_monitor:
            tqdm._monitor.TMonitor = original_monitor

def run_analysis(filename, model="tiny", prompt="以下是普通话的句子。"):
    """
    执行语音转文字分析，生成原始转写文档（不进行AI润色）。
    参数:
        filename: 音频文件夹名称
        model: Whisper模型名称（未使用，保留兼容性）
        prompt: 转写提示词
    返回:
        原始转写文本
    """
    global whisper_model
    print("正在加载Whisper模型或准备API...")
    # 读取列表中的音频文件
    audio_list = os.listdir(f"audio/slice/{filename}")
    print("模型准备完成！")
    # 添加排序逻辑
    audio_files = sorted(
        audio_list,
        key=lambda x: int(os.path.splitext(x)[0])  # 按文件名数字排序
    )
    # 创建输出文件夹
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print("正在转换文本...")

    audio_list.sort(key=lambda x: int(x.split(".")[0])) # 将 audio_list 按照切片序号排序

    texts = []
    for idx, fn in enumerate(audio_files, start=1):
        # 检查是否请求停止
        if stop_event and stop_event.is_set():
            print("任务已停止")
            raise KeyboardInterrupt("用户请求停止任务")
        
        print(f"正在转换第{idx}/{len(audio_files)}个音频... {fn}")
        slice_path = f"audio/slice/{filename}/{fn}"
        if USE_OPENAI_API:
            text = _transcribe_via_openai(slice_path, prompt)
        else:
            result = whisper_model.transcribe(slice_path, initial_prompt=prompt)
            text = "".join([seg["text"] for seg in result.get("segments", []) if seg is not None])
        print(text)
        texts.append(text)

    raw_text = "\n".join(texts)
    
    # 只保存原始转写结果
    output_path = os.path.join(OUTPUT_DIR, f"{filename}.md")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("# 转写结果（原始）\n\n")
        f.write(raw_text.strip())
        f.write("\n")
    print(f"原始转写结果已保存为 Markdown: {output_path}")
    
    return raw_text


def refine_text(filename, prompt="以下是普通话的句子。"):
    """
    对已生成的原始转写文档进行AI润色，生成新的润色文档。
    参数:
        filename: 音频文件夹名称（用于读取原始文档）
        prompt: 润色提示词
    返回:
        润色后的文本
    """
    if not MINIMAX_API_KEY:
        raise RuntimeError(
            "未配置 AI 润色密钥，无法进行AI润色。"
            "请设置 LLM_API_KEY（或兼容旧的 MINIMAX_APIKEY），"
            "或在界面「AI 润色」区域填写后重试。"
        )

    if not minimax_client:
        raise RuntimeError("AI 客户端未初始化，请检查 API Key、Base URL 与模型 ID 是否正确。")
    
    # 读取原始转写文档
    raw_file_path = os.path.join(OUTPUT_DIR, f"{filename}.md")
    if not os.path.exists(raw_file_path):
        raise FileNotFoundError(f"未找到原始转写文档: {raw_file_path}，请先执行转写。")
    
    with open(raw_file_path, "r", encoding="utf-8") as f:
        content = f.read()
        # 提取原始文本（跳过标题）
        lines = content.split("\n")
        raw_text = "\n".join([line for line in lines if not line.startswith("#")]).strip()
    
    try:
        # 检查是否请求停止
        if stop_event and stop_event.is_set():
            print("任务已停止")
            raise KeyboardInterrupt("用户请求停止任务")
        
        print(f"正在调用 AI 润色（{MINIMAX_MODEL} @ {MINIMAX_API_BASE}）...")
        refined_text = _refine_with_minimax(raw_text, prompt)
        
        # 再次检查是否请求停止
        if stop_event and stop_event.is_set():
            print("任务已停止")
            raise KeyboardInterrupt("用户请求停止任务")
        
        print("润色完成。")

        # 防止把空内容写成空文件
        if not refined_text or not refined_text.strip():
            raise RuntimeError("润色结果为空，已放弃写入文件（原始文档未改动）。")

        # 保存润色结果到新文件（不覆盖原始文档）
        refined_output_path = os.path.join(OUTPUT_DIR, f"{filename}_refined.md")
        with open(refined_output_path, "w", encoding="utf-8") as f:
            f.write("# 转写结果（AI润色版）\n\n")
            f.write(refined_text.strip())
            f.write("\n")
        print(f"AI润色结果已保存为 Markdown: {refined_output_path}")
        
        return refined_text
    except Exception as e:
        print(f"MiniMax 润色失败: {e}")
        raise


def _transcribe_via_openai(file_path: str, prompt: str) -> str:
    """使用 OpenAI Whisper API 进行转写，加速 CPU 设备的处理。"""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("缺少 OPENAI_API_KEY，无法使用云端转写。")
    headers = {"Authorization": f"Bearer {api_key}"}
    with open(file_path, "rb") as f:
        files = {"file": (os.path.basename(file_path), f, "audio/mpeg")}
        data = {"model": OPENAI_MODEL, "prompt": prompt}
        resp = requests.post("https://api.openai.com/v1/audio/transcriptions", headers=headers, data=data, files=files, timeout=300)
    if not resp.ok:
        raise RuntimeError(f"OpenAI 转写失败: {resp.status_code} {resp.text}")
    return resp.json().get("text", "")


def _estimate_tokens(text: str) -> int:
    """
    估算文本的 token 数量。
    中文大约 1.5 字符 = 1 token，英文大约 4 字符 = 1 token。
    这里使用一个简单的估算：中文字符按 1.5 计算，其他字符按 4 计算。
    """
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other_chars = len(text) - chinese_chars
    # 中文字符按 1.5 字符/token，其他按 4 字符/token
    estimated_tokens = int(chinese_chars / 1.5 + other_chars / 4)
    return estimated_tokens


def _get_model_max_tokens(model_name: str) -> int:
    """根据模型名称获取最大 token 限制。"""
    model_lower = model_name.lower()
    if "minimax" in model_lower:
        return 200000  # MiniMax M 系列支持约 200k 上下文
    elif "k2" in model_lower or "256k" in model_lower:
        return 256000  # k2 thinking 模型支持 256k
    elif "128k" in model_lower:
        return 128000
    elif "32k" in model_lower:
        return 32000
    elif "8k" in model_lower:
        return 8000
    else:
        # 默认按长上下文模型保守处理
        return 256000


def _split_text_into_chunks(text: str, max_tokens_per_chunk: int) -> list:
    """
    将文本分割成多个块，每块不超过指定的 token 数量。
    尽量在段落边界（换行符）处分割，保持语义完整性。
    """
    # 估算每块的最大字符数（保守估计，假设全是中文）
    max_chars_per_chunk = int(max_tokens_per_chunk * 1.5)
    
    chunks = []
    paragraphs = text.split('\n')
    current_chunk = []
    current_length = 0
    
    for para in paragraphs:
        para_length = len(para)
        # 如果单个段落就超过限制，需要进一步分割
        if para_length > max_chars_per_chunk:
            # 先保存当前块
            if current_chunk:
                chunks.append('\n'.join(current_chunk))
                current_chunk = []
                current_length = 0
            
            # 将长段落按句子分割
            sentences = para.split('。')
            for sent in sentences:
                sent = sent.strip()
                if not sent:
                    continue
                sent_length = len(sent)
                if current_length + sent_length + 1 > max_chars_per_chunk:
                    if current_chunk:
                        chunks.append('\n'.join(current_chunk))
                    current_chunk = [sent]
                    current_length = sent_length
                else:
                    current_chunk.append(sent)
                    current_length += sent_length + 1
        else:
            # 检查添加这个段落后是否超过限制
            if current_length + para_length + 1 > max_chars_per_chunk:
                if current_chunk:
                    chunks.append('\n'.join(current_chunk))
                current_chunk = [para]
                current_length = para_length
            else:
                current_chunk.append(para)
                current_length += para_length + 1
    
    # 添加最后一个块
    if current_chunk:
        chunks.append('\n'.join(current_chunk))
    
    return chunks


def _refine_with_minimax(text: str, prompt: str) -> str:
    """调用 MiniMax 进行润色/纠错。支持长文本自动分块处理。"""
    # 获取模型的最大 token 限制
    max_tokens = _get_model_max_tokens(MINIMAX_MODEL)
    # 估算 prompt 和 system message 的 token（约 200-300 tokens）
    prompt_tokens = _estimate_tokens(prompt) + 300
    # 根据模型大小动态调整每块的最大 token 数（为 prompt 和响应预留空间）
    if max_tokens >= 256000:
        max_text_tokens_per_chunk = 200000  # 256k 模型（k2 thinking）
    elif max_tokens >= 128000:
        max_text_tokens_per_chunk = 100000   # 128k 模型
    elif max_tokens >= 32000:
        max_text_tokens_per_chunk = 25000    # 32k 模型
    else:
        max_text_tokens_per_chunk = 5500     # 8k 模型（保守估计）
    
    # 估算整个文本的 token 数量
    total_tokens = _estimate_tokens(text)
    
    print(f"文档总长度约 {total_tokens} tokens，模型限制 {max_tokens} tokens")
    
    # 对于长上下文模型，大多数文档都可以一次性处理
    if total_tokens + prompt_tokens <= max_tokens:
        if total_tokens <= max_text_tokens_per_chunk:
            print("文档长度在模型限制内，直接处理...")
            return _refine_single_chunk(text, prompt)
    
    # 需要分块处理（对于超长文档）
    print(f"文档较长，将分块处理（每块文本约 {max_text_tokens_per_chunk} tokens）...")
    chunks = _split_text_into_chunks(text, max_text_tokens_per_chunk)
    print(f"共分为 {len(chunks)} 块进行润色")
    
    refined_chunks = []
    for idx, chunk in enumerate(chunks, 1):
        # 检查是否请求停止
        if stop_event and stop_event.is_set():
            print("任务已停止")
            # 将已处理的块和未处理的块合并
            if refined_chunks:
                return '\n\n'.join(refined_chunks) + '\n\n[任务已停止，以下内容未润色]\n\n' + '\n\n'.join(chunks[idx-1:])
            else:
                raise KeyboardInterrupt("用户请求停止任务")
        
        chunk_tokens = _estimate_tokens(chunk)
        print(f"正在润色第 {idx}/{len(chunks)} 块（约 {chunk_tokens} tokens）...")
        try:
            refined_chunk = _refine_single_chunk(chunk, prompt)
            refined_chunks.append(refined_chunk)
        except KeyboardInterrupt:
            raise  # 重新抛出停止请求
        except Exception as e:
            print(f"第 {idx} 块润色失败: {e}，保留原文")
            refined_chunks.append(chunk)  # 失败时保留原文
    
    # 合并所有润色后的块
    print("所有块润色完成，正在合并结果...")
    return '\n\n'.join(refined_chunks)


def _strip_repetition_tail(text: str) -> str:
    """
    清理模型偶发的“句子级重复循环”尾部。
    免费模型在长文本下可能连续重复同一句话数百遍，导致输出膨胀。
    这里一旦发现连续 4 句完全相同，就从重复开始处截断。
    """
    parts = re.split(r'(?<=[。！？!?\n])', text)
    prev = None
    run = 0
    for idx, part in enumerate(parts):
        sentence = part.strip()
        if sentence and sentence == prev:
            run += 1
        else:
            prev = sentence
            run = 1
        if run >= 4:
            return ''.join(parts[:idx - run + 1]).strip()
    return text.strip()


def _refine_single_chunk(text: str, prompt: str) -> str:
    """对单个文本块调用 MiniMax API 进行润色。使用 OpenAI 兼容客户端。"""
    if not minimax_client:
        raise RuntimeError("MiniMax 客户端未初始化，请检查 MINIMAX_APIKEY 配置。")

    # 输出上限按输入长度动态设定，避免模型陷入重复循环把预算耗尽
    input_tokens = _estimate_tokens(text)
    max_output_tokens = max(2000, min(32000, int(input_tokens * 2.5) + 800))

    last_error = None
    for attempt in range(1, 4):
        try:
            # 使用 OpenAI 兼容客户端调用 MiniMax API
            response = minimax_client.chat.completions.create(
                model=MINIMAX_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是一个严格的中文转写润色助手。"
                            "请仅对给定文本进行错别字、标点、断句和轻微语气调整，"
                            "保持原始语序与信息完整，不新增总结、点评、延伸内容，"
                            "不改写成新的风格，也不要删除有效信息。"
                            "如果文本分块提供，也要保持上下文衔接。"
                            "输出与输入同一语种，并保持 Markdown 中的段落结构。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"主题提示：{prompt}\n\n"
                            "以下是转写原文，请仅进行必要的纠错和轻量润色，不要总结：\n"
                            f"{text}"
                        ),
                    },
                ],
                temperature=0.3,  # 略高一点，降低长文本重复循环概率
                max_tokens=max_output_tokens,
                # 智谱 GLM 等模型默认开思考模式，会吃光输出预算，需显式关闭
                **llm_config.request_kwargs(LLM_SETTINGS),
            )
        except Exception as e:
            last_error = e
            status = getattr(e, "status_code", None)
            # 1305/1302 等限流与临时过载：退避重试
            if status in (429, 500, 502, 503, 504) and attempt < 3:
                wait = 10 * attempt
                print(f"润色请求被限流（HTTP {status}），{wait}s 后重试（第 {attempt}/3 次）...")
                time.sleep(wait)
                continue
            category, friendly = llm_config.classify_error(e)
            raise RuntimeError(f"AI 润色调用失败（{category}）：{friendly}")

        # 获取返回的内容
        if response.choices and len(response.choices) > 0:
            content = response.choices[0].message.content or ""
            # MiniMax M 系列为推理模型，剥离返回内容中的 <think>...</think> 思考过程
            content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL)
            content = _strip_repetition_tail(content)
            if not content.strip():
                raise RuntimeError(
                    "AI 返回空内容（思考模式可能未关闭或输出被截断）"
                )
            return content
        else:
            raise RuntimeError("AI 接口返回空结果")
    
