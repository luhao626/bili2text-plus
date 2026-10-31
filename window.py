"""Bili2Text Plus 桌面界面。

与原版相比的主要变化：
1. 输入区自动识别平台（B站 / 抖音 / YouTube），并支持直接粘贴抖音分享文案；
2. 转写配置、AI 润色配置、结果管理分区展示；
3. 所有耗时任务在后台线程执行，界面更新统一回到主线程（队列轮询），避免假死；
4. 结果区可预览文本、查看完整路径、打开文件夹、打开文件、复制内容；
5. AI 润色支持服务商切换与「测试连接」，并可对已有 Markdown 直接润色。
"""

from __future__ import annotations

# 高分屏适配：必须在导入 tkinter 之前声明，否则 125%/200% 缩放下界面会被压缩
import ctypes

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import os
import queue
import sys
import threading
import webbrowser

import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from tkinter import END, filedialog

import llm_config
import platforms
from exAudio import process_audio_split
from utils import download_video

speech_to_text = None  # speech2text 模块
stop_event = threading.Event()
worker_thread = None
ui_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()

current_md_path = None  # 当前结果对应的原始 Markdown
last_stem = None  # 当前任务的文件名主干（用于 AI 润色）
_orig_stdout = sys.stdout
_orig_stderr = sys.stderr

PROVIDER_ORDER = ["zhipu", "minimax", "custom"]
PROVIDER_LABELS = [llm_config.PROVIDER_PRESETS[p]["label"] for p in PROVIDER_ORDER]


# --------------------------------------------------------------------------- #
# 线程安全的 UI 消息通道
# --------------------------------------------------------------------------- #
def post(kind: str, text: str) -> None:
    ui_queue.put((kind, text))


def pump_ui() -> None:
    """在主线程里消费后台线程的消息。"""
    while True:
        try:
            kind, text = ui_queue.get_nowait()
        except queue.Empty:
            break
        if kind == "log":
            append_log(text)
        elif kind == "status":
            status_var.set(text)
        elif kind == "stage":
            stage_var.set(text)
        elif kind == "done":
            reset_running_state()
        elif kind == "result":
            show_result(text)
    app.after(120, pump_ui)


class QueueWriter:
    """把 print/异常输出重定向到界面日志（线程安全）。"""

    def __init__(self) -> None:
        self._buffer = ""

    def write(self, message: str, state: str = "INFO") -> None:
        if not message:
            return
        self._buffer += message
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                post("log", f"[{state}] {line}")

    def flush(self) -> None:
        if self._buffer.strip():
            post("log", f"[INFO] {self._buffer.strip()}")
        self._buffer = ""


def append_log(line: str) -> None:
    log_text.config(state="normal")
    log_text.insert(END, line + "\n")
    log_text.see(END)
    log_text.config(state="disabled")


# --------------------------------------------------------------------------- #
# 状态与按钮
# --------------------------------------------------------------------------- #
def set_running_state(running: bool) -> None:
    if running:
        start_button.config(state="disabled")
        refine_button.config(state="disabled")
        test_api_button.config(state="disabled")
        stop_button.config(state="normal")
    else:
        start_button.config(state="normal")
        refine_button.config(state="normal")
        test_api_button.config(state="normal")
        stop_button.config(state="disabled")


def reset_running_state() -> None:
    set_running_state(False)


def run_in_thread(target, *args) -> None:
    global worker_thread
    worker_thread = threading.Thread(target=target, args=args, daemon=True)
    worker_thread.start()


# --------------------------------------------------------------------------- #
# 平台识别
# --------------------------------------------------------------------------- #
def refresh_platform_hint(*_args) -> None:
    raw = link_entry.get().strip()
    if not raw:
        platform_var.set("平台：未识别")
        return
    try:
        ref = platforms.resolve_video_input(raw)
    except platforms.UnsupportedLinkError as error:
        platform_var.set(f"平台：未识别（{error}）")
        return
    platform_var.set(f"平台：{ref.display}")


