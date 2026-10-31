# Bili2Text Plus V2 设计与实现说明

本文说明 V2 两个功能（人工字幕优先 / 可溯源智能笔记）的设计取舍、数据契约与错误处理，
供验收与后续维护使用。

- 基线：V1 分支 `feature/douyin-support`（commit `e647764`）
- 开发分支：`feature/subtitles-and-study-notes`
- 范围：只做「可靠字幕变文字」与「文字变可溯源笔记」，其余保持 V1 行为

---

## 一、总体思路

V2 把"文字从哪来"和"文字怎么变成笔记"拆成两条独立的链：

```
输入 → 平台识别(platforms) ─┬─→ 字幕发现(subtitles) ─→ 解析/校验 ─→ TranscriptDocument ─┐
                            └─→ 下载 → 切音 → Whisper ─→ 近似时间轴 ─→ TranscriptDocument ─┤
                                                                                        ├→ outputs/<task_id>/{transcript.md, transcript.json, task_manifest.json}
                                                                                        └→ 智能笔记(study_notes) → study_notes.md
```

关键点：

1. **字幕优先不代表字幕一定可用**：只有"来源可确认的人工字幕 + 完整性校验通过"才会跳过下载与转写；
2. **JSON 是事实来源**：所有证据文字、时间位置都从 `transcript.json` 取，AI 只提供结构和引用 ID；
3. **不猜测**：判断不了来源就标 `unknown_subtitle`，判断不了时长就标 `approximate`/`unknown`，
   绝不用默认值把不确定变成看似确定。

---

## 二、统一数据契约 TranscriptDocument

实现文件：`transcript.py`

```json
{
  "schema_version": "2.0",
  "video": {
    "platform": "bilibili",
    "video_id": "BV1vwME6AEpd",
    "page": 33,
    "title": "8-加载所有实体数据",
    "url": "https://www.bilibili.com/video/BV1vwME6AEpd?p=33",
    "duration_ms": 413000
  },
  "transcript": {
    "source_type": "human_subtitle",
    "language": "zh-Hans",
    "segments": [
      {"id": "seg_0001", "start_ms": 15000, "end_ms": 19500, "time_precision": "exact", "text": "示例字幕内容"}
    ]
  },
  "provenance": {
    "track_language": "zh-Hans",
    "track_evidence": "B站 CC 字幕（lan=zh-Hans，非 ai- 前缀）",
    "subtitle_format": "bilibili_json",
    "cid": 39776879310,
    "coverage": {"ratio": 0.92, "verdict": "ok", "reasons": []}
  }
}
```

字段语义：

| 字段 | 说明 |
| --- | --- |
| `schema_version` | 固定 `"2.0"`；不是 2.0 直接拒绝读取，避免旧结构被误当新结构 |
| `video.page` | B站分P号；非分P视频为 `null` |
| `video.duration_ms` | 平台返回的时长；拿不到为 `null`，不估算 |
| `transcript.source_type` | `human_subtitle` / `automatic_subtitle` / `unknown_subtitle` / `whisper` / `legacy_markdown` |
| `segments[].id` | `seg_0001` 递增，创建后不重排；笔记引用以此为准 |
| `segments[].start_ms/end_ms` | 毫秒整数；无时间信息时为 `null` |
| `segments[].time_precision` | `exact` / `approximate` / `unknown` |
| `provenance` | 扩展字段（非契约必需）：字幕轨道证据、CID、覆盖率等 |

`time_precision` 的严格定义：

- `exact`：**直接使用字幕源给出的时间码**。它只表示"数字来自字幕文件"，不代表该字幕与语音毫秒级同步
  （字幕本身可能整体偏移）；
- `approximate`：Whisper 45 秒切片推算出的位置，只有近似定位能力；渲染时统一加 `≈` 前缀，不冒充逐句精确；
- `unknown`：没有时间信息（如旧 Markdown），时间为 `null`，笔记改用「原文第 N 段」定位。

校验规则（`TranscriptDocument.validate()`）：ID 非空且唯一、`time_precision` 合法、
`unknown` 不得带时间、`exact/approximate` 必须带合法时间、文本不得为空。任何一项不通过
都不会写出正式文件。

---

## 三、功能 A：字幕发现 / 解析 / 选择

实现文件：`subtitles.py`

### 3.1 发现

| 平台 | 方式 | 分P处理 |
| --- | --- | --- |
| B站 | `x/player/pagelist` 取目标 P 的 **cid** → `x/player/wbi/v2`（WBI 签名）；签名查询失败则回退 Whisper，不采用旧版无签名接口的字幕正文 | 一律以目标 P 的 cid 查询，**不会**用第 1P 的 cid |
| YouTube / 其他 | `yt-dlp` info dict：`subtitles`（人工）与 `automatic_captions`（自动） | 无分P概念 |

B站字幕来源判定：

