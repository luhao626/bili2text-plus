# Bili2Text Plus V2 测试报告

测试时间：2026-10-31 16:20–17:10　环境：Windows 11 / Python 3.10 / Whisper small / 智谱 `glm-4.7-flash`

---

## 一、自动化单元测试

| 测试文件 | 用例数 | 覆盖内容 |
| --- | --- | --- |
| `tests/test_platforms.py` | 34 | 链接解析、本地文件路径、缓存键隔离（V1） |
| `tests/test_utils_download.py` | 20 | 下载错误翻译、缓存、Cookie 参数（V1） |
| `tests/test_llm_config.py` | 18 | AI 配置优先级、错误分类（V1） |
| `tests/test_speech2text_units.py` | 8 | 分块、重复尾部清理、空内容防护（V1） |
| `tests/test_transcript.py` | 17 | 数据契约：序列化、校验规则、时间标签、覆盖率、原子写入 |
| `tests/test_subtitles.py` | 23 | SRT/VTT/B站JSON 解析、非法时间码、来源判别、分P cid 隔离、三种模式 |
| `tests/test_study_notes.py` | 20 | JSON 解析、无效引用丢弃、待核实标记、两栏目渲染、分块合并、失败保护 |
| `tests/test_legacy_markdown.py` | 8 | 只读导入、段落引用、无假时间、任务产物 |

**结果：148 / 148 通过（`OK`，1.3s）。** V1 原有 80 项全部保留并通过；V2 新增 68 项，
全部使用固定样本与 mock，不访问视频平台、不调用真实 AI。

---

## 二、真实端到端测试

所有任务输出在 `outputs/<task_id>/`，可随时打开核验；`transcript.json` 均通过
`pipeline.load_document()` 回读校验（0 个问题）。

### 测试 A：有人工字幕的视频 ✅

- 视频：`https://www.youtube.com/watch?v=aircAruvnKk`（3Blue1Brown，字幕来源可核实：
  yt-dlp `subtitles` 字典 = 上传者/人工提供，语言 zh-CN，视频时长 18:40）
- 命令：`python tools/v2_e2e.py --url ... --mode smart --notes`（`YTDLP_PROXY` 指向本机代理）
- 结果目录：`outputs/20261031165129_yt_aircAruvnKk/`

| 检查项 | 结果 |
| --- | --- |
| 选择人工字幕 | ✅ `human_subtitle / zh-CN`，证据写入 `provenance.track_evidence` |
| 不执行 Whisper / 不下载视频 | ✅ 全程无下载与转写日志，任务在字幕解析后直接完成 |
| `transcript.md` / `transcript.json` | ✅ 290 段，时间码为字幕原始毫秒值（`exact`） |
| 覆盖率校验 | ✅ 90.4%，`verdict=ok` |
| 智能笔记 | ✅ 5 章节 / 7 知识点 / 丢弃引用 0 / 完整=True，仅两个栏目 |

### 测试 B：无可靠字幕 → 自动回退 Whisper ✅

与测试 C 同一素材（该分P 实测无任何字幕轨道，正是"无可靠字幕"的真实样本）：

- 智能模式先查字幕：日志 `[字幕] 没有发现任何字幕轨道` → 自动进入 Whisper；
- 转写 114 段，**全部 `time_precision=approximate`**，`transcript.md` 明确标注
  "按 45 秒切片估算，为近似位置"，笔记中时间显示为 `≈[03:21]`；
- 基于该转写生成笔记：3 章节 / 6 知识点 / 丢弃引用 0 / 完整=True。

### 测试 C：B站分P `?p=33` ✅

- 链接：`https://www.bilibili.com/video/BV1vwME6AEpd?p=33`
- 结果目录：`outputs/20261031170309_BV1vwME6AEpd_p33/`

| 检查项 | 结果 |
| --- | --- |
| 保留第 33P | ✅ `transcript.json` 中 `page=33`、`url` 带 `?p=33`、标题为第 33P 的分P名 |
| 字幕按目标分P查询 | ✅ 以该分P cid（`39776879310`，与第 1P 的 `39776815200` 不同）请求字幕接口 |
| 不误用第 1P 字幕 | ✅ 该分P 无字幕 → 回退 Whisper，不存在串台可能 |
| 无可靠字幕时走 Whisper | ✅ 复用已缓存视频 → 切音 → Whisper small → 114 段 |

### 测试 D：旧 Markdown 生成笔记 ✅

- 输入：`outputs/20261031150524.md`（V1 时期生成的真实转写，6.5KB）
- 命令：`python tools/v2_e2e.py --legacy outputs/20261031150524.md --notes`
- 结果目录：`outputs/legacy_20261031170234_20261031150524/`

| 检查项 | 结果 |
| --- | --- |
| 不重新转写 | ✅ 直接读取 Markdown，6 段，无网络下载/Whisper 日志 |
| 原文件不被修改 | ✅ SHA256 前后一致（`5ce7c1fe…`） |
| 章节与知识点 | ✅ 3 章节 / 7 知识点 |
| 原文证据 | ✅ 以「原文第 N 段」引用，文字与原文逐字一致 |
| 不显示虚构时间 | ✅ 全文 0 处 `[mm:ss]`，均为段落定位，`time_precision=unknown` |

### 测试 E：AI 引用异常（构造场景）✅

构造一个真实转写 + 模型返回坏数据（不存在的 `seg_0404`/`seg_0405`、编造引文、
多余的 `overview` 栏目），验证程序行为：