# --------------------------------------------------------------------------- #
# AI 配置
# --------------------------------------------------------------------------- #
def current_llm_override() -> dict:
    index = provider_combo.current()
    provider = PROVIDER_ORDER[index] if index >= 0 else ""
    override = {
        "provider": provider,
        "base_url": base_entry.get().strip(),
        "model": model_entry.get().strip(),
    }
    key = key_entry.get().strip()
    if key:
        override["api_key"] = key
    return override


def apply_llm_settings() -> None:
    """把界面上的配置应用到 speech2text 模块。"""
    if speech_to_text is None:
        return
    settings = speech_to_text.reload_llm_settings(current_llm_override())
    post("log", f"[GLM] 当前 AI 配置：{settings.summary()}")


def on_provider_change(_event=None) -> None:
    index = provider_combo.current()
    if index < 0:
        return
    provider = PROVIDER_ORDER[index]
    preset = llm_config.PROVIDER_PRESETS[provider]
    base_entry.delete(0, END)
    model_entry.delete(0, END)
    if preset["base_url"]:
        base_entry.insert(0, preset["base_url"])
    if preset["model"]:
        model_entry.insert(0, preset["model"])


def worker_test_api() -> None:
    try:
        post("status", "正在测试 AI 接口…")
        settings = llm_config.load_settings(current_llm_override())
        result = llm_config.test_connection(settings)
        if result["ok"]:
            post("log", f"[成功] {result['message']}")
            post("status", f"接口正常：{result['model']}（{result['latency']}s）")
        else:
            post("log", f"[失败] {result['message']}（分类：{result['category']}）")
            post("status", f"接口测试失败：{result['message']}")
    except Exception as error:  # noqa: BLE001
        post("log", f"[错误] 测试接口异常：{error}")
        post("status", "接口测试异常，详见日志")
    finally:
        post("done", "")


def on_test_api() -> None:
    set_running_state(True)
    run_in_thread(worker_test_api)


# --------------------------------------------------------------------------- #
# Whisper
# --------------------------------------------------------------------------- #
def worker_load_whisper(model_name: str) -> None:
    global speech_to_text
    try:
        post("status", f"正在加载 Whisper 模型 {model_name}…")
        import speech2text

        speech_to_text = speech2text
        speech2text.set_output_dir(output_dir_var.get().strip() or "outputs")
        speech2text.load_whisper(model=model_name)
        speech2text.reload_llm_settings(current_llm_override())
        post("log", f"[成功] Whisper 模型已加载：{model_name}")
        post("status", f"Whisper 就绪（{model_name}）")
    except Exception as error:  # noqa: BLE001
        post("log", f"[错误] 加载 Whisper 失败：{error}")
        post("status", "Whisper 加载失败，详见日志")
    finally:
        post("done", "")


def on_load_whisper() -> None:
    set_running_state(True)
    run_in_thread(worker_load_whisper, model_var.get())