- `lan` 以 `ai-` 开头，或轨道 `type=1` → `automatic_subtitle`（B站 AI 字幕）；
- `lan` 不是 `ai-` 且轨道 `type=0` → `human_subtitle`（按平台 CC 类型分类，非独立人工审稿证明）；
- 仅有普通语言码而缺少可靠 `type` 或类型未知 → `unknown_subtitle`，智能模式回退 Whisper；`ai-` 与 `type=0` 矛盾时按自动字幕保守处理；
- `ai_type` 在不同接口样本的含义并不一致，不能仅凭其为 0 判断成人工字幕。

YouTube：`subtitles` → 人工，`automatic_captions` → 自动。yt-dlp 本身不区分"社区贡献"与"作者上传"，
因此这里的"人工"仅表示"非平台 ASR 生成"，这一点在文档中如实说明。

### 3.2 选择矩阵

| 模式 | 有可靠人工字幕 | 只有自动字幕 | 无字幕 |
| --- | --- | --- | --- |
| 智能模式（默认） | ✅ 用字幕，不下载不转写 | ⚠️ 不用，回退 Whisper 并说明原因 | ⚠️ 回退 Whisper |
| 强制 Whisper | 忽略字幕 | 忽略字幕 | 转写 |
| 仅提取字幕 | ✅ 用字幕 | ✅ 用自动字幕但**明确标注**为自动 | ❌ 报错，不转写 |

本地音视频在本轮没有字幕提取入口：选择「仅提取字幕」必须明确失败并且不运行 Whisper（GUI 与 E2E 工具一致）。

设计取舍：智能模式**不**使用自动字幕，因为需求明确"有可靠人工字幕优先，没有则调用 Whisper"，
且要求"默认模式不应静默将自动字幕冒充人工字幕"。自动字幕只在"仅提取字幕"下使用，并如实标注。

### 3.3 解析

支持 SRT、WebVTT、B站字幕 JSON，统一转为 `(start_ms, end_ms, text)`：

- 时间码支持 `hh:mm:ss,mmm`、`hh:mm:ss.mmm`、`mm:ss.mmm`；分钟/秒 > 59 视为非法；
- 清洗 `<c>`、`<00:00:00.000>`、`{\an8}` 等标记与 HTML 实体，**不删除正文**；
- 空块、`NOTE`/`STYLE` 元数据块跳过；非法时间码计入 `invalid_count`，不静默当成成功；
- 完全无法识别格式时报错，不猜格式。

### 3.4 完整性校验

只看文本长度会误判（2 小时视频里 3 分钟字幕也可能很长），因此 `assess_coverage()` 综合：

- 有效覆盖时长 / 视频时长（默认需 ≥ 55%）；
- 空文本比例（默认 ≤ 35%）；
- 非法时间码数量（必须为 0）；
- 相邻片段重叠/异常间断计数（仅记录，用于诊断）。

任一项不满足 → `verdict=incomplete`：智能模式回退 Whisper，仅字幕模式直接报错，
不产出"看似成功"的字幕文件。

---

## 四、功能 B：可溯源智能笔记

实现文件：`study_notes.py`

### 4.1 两阶段生成

1. **AI 只产出结构**：给模型带 ID 的原文，要求返回
   `{"chapters":[{"title","segment_ids"}],"key_points":[{"title","explanation","segment_ids","quote"}]}`；
2. **程序回填证据**：按 `segment_ids` 从 `TranscriptDocument` 取原文与时间范围，
   **不采用 AI 写出的引文**。

因此：

- 引用不存在的 ID → 直接丢弃并计数（`dropped_refs`），不编造证据；
- AI 引文需规范化后在原文中连续出现，取消仅靠字符集合重叠的模糊放行；不匹配或缺少引文 → 标**（待核实）**；
- 知识点中的数字、英文术语若不在所引用原文出现 → 标**（待核实）**，显示原因；这仅能发现部分可疑结论；
- Markdown 在两大栏目之前明确提示：证据可溯源不代表知识点解释经过独立语义核验；
- 章节/知识点没有任何有效引用 → 丢弃，不留空壳。

### 4.2 输出结构

正文只允许两个栏目（渲染层写死，AI 返回的 `overview`/`glossary` 等一律忽略）：

```markdown
# 学习笔记：<标题>

## 02 · 章节结构

- [00:15] 实体数据加载原理
- [02:30] 节点与关系创建

## 03 · 核心知识点

### 知识点：实体与关系需要分别建模

实体表示图中的对象，关系描述对象之间的连接。

**原文证据**

[02:30–02:48]
「对应的原始字幕或转写内容」
```

- 有 `exact` 时间 → `[mm:ss]` / `[mm:ss–mm:ss]`；`approximate` → 前缀 `≈`；
- 无时间（旧 Markdown）→ `原文第 N 段`，不显示任何虚构时间；
- 引文过长时截断到 220 字并加 `…`，保持原文顺序，不改写。

### 4.3 长文档

- 按原文顺序分块（默认 6000 tokens/块），块内保留原始 ID；
- 每块独立提取，再按标题规范化去重合并、合并 ID 列表；
- 合并后再次校验：所有引用必须存在于最终文档；
- 任一分块失败 → `complete=False`，Markdown 顶部标注"只覆盖了部分原文"，
  manifest 记录失败原因，**不会**把部分结果报告为完整成功。

