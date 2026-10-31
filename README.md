<p align="center">
  <img src="light_logo2.png" alt="bili2text logo" width="400"/>
</p>


<p align="center">
    <img src="https://img.shields.io/github/stars/lanbinshijie/bili2text" alt="GitHub stars"/>
    <img src="https://img.shields.io/github/license/lanbinshijie/bili2text" alt="GitHub"/>
    <img src="https://img.shields.io/github/last-commit/lanbinshijie/bili2text" alt="GitHub last commit"/>
    <img src="https://img.shields.io/github/v/release/lanbinshijie/bili2text" alt="GitHub release (latest by date)"/>
</p>

# Bili2text 📺

## Bili2Text Plus（本分支新增功能）✨

本分支（`feature/douyin-support`）在原版基础上做了以下增强，详细说明见 [docs/IMPROVEMENTS.md](docs/IMPROVEMENTS.md)，测试记录见 [docs/TEST_REPORT.md](docs/TEST_REPORT.md)：

- 🎬 **抖音支持**：直接粘贴抖音分享文案（含中文说明和短链）即可转写，自动提取链接并解析短链跳转
- 📁 **本地文件兜底**：本机实测抖音链接会被平台接口以 403 拒绝（带有效 cookies 也一样，见测试报告 2.4）。此时可在浏览器里正常下载视频，然后点「选择文件…」或直接粘贴本地路径，转写流程完全一致（支持 mp4/mov/mkv/flv/webm/avi 等视频与 mp3/m4a/wav 等音频）。默认**复制**一份到缓存，**原文件绝不会被修改**；超大文件可设 `BILI2TEXT_LOCAL_IMPORT=hardlink` 改为同盘硬链接（零拷贝，但与原文件共享数据）
- 🧩 **B站分P修复**：`https://www.bilibili.com/video/BVxxx?p=33` 不再丢失分P参数，每个分P使用独立缓存目录，互不覆盖
- 🤖 **通用 AI 润色配置**：支持智谱 GLM / MiniMax / 任意 OpenAI 兼容接口，环境变量 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`（完全兼容旧的 `MINIMAX_*` 变量），界面内可切换并一键「测试连接」
- 🖥️ **界面重构**：视频输入（含平台识别）、转写配置、AI 润色、结果管理四个分区；支持文本预览、打开文件/文件夹、复制结果；修复高分屏（125%/150%/200% 缩放）下界面被压缩的问题
- 🛡️ **润色健壮性**：自动关闭思考模式（避免输出为空）、429 限流自动重试、重复内容尾部清理、空结果拒绝落盘
- ✅ **自动化测试**：148 个单元测试（`python -m unittest discover -s tests`），覆盖链接解析、本地文件接入、缓存隔离、错误分类、字幕解析与笔记校验等

运行方式与原版一致（`python window.py`），启动界面见 [docs/screenshots/ui-main.png](docs/screenshots/ui-main.png)。

## V2：人工字幕优先 · 可溯源智能笔记 ✨

当前分支 `feature/subtitles-and-study-notes` 在上一分支基础上新增两项功能，设计与取舍见 [docs/V2_DESIGN.md](docs/V2_DESIGN.md)，实测记录见 [docs/V2_TEST_REPORT.md](docs/V2_TEST_REPORT.md)：

- 📝 **人工字幕优先，Whisper 兜底**：有可靠人工字幕时直接取字幕生成 Markdown，**不下载视频、不跑 Whisper**；没有可靠字幕时自动回退原有转写流程。三种模式：智能模式 / 强制 Whisper / 仅提取字幕
  - 严格区分 `human_subtitle`、`automatic_subtitle`、`unknown_subtitle`，自动字幕绝不冒充人工字幕
  - B站按**目标分P的 cid** 取字幕，`?p=33` 不会误用第 1P 的字幕
  - 支持 SRT / VTT / B站字幕 JSON，校验空字幕、非法时间码与覆盖率不足
- 🧠 **可溯源智能笔记**：把转写生成 `study_notes.md`，正文**只有**「章节结构」与「核心知识点」两个栏目
  - AI 只返回结构（章节 + 知识点 + `segment_id`），**证据文字由程序从原文回填**，不采用 AI 编造的引文
  - 引用不存在的 ID 直接丢弃；引文与原文不一致时标记「待核实」
  - 长视频自动分块提取后合并去重，任一块失败会在文件顶部标注"只覆盖部分原文"
- 📄 **旧 Markdown 也能用**：选择 Bili2Text 以前生成的 `.md` 即可生成笔记，不重新下载/转写，**原文件只读**；无时间信息时用「原文第 N 段」定位，绝不编造时间戳
- 🧾 **统一数据契约**：每个任务输出 `transcript.json`（`schema_version: 2.0`，毫秒时间 + 来源类型 + 时间精度），JSON 是溯源事实来源，Markdown 只是展示


## 转移说明
因为作者的旧账号（lanbinshijie）已经停用，仓库已经转移到新账号（lanbinleo）

感谢各位的支持，如果有任何想法欢迎在issue中提出，或者提交pr~

v2版本开发进度，请查看dev分支；v3版本更名为v2版本

![alt text](./assets/new_v_sc.png)

## 简介 🌟
bili2text 是一个用于将视频（Bilibili、YouTube等）转换为文本的工具🛠️。这个项目通过一个简单的流程实现：下载视频、提取音频、分割音频，并使用 whisper 模型将语音转换为文本。支持AI润色功能，让转写结果更加准确和流畅。整个过程是自动的，只需输入视频链接即可。整个过程行云流水，一步到胃😂

## 功能 🚀
- 🎥**下载视频**：支持从 Bilibili 和 YouTube 下载视频，优先下载 MP4 格式，支持多P视频的下载。
- 🎵**提取音频**：从下载的视频中提取音频。
- 💬**音频分割**：将音频分割成小段，以便于进行高效的语音转文字处理。
- 🤖**语音转文字**：使用 OpenAI 的 whisper 模型将音频转换为文本，生成原始转写文档。
- ✨**AI润色**：使用 MiniMax API 对转写结果进行智能润色，修正错别字、优化断句和表达，生成更精炼的文档。

## 使用方法 📘

### 环境要求

| 依赖 | 版本 / 说明 |
| --- | --- |
| Python | 3.10 – 3.12（本项目在 3.10 验证） |
| FFmpeg | 需要 `ffmpeg` 在 PATH 中（音频提取与合并依赖它） |
| yt-dlp | 由 `requirements.txt` 安装；若下载失效，可 `pip install -U yt-dlp` |
| Whisper 模型 | 首次使用会在 `~/.cache/whisper` 自动下载（`small` 约 480MB） |

### 1. 克隆并安装

```bash
git clone https://github.com/<你的用户名>/bili2text.git
cd bili2text
git checkout feature/douyin-support
```

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

Windows 上如果 `ffmpeg` 不在 PATH，请从 [ffmpeg.org](https://ffmpeg.org/download.html) 下载后把 `bin` 目录加入 PATH。

### 2. 配置 AI 润色（可选）

不配置也能正常下载与转写，只生成原始 Markdown。需要润色时二选一：

**方式 A：`.env` 文件**（复制 `.env.example` 后填写）

```bash
cp .env.example .env
```

```ini
LLM_API_KEY=你的密钥
LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
LLM_MODEL=glm-4.7-flash
```

**方式 B：界面填写**（「AI 润色」区域直接输入，不回显、不写入文件）

> 旧版本变量 `MINIMAX_APIKEY` / `MINIMAX_API_BASE` / `MINIMAX_MODEL` 仍然可用，程序会自动回退读取，因此原有配置无需迁移。`.env` 已在 `.gitignore` 中，不会被提交。

### 3. 启动

Windows 可直接双击 `Bili2Text-Plus.cmd`，或：

```bash
python window.py
```

命令行方式（无界面）：

```bash
python main.py
```

在提示时输入视频链接或 BV 号。

### 4. 界面操作流程

1. 在「视频输入」粘贴链接或抖音分享文案，右侧会自动显示识别到的平台（B站 / 抖音 / YouTube / 本地文件）与分P；
2. 平台下载被拒绝时（例如抖音返回 403），点「选择文件…」选中本地视频/音频，或直接把文件路径粘贴进输入框；
3. 在「字幕设置」选择文字获取方式（默认「智能模式」：有人工字幕就不下载不转写，没有才用 Whisper）；
4. 选择 Whisper 模型并点击「加载模型」（仅智能模式回退或强制 Whisper 时需要）；
5. 点击「开始转写」，等待完成；界面会显示当前文字来源与回退原因；
6. 需要润色时点击「AI 修订」，生成 `*_refined.md`（**永不覆盖原始文件**）；
7. 需要笔记时点击「生成智能笔记」；也可以点「选择 Markdown 生成笔记…」直接对旧文件生成；
8. 结果区可预览文本、查看完整路径、打开文件 / 文件夹、复制结果。

### 5. 运行测试

```bash
python -m unittest discover -s tests
```

148 个单元测试全部使用 mock，不访问任何视频平台、不调用真实 AI 接口。

### 6. 输出文件位置

V2 任务按任务目录组织（重跑不会覆盖上一次结果），V1 的 `outputs/*.md` 原样保留：

```
outputs/
  <task_id>/                    # 时间戳_缓存键，例如 20261031165129_yt_aircAruvnKk
    transcript.md               # 原始字幕或 Whisper 转写
    transcript.json             # 可溯源时间轴（事实来源）
    study_notes.md              # 章节结构 + 核心知识点
    task_manifest.json          # 任务状态、模型、来源、文件清单（不含密钥）
  20261031133846.md             # V1 历史转写，原样保留
  20261031133846_refined.md     # V1 AI 润色版
```

媒体缓存分别在 `bilibili_video/<缓存键>/` 与 `audio/`，均已被 `.gitignore` 排除。

## 示例 📋

```python
from exAudio import process_audio_split
from speech2text import load_whisper, run_analysis, refine_text
from utils import download_video

url = "https://www.bilibili.com/video/BV1vwME6AEpd?p=33"
cache_key = download_video(url)            # -> BV1vwME6AEpd_p33
folder = process_audio_split(cache_key)    # 提取并切分音频
load_whisper("small")
run_analysis(folder, prompt="以下是普通话的句子。")
refine_text(folder)                        # 生成 outputs/<folder>_refined.md
```

## 技术栈 🧰
- [Python](https://www.python.org/) 主要编程语言，负责实现程序逻辑功能
- [Whisper](https://github.com/openai/whisper) 语音转文字模型
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) 视频下载工具，支持 Bilibili、抖音、YouTube 等多种平台
- [智谱 GLM](https://open.bigmodel.cn/) / [MiniMax](https://platform.minimaxi.com/) / 任意 OpenAI 兼容接口，提供 AI 润色
- [Tkiner](https://docs.python.org/3/library/tkinter.html) UI界面展示相关工具
- [TTKbootstrap](https://ttkbootstrap.readthedocs.io/en/latest/zh/) UI界面美化库

## 功能特点 ✨

### 视频下载
- ✅ 支持 Bilibili 视频下载（BV号或完整链接，`?p=` 分P精确区分）
- ⚠️ 抖音视频下载：支持解析网页链接、App 分享文案与短链；但**本机实测**命令行直连会被平台接口返回 403（带有效 cookies 也一样，见 `docs/TEST_REPORT.md` 2.4），此时请改用下面的本地文件方式
- ✅ 支持 YouTube 视频下载
- ✅ 支持**本地视频/音频文件**直接转写（`选择文件…` 或粘贴路径），用于平台拒绝命令行访问时的兜底
- ✅ 优先下载 MP4 格式，确保兼容性
- ✅ 自动处理多P视频，每个分P使用独立缓存，互不覆盖

### 文本转写
- ✅ 使用 Whisper 模型进行高精度语音转文字
- ✅ 支持本地模型和 OpenAI API 云端转写
- ✅ 自动分割长音频，提高处理效率
- ✅ 生成 Markdown 格式的原始转写文档

### 字幕与智能笔记（V2）
- ✅ **人工字幕优先**：有可靠人工字幕时直接生成 Markdown，不下载视频、不运行 Whisper
- ✅ **三种文字获取方式**：智能模式 / 强制 Whisper / 仅提取字幕（后者失败即报错，绝不偷偷转写）
- ✅ **来源可核实**：区分人工字幕、自动字幕、未知来源，界面显示来源与回退原因
- ✅ **B站分P隔离**：按目标分P的 cid 查询字幕，`?p=33` 不会误用第 1P
- ✅ **字幕校验**：SRT / VTT / B站 JSON 解析，检查空字幕、非法时间码与覆盖率
- ✅ **可溯源笔记**：只输出「章节结构」「核心知识点」，证据由程序按 `segment_id` 从原文回填
- ✅ **无效引用可拒绝**：AI 编造的 ID 直接丢弃，引文与原文不符时标注「待核实」
- ✅ **旧 Markdown 兼容**：直接对历史 `.md` 生成笔记，原文件只读、无时间信息时不编造时间

### AI润色
- ✅ 支持智谱 GLM（含免费 `glm-4.7-flash`）、MiniMax 或任意 OpenAI 兼容接口
- ✅ 界面可切换服务商并「测试连接」，错误分类为中文提示
- ✅ 自动关闭思考模式、429 限流重试、重复尾部清理，避免空输出
- ✅ 润色文档独立保存为 `*_refined.md`，不覆盖原始文档

## 后续开发计划 📅

- [X] 生成requirements.txt
- [X] UI化设计
- [X] 支持YouTube视频下载
- [X] 优先下载MP4格式
- [X] AI润色功能


## 运行截图 📷
<!-- assets/screenshot1.png -->
<img src="assets/screenshot3.png" alt="screenshot3" width="600"/>
<img src="assets/screenshot2.png" alt="screenshot2" width="600"/>
<img src="assets/screenshot1.png" alt="screenshot1" width="600"/>

## Star History ⭐

[![Star History Chart](https://api.star-history.com/svg?repos=lanbinshijie/bili2text&type=Date)](https://star-history.com/#lanbinshijie/bili2text&Date)



## 许可证 📄
本项目根据 MIT 许可证发布。

## 贡献 💡
如果你想为这个项目做出贡献，欢迎提交 Pull Request 或创建 Issue。

## 投喂一下！

> TKTg2T7u7xdV4xDAzbzird2qmWoqLanbin

![image](https://github.com/user-attachments/assets/412470b8-7fd5-4632-a085-9c48a9d5e18b)

TRC20链！谢谢大家！

## 致谢 🙏
再此感谢Open Teens对青少年开源社区做出的贡献！[@OpenTeens](https://openteens.org)

## 使用须知 🖥️

**用户在使用 bili2text 工具时，必须遵守用户所在地区的相关版权法律和规定。请确保您有权利下载和转换的视频内容，尊重创作者的劳动成果。**