# --------------------------------------------------------------------------- #
# 转写主流程
# --------------------------------------------------------------------------- #
def worker_process(link: str) -> None:
    global last_stem
    try:
        if speech_to_text is None:
            post("log", "[错误] 请先点击「加载模型」")
            post("status", "Whisper 未加载")
            return
        if speech_to_text.whisper_model is None and not speech_to_text.USE_OPENAI_API:
            post("log", "[错误] Whisper 模型尚未加载完成")
            post("status", "Whisper 未加载")
            return

        speech_to_text.set_output_dir(output_dir_var.get().strip() or "outputs")
        speech_to_text.set_stop_event(stop_event)

        post("stage", "① 下载视频")
        post("status", "正在下载视频…")
        file_identifier = download_video(link, progress=lambda message: post("status", message))
        if file_identifier is None:
            post("log", "[失败] 视频下载失败，请查看上方日志中的具体原因")
            post("status", "下载失败")
            return
        if stop_event.is_set():
            post("log", "[停止] 任务已在下载后停止")
            return

        post("stage", "② 提取并切分音频")
        post("status", "正在提取音频…")
        folder_name = process_audio_split(file_identifier)
        last_stem = folder_name
        if stop_event.is_set():
            post("log", "[停止] 任务已在音频处理后停止")
            return

        post("stage", "③ Whisper 转写")
        post("status", "正在转写（耗时较长，请耐心等待）…")
        speech_to_text.run_analysis(
            folder_name,
            prompt=f"以下是普通话的句子。这是一个关于{file_identifier}的视频。",
        )

        output_path = os.path.join(speech_to_text.OUTPUT_DIR, f"{folder_name}.md")
        post("result", output_path)
        post("log", f"[完成] 原始转写已保存：{os.path.abspath(output_path)}")
        post("stage", "④ 完成")
        post("status", "转写完成，可点击「AI 修订」生成润色版")
    except KeyboardInterrupt:
        post("log", "[停止] 任务已停止")
        post("status", "任务已停止")
    except Exception as error:  # noqa: BLE001
        if stop_event.is_set():
            post("log", "[停止] 任务已停止")
            post("status", "任务已停止")
        else:
            post("log", f"[错误] 处理失败：{error}")
            post("status", "处理失败，详见日志")
            import traceback

            post("log", traceback.format_exc())
    finally:
        post("done", "")


def on_start() -> None:
    raw = link_entry.get().strip()
    if not raw:
        post("log", "[提示] 请输入视频链接或粘贴分享文案")
        return
    if speech_to_text is None:
        post("log", "[提示] 请先点击「加载模型」")
        return
    try:
        ref = platforms.resolve_video_input(raw)
    except platforms.UnsupportedLinkError as error:
        post("log", f"[提示] {error}")
        return

    stop_event.clear()
    post("log", f"[开始] 目标：{ref.display}")
    post("log", f"[开始] 链接：{ref.canonical_url}")
    post("log", f"[开始] 缓存目录：bilibili_video/{ref.cache_key}")
    set_running_state(True)
    run_in_thread(worker_process, raw)


def on_stop() -> None:
    stop_event.set()
    post("log", "[停止] 已发送停止请求，当前步骤结束后停止")


# --------------------------------------------------------------------------- #
# AI 润色
# --------------------------------------------------------------------------- #
def worker_refine(stem: str) -> None:
    try:
        if speech_to_text is None:
            post("log", "[错误] 请先加载 Whisper（仅为初始化模块）")
            return
        apply_llm_settings()
        post("stage", "④ AI 润色")
        post("status", "正在调用 AI 润色…")
        speech_to_text.set_stop_event(stop_event)
        speech_to_text.refine_text(stem, prompt=f"以下是普通话的句子。这是一个关于{stem}的视频。")
        refined_path = os.path.join(speech_to_text.OUTPUT_DIR, f"{stem}_refined.md")
        post("result", refined_path)
        post("log", f"[完成] 润色文档已保存：{os.path.abspath(refined_path)}")
        post("status", "润色完成")
    except KeyboardInterrupt:
        post("log", "[停止] 润色已停止")
        post("status", "润色已停止")
    except Exception as error:  # noqa: BLE001
        post("log", f"[错误] 润色失败：{error}")
        post("status", "润色失败，详见日志")
    finally:
        post("done", "")


def on_refine() -> None:
    stem = last_stem
    if current_md_path:
        candidate = os.path.splitext(os.path.basename(current_md_path))[0]
        if candidate.endswith("_refined"):
            candidate = candidate[: -len("_refined")]
        stem = candidate
    if not stem:
        post("log", "[提示] 请先完成一次转写，或用「选择已有 Markdown」指定文件")
        return

    stop_event.clear()
    set_running_state(True)
    run_in_thread(worker_refine, stem)