### 4.4 诚实性边界

程序能验证：引用 ID 是否存在、引文是否与原文一致、时间是否来自源数据。
程序**不能**验证：该证据在语义上是否真的支持该结论。

因此 `StudyNotes.semantic_verification` 固定为 `not_performed`，文档与验收报告都明确说明
"引用可核对 ≠ 语义已核验"，不声称所有知识点都通过了事实核验。

---

## 五、旧 Markdown 兼容

实现文件：`legacy_markdown.py`

- 读取 V1 的 `outputs/*.md`，跳过标题行与元信息行；
- 按空行切段；单段超过 400 字时按句切分为稳定单元（V1 转写常常整篇没有空行，
  否则会变成一个无法定位的巨型片段）；
- 每段生成 `seg_0001…`，`time_precision=unknown`、时间为 `null`；
- **只读**：读取前后各算一次 SHA256，若不一致立即中止；从不写回原文件；
- 旧文档任务只写 `transcript.json`（不复制一份 `transcript.md`，避免制造重复文件）；
- 原有「AI 修订」不受影响：`refine_text()` 新增可选 `source_path`，默认行为与 V1 完全一致。

---

## 六、输出布局与安全写入

```
outputs/
  <task_id>/                 # 时间戳 + 缓存键，重跑不会覆盖上一次结果
    transcript.md
    transcript.json
    study_notes.md
    task_manifest.json       # 状态/模型/来源/文件清单，不含任何密钥
  20261031155014.md          # V1 历史文件原样保留，不被移动或覆盖
```

`task_id` 形如 `20261031165129_yt_aircAruvnKk`，旧文档任务加 `legacy_` 前缀。

写入策略：`atomic_write_text()` 先写同目录 `.tmp-*.part`，`fsync` 后 `os.replace` 原子替换。
中断只会留下临时文件，不会出现半截的正式文件。

---

## 七、界面改动（最小化）

保留 Tkinter / ttkbootstrap 与原有分区，只新增：

- **字幕设置**：文字获取方式下拉（智能 / 强制 Whisper / 仅提取字幕）、当前来源标识、回退原因；
- **智能笔记**：`生成智能笔记`、`选择 Markdown 生成笔记…`、`打开笔记文件`；
- 笔记沿用原「文本预览」区展示，`打开文件夹` 复用原按钮；
- 所有耗时操作仍在后台线程执行，UI 更新经队列回到主线程。

未新增：视频播放器、时间戳跳转、字幕编辑器、批量处理。

---

## 八、异常处理对照

| 场景 | 行为 |
| --- | --- |
| 视频没有字幕 | 智能模式回退 Whisper，日志与界面显示原因 |
| 字幕为空/损坏 | 解析失败或 `cues` 为空 → 回退（智能）或报错（仅字幕） |
| 仅字幕模式无字幕 | 明确失败，**不**运行 Whisper，不下载视频 |
| 字幕属于错误分P | 以目标 P 的 cid 查询；cid 不匹配即取不到字幕 → 回退/报错 |
| 字幕来源无法确认 | `unknown_subtitle`，不冒充人工 |
| API 401/429/5xx/超时 | 复用 `llm_config.classify_error`；429/5xx 有限退避重试（最多 3 次） |
| AI 返回无效 JSON | 一次纠错重试，仍失败则丢弃该块并标记不完整 |
| AI 引用不存在的 ID | 丢弃该条目并计数，不编造证据 |
| 笔记失败 | 已写入的 `transcript.md/json` 保留 |
| 中途关闭/中断 | 原子写入，保留已完成的文件 |
| 字幕文字与时间码不一致 | 非法时间码计入 `invalid_count`，覆盖率判定不放行 |
| 旧 Markdown 无时间 | 可生成笔记，使用段落定位，不产生假时间戳 |

---

## 九、明确未做的内容

批量转写、新语音引擎、新下载平台、播放器、时间戳跳转、术语词典、课程概述、复习题、
思维导图、知识图谱、PPT/PDF 导出、多用户服务端、UI 全面重写、自动推送 GitHub —— 均未实现。

---

## 十、已知限制

- **B站字幕需要登录态**：匿名请求 `x/player/wbi/v2` 对多数视频返回空字幕列表。
  本机实测 BV1vwME6AEpd 的全部 50 个分P 均无字幕，因此测试 C 走的是 Whisper 回退路径；
  若需字幕，可通过 `YTDLP_COOKIE_FILE` 提供浏览器导出的 cookies（仅用于你本就能观看的公开视频）；
- **YouTube 需要代理**：本机直连不可达，字幕发现与下载需 `YTDLP_PROXY`（仅影响 yt-dlp 侧）；
- **人工字幕的判定粒度**：依赖平台分类（B站 `lan` 前缀、yt-dlp `subtitles` 桶），
  无法追溯更细的来源（例如"社区贡献"与"作者上传"的差别）；
- **语义核验未实现**：见 4.4，程序只做引用级校验；
- **45 秒切片**：Whisper 时间只能到切片级近似，未改为逐句对齐（属于 V3 范围）。