| 场景 | 程序行为 |
| --- | --- |
| 章节引用不存在的 ID | ❌ 整章丢弃（0 个有效引用的章节不留空壳） |
| 知识点引用不存在的 ID | ❌ 丢弃；共丢弃 5 处无效引用并在结果中报告 |
| 引文与原文不一致 | ⚠️ 保留知识点但**不用 AI 的引文**，改用原文并标注「（待核实）」 |
| 引文与原文一致 | ✅ 正常保留 |
| 多余栏目 `overview` | 被忽略，未出现在 Markdown 中（仍只有两个栏目） |

### 真实异常处理记录（非构造）

- 智谱 1305 过载（HTTP 429）：测试 D 首次运行时触发，自动退避重试 3 次后仍失败 →
  **笔记未生成，但已写入的 `transcript.json` 保留**，原 Markdown 哈希不变；
  稍后重试成功。另在测试 C 中观察到一次 429 自动重试后成功。
- 这条记录同时覆盖了需求 9.6（限流重试）与 9.9/9.10（失败保留阶段性结果）。

---

## 三、回归测试

| 项目 | 结果 |
| --- | --- |
| V1 全部 80 项单元测试 | ✅ 通过（见第一节） |
| B站普通视频/分P/本地文件解析 | ✅ `test_platforms.py` 34 项 |
| 本地文件导入（默认复制、原文件不变） | ✅ `test_utils_download.py` |
| AI 修订（真实调用） | ✅ 对 `outputs/20261031165315.md` 润色成功（2322 字），
    生成独立 `_refined.md`，原文件哈希不变 |
| 界面启动 | ✅ `python window.py --selftest` 退出码 0，新控件（字幕设置/智能笔记）正常构建 |
| V1 历史输出文件 | ✅ `outputs/*.md` 与 `*_refined.md` 均未被移动、覆盖或删除 |

---

## 四、未验证项（如实说明）

| 项目 | 原因 | 状态 |
| --- | --- | --- |
| B站 CC 字幕的真实下载 | 本机匿名访问 `x/player/wbi/v2` 对目标视频返回空字幕列表
    （B站字幕接口通常需要登录态）；本机无可用登录 cookies | ⏸ 解析与选择逻辑已由
    单元测试与 mock 覆盖，真实取流未验证 |
| 仅字幕模式在真实平台上的成功路径 | 同上（B站无字幕；YouTube 自动字幕不满足
    "人工"条件，智能模式按设计回退） | ⏸ 失败路径已真实验证（测试 B/C 的发现阶段） |
| 长视频（>1 小时）多分块笔记 | 未构造该规模样本；分块逻辑由单元测试覆盖 | ⏸ |
| "语义核验" | 程序只能验证引用与引文一致性，语义支持无法自动判断
    （见 `docs/V2_DESIGN.md` 4.4） | ❌ 明确不声称已验证 |

---

## 五、复现方式

```bash
# 单元测试
python -m unittest discover -s tests

# 测试 A：人工字幕
YTDLP_PROXY=http://127.0.0.1:7897 python tools/v2_e2e.py --url "https://www.youtube.com/watch?v=aircAruvnKk" --mode smart --notes

# 测试 B/C：B站分P（无字幕 → Whisper）
python tools/v2_e2e.py --url "https://www.bilibili.com/video/BV1vwME6AEpd?p=33" --mode smart --notes

# 测试 D：旧 Markdown
python tools/v2_e2e.py --legacy outputs/20261031150524.md --notes

# 界面
python window.py            # 或双击 Bili2Text-Plus.cmd
python window.py --selftest # 冒烟测试
```

工具与界面都不读取、不打印密钥；AI 配置来自 `LLM_*` / `MINIMAX_*` 环境变量或 `.env`。


## 六、补丁后最终验收记录（2026-10-31）

本节为补丁应用后的最新验收记录，优先于前文的历史测试数量及未验证状态。

### 1. 自动化与 GUI

- 原开发阶段：148/148 项测试通过。
- 定向补丁后：新增 12 项测试，总计 160 项。
- 项目 Python 3.10.11、隔离虚拟环境 Scripts PATH 后：160/160 通过。
- 原 PATH 下有 1 项既有断言失败：Windows 返回 yt-dlp.EXE，大写扩展名导致大小写敏感测试失败。本轮未修改该测试。
- window.py --selftest：退出码 0。
- 原有七文件补丁通过 git apply 和 git diff --check。

### 2. YouTube 仅字幕模式真实测试

- 视频 ID：aircAruvnKk。
- 模式：subtitle_only。
- 代理：仅在测试进程设置 YTDLP_PROXY=http://127.0.0.1:7897，测试后恢复。
- 字幕语言：zh-CN。
- source_type：human_subtitle。
- 实际获得 290 个字幕片段。
- 覆盖率约 90.4%，verdict=ok。
- transcript.json、transcript.md、task_manifest.json 均存在。
- 未运行 Whisper，未调用 AI 笔记模型。
- 字幕来源标记依据 yt-dlp 普通字幕类别，不代表独立核实了人工制作属性。
- 此项已通过真实平台测试，覆盖前文第四节关于仅字幕模式成功路径尚未验证的历史记录。

### 3. 桌面 UI 本地文件拦截

- 输入临时本地 MP4 文件。
- 选择仅提取字幕模式。
- 界面正确提示不支持本地文件的字幕提取入口。
- 没有启动 Whisper。
- 没有生成额外 transcript.json。
- GUI 正常关闭，退出码 0。
- 首次 UI 自动化输入路径存在残留字符，清空重输后测试通过；该过程记录保留。

### 4. 保留的限制

- B站有字幕轨道时的登录态真实下载尚未完成。
- 长视频多分块笔记尚未完成真实端到端验收。
- 证据一致性检查不等于完整语义事实核验。
- Whisper small 的识别准确率未在本轮优化。
- 历史代理 502 故障记录保留，7897 代理下后续测试成功。

结论：V2 当前冻结功能范围的本地验收通过。GitHub 推送须另行取得用户明确授权。