# --------------------------------------------------------------------------- #
# 结果管理
# --------------------------------------------------------------------------- #
def show_result(path: str) -> None:
    global current_md_path
    current_md_path = path
    result_path_var.set(os.path.abspath(path))
    try:
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()
    except OSError as error:
        append_log(f"[错误] 无法读取结果文件：{error}")
        return
    preview_text.config(state="normal")
    preview_text.delete("1.0", END)
    preview_text.insert(END, content)
    preview_text.config(state="disabled")


def on_pick_markdown() -> None:
    initial = output_dir_var.get().strip() or "outputs"
    path = filedialog.askopenfilename(
        title="选择要润色的 Markdown",
        initialdir=initial if os.path.isdir(initial) else ".",
        filetypes=[("Markdown", "*.md")],
    )
    if not path:
        return
    global last_stem
    last_stem = os.path.splitext(os.path.basename(path))[0]
    if last_stem.endswith("_refined"):
        last_stem = last_stem[: -len("_refined")]
    if speech_to_text is not None:
        speech_to_text.set_output_dir(os.path.dirname(path) or ".")
    output_dir_var.set(os.path.dirname(path))
    show_result(path)
    append_log(f"[选择] 已载入：{path}")


def on_pick_output_dir() -> None:
    path = filedialog.askdirectory(title="选择 Markdown 保存目录")
    if not path:
        return
    output_dir_var.set(path)
    if speech_to_text is not None:
        speech_to_text.set_output_dir(path)
    append_log(f"[设置] 输出目录：{path}")


def on_open_folder() -> None:
    path = result_path_var.get().strip()
    folder = os.path.dirname(path) if path else (output_dir_var.get().strip() or "outputs")
    if not os.path.isdir(folder):
        append_log(f"[提示] 目录不存在：{folder}")
        return
    os.startfile(folder)  # noqa: S606 - Windows 桌面应用


def on_open_file() -> None:
    path = result_path_var.get().strip()
    if not path or not os.path.isfile(path):
        append_log("[提示] 还没有可打开的结果文件")
        return
    os.startfile(path)  # noqa: S606


def on_copy_text() -> None:
    content = preview_text.get("1.0", END).strip()
    if not content:
        append_log("[提示] 预览区没有内容")
        return
    app.clipboard_clear()
    app.clipboard_append(content)
    append_log("[复制] 结果文本已复制到剪贴板")


def on_clear_log() -> None:
    log_text.config(state="normal")
    log_text.delete("1.0", END)
    log_text.config(state="disabled")


def open_github_link(_event=None) -> None:
    webbrowser.open_new("https://github.com/beingaigital/bili2text")


# --------------------------------------------------------------------------- #
# 界面
# --------------------------------------------------------------------------- #
def build_ui(selftest: bool = False) -> None:
    global app, log_text, preview_text, status_var, stage_var, platform_var
    global link_entry, model_var, output_dir_var, result_path_var
    global provider_combo, base_entry, model_entry, key_entry
    global start_button, stop_button, refine_button, test_api_button

    app = ttk.Window(title="Bili2Text Plus — 多平台视频转文字助手", themename="litera")
    # 按 DPI 缩放窗口与字体度量，避免高分屏下界面过小
    try:
        dpi = ctypes.windll.user32.GetDpiForSystem()
    except Exception:
        dpi = 96
    scale = max(1.0, dpi / 96.0)
    app.tk.call("tk", "scaling", scale)
    app.geometry(f"{int(1120 * scale)}x{int(840 * scale)}")
    app.minsize(int(980 * scale), int(720 * scale))
    try:
        app.iconbitmap("favicon.ico")
    except Exception:
        pass

    status_var = ttk.StringVar(value="就绪")
    stage_var = ttk.StringVar(value="等待任务")
    platform_var = ttk.StringVar(value="平台：未识别")
    output_dir_var = ttk.StringVar(value="outputs")
    result_path_var = ttk.StringVar(value="")

    # ---------------- 标题 ----------------
    header = ttk.Frame(app)
    header.pack(fill=X, padx=16, pady=(12, 4))
    ttk.Label(header, text="Bili2Text Plus", font=("Microsoft YaHei UI", 18, "bold")).pack(side=LEFT)
    ttk.Label(
        header,
        text="B站 / 抖音 / YouTube · Whisper 转写 · AI 润色",
        bootstyle="secondary",
    ).pack(side=LEFT, padx=12)

    # ---------------- 视频输入 ----------------
    input_box = ttk.Labelframe(app, text=" 视频输入 ")
    input_box.pack(fill=X, padx=16, pady=6)
    row = ttk.Frame(input_box)
    row.pack(fill=X, padx=10, pady=8)
    link_entry = ttk.Entry(row)
    link_entry.pack(side=LEFT, fill=X, expand=YES)
    link_entry.bind("<KeyRelease>", refresh_platform_hint)
    start_button = ttk.Button(row, text="开始转写", bootstyle="primary", command=on_start)
    start_button.pack(side=LEFT, padx=6)
    stop_button = ttk.Button(row, text="停止", bootstyle="danger-outline", command=on_stop, state="disabled")
    stop_button.pack(side=LEFT)
    ttk.Label(input_box, textvariable=platform_var, bootstyle="info").pack(anchor=W, padx=12, pady=(0, 8))

    # ---------------- 转写配置 ----------------
    cfg_box = ttk.Labelframe(app, text=" 转写配置 ")
    cfg_box.pack(fill=X, padx=16, pady=6)
    cfg_row = ttk.Frame(cfg_box)
    cfg_row.pack(fill=X, padx=10, pady=8)
    ttk.Label(cfg_row, text="Whisper 模型").pack(side=LEFT)
    model_var = ttk.StringVar(value="small")
    ttk.Combobox(
        cfg_row, textvariable=model_var, values=["tiny", "base", "small", "medium", "large"], width=8
    ).pack(side=LEFT, padx=6)
    ttk.Button(cfg_row, text="加载模型", bootstyle="success-outline", command=on_load_whisper).pack(side=LEFT)
    ttk.Label(cfg_row, text="保存目录").pack(side=LEFT, padx=(16, 4))
    ttk.Entry(cfg_row, textvariable=output_dir_var, width=28).pack(side=LEFT)
    ttk.Button(cfg_row, text="选择…", bootstyle="secondary-outline", command=on_pick_output_dir).pack(side=LEFT, padx=4)

    # ---------------- AI 润色 ----------------
    ai_box = ttk.Labelframe(app, text=" AI 润色 ")
    ai_box.pack(fill=X, padx=16, pady=6)
    ai_row = ttk.Frame(ai_box)
    ai_row.pack(fill=X, padx=10, pady=(8, 4))
    ttk.Label(ai_row, text="服务商").pack(side=LEFT)
    provider_combo = ttk.Combobox(ai_row, values=PROVIDER_LABELS, width=16, state="readonly")
    provider_combo.pack(side=LEFT, padx=6)
    provider_combo.bind("<<ComboboxSelected>>", on_provider_change)

    ttk.Label(ai_row, text="模型").pack(side=LEFT, padx=(8, 2))
    model_entry = ttk.Entry(ai_row, width=18)
    model_entry.pack(side=LEFT)

    ttk.Label(ai_row, text="API Key").pack(side=LEFT, padx=(8, 2))
    key_entry = ttk.Entry(ai_row, width=22, show="*")
    key_entry.pack(side=LEFT)

    ai_row2 = ttk.Frame(ai_box)
    ai_row2.pack(fill=X, padx=10, pady=(0, 8))
    ttk.Label(ai_row2, text="Base URL").pack(side=LEFT)
    base_entry = ttk.Entry(ai_row2)
    base_entry.pack(side=LEFT, fill=X, expand=YES, padx=6)
    test_api_button = ttk.Button(ai_row2, text="测试连接", bootstyle="info-outline", command=on_test_api)
    test_api_button.pack(side=LEFT, padx=4)
    refine_button = ttk.Button(ai_row2, text="AI 修订", bootstyle="info", command=on_refine)
    refine_button.pack(side=LEFT, padx=4)
    ttk.Button(
        ai_row2, text="选择已有 Markdown", bootstyle="secondary-outline", command=on_pick_markdown
    ).pack(side=LEFT)

    settings = llm_config.load_settings()
    if settings.provider in PROVIDER_ORDER:
        provider_combo.current(PROVIDER_ORDER.index(settings.provider))
    base_entry.insert(0, settings.base_url)
    model_entry.insert(0, settings.model)

    # ---------------- 状态栏 ----------------
    state_row = ttk.Frame(app)
    state_row.pack(fill=X, padx=16, pady=(2, 6))
    ttk.Label(state_row, textvariable=stage_var, bootstyle="primary").pack(side=LEFT)
    ttk.Label(state_row, text="｜").pack(side=LEFT, padx=4)
    ttk.Label(state_row, textvariable=status_var, bootstyle="secondary").pack(side=LEFT)

    # ---------------- 结果区 ----------------
    result_box = ttk.Labelframe(app, text=" 结果管理 ")
    result_box.pack(fill=BOTH, expand=YES, padx=16, pady=6)
    path_row = ttk.Frame(result_box)
    path_row.pack(fill=X, padx=10, pady=(8, 4))
    ttk.Label(path_row, text="输出文件").pack(side=LEFT)
    ttk.Entry(path_row, textvariable=result_path_var, state="readonly").pack(
        side=LEFT, fill=X, expand=YES, padx=6
    )
    ttk.Button(path_row, text="打开文件夹", bootstyle="secondary-outline", command=on_open_folder).pack(side=LEFT, padx=2)
    ttk.Button(path_row, text="打开文件", bootstyle="secondary-outline", command=on_open_file).pack(side=LEFT, padx=2)
    ttk.Button(path_row, text="复制文本", bootstyle="secondary-outline", command=on_copy_text).pack(side=LEFT, padx=2)
    ttk.Button(path_row, text="清空日志", bootstyle="secondary-outline", command=on_clear_log).pack(side=LEFT, padx=2)

    notebook = ttk.Notebook(result_box)
    notebook.pack(fill=BOTH, expand=YES, padx=10, pady=(0, 10))

    preview_frame = ttk.Frame(notebook)
    preview_text = ttk.ScrolledText(preview_frame, height=16, state="disabled", wrap="word")
    preview_text.pack(fill=BOTH, expand=YES)
    notebook.add(preview_frame, text="文本预览")

    log_frame = ttk.Frame(notebook)
    log_text = ttk.ScrolledText(log_frame, height=16, state="disabled", wrap="word")
    log_text.pack(fill=BOTH, expand=YES)
    notebook.add(log_frame, text="任务日志")

    footer = ttk.Frame(app)
    footer.pack(fill=X, padx=16, pady=(0, 10))
    ttk.Label(footer, text="版本 2.1.0-plus", bootstyle="secondary").pack(side=LEFT)
    github_label = ttk.Label(footer, text="开源仓库", bootstyle="primary", cursor="hand2")
    github_label.pack(side=LEFT, padx=12)
    github_label.bind("<Button-1>", open_github_link)

    append_log(f"[配置] {settings.summary()}")
    if not settings.configured:
        append_log("[配置] 未检测到 AI 密钥，仍可正常生成原始转写（AI 修订不可用）")

    app.after(120, pump_ui)
    if selftest:
        app.after(900, app.destroy)
    app.mainloop()


def main() -> None:
    sys.stdout = QueueWriter()
    sys.stderr = QueueWriter()
    build_ui(selftest="--selftest" in sys.argv)


if __name__ == "__main__":
    main()
