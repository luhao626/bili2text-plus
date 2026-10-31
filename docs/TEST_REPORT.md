# Bili2Text Plus 测试报告（docs/TEST_REPORT.md）

测试时间：2026-10-31
测试环境：Windows 11 · Python 3.10（项目 venv）· yt-dlp 2026.10.21 · FFmpeg 8.1.2 · openai-whisper 20240930 · openai 2.28.0
被测分支：`feature/douyin-support`（基线：上游 `92e13ca`）

> 说明：本报告只记录**实际执行并观察到结果**的测试。未执行的项一律列在第三节「未验证项」，不写入通过项。

## 一、自动化单元测试

命令：`python -m unittest discover -s tests`（pytest 兼容收集，未安装 pytest 也能运行）

| 测试文件 | 用例数 | 覆盖内容 |
| --- | --- | --- |
| `tests/test_platforms.py` | 20 | B站 URL/BV/av/分P/短链、抖音分享文案与视频 ID、YouTube、伪装域名、缓存键隔离、安全文件名 |
| `tests/test_llm_config.py` | 18 | 配置优先级（UI > LLM_* > MINIMAX_*）、服务商预设、密钥脱敏、错误分类、思考模式开关 |
| `tests/test_utils_download.py` | 10 | yt-dlp 错误翻译、缓存隔离、WBI 分P越界报错（mock API）、非法输入不发起网络请求 |
| `tests/test_speech2text_units.py` | 8 | 分块与 token 估算、重复尾部清理、无 Key 报错、空内容防护、输出目录切换 |

**结果：56 / 56 通过（`OK`，耗时 0.42s）。** 全部使用 mock，不访问真实平台。

执行原始输出：

```
Ran 56 tests in 0.419s

OK
```

## 二、真实网络与端到端验证（有执行记录）

### 2.1 B站分P：完整流程（下载 → 音频 → Whisper small → GLM 润色）

被测链接：`https://www.bilibili.com/video/BV1vwME6AEpd?p=33`（该合集共 50 P，p=33 时长 6m53s）

| 步骤 | 观察到的结果 |
| --- | --- |
| 链接解析 | `platform=bilibili` `page=33` `cache_key=BV1vwME6AEpd_p33` |
| 下载 | yt-dlp 取到 `cid=39776879310`，下载 20.85 MiB，耗时 8.8s；缓存目录 `bilibili_video/BV1vwME6AEpd_p33/`，**只含 1 个文件**，未下载整个合集 |
| 分P正确性 | 文件名含 `p33 8-加载所有实体数据`，与 p=33 标题一致（非第一P「1-AI Agent介绍」） |
| 缓存复用 | 第二次运行时输出 `复用已缓存视频`，未重复下载 |
| 音频切分 | 45s 一片，共 **10 个切片**（`audio/slice/20261031150524/`） |
| Whisper small 转写 | **284.0s** 完成，生成 `outputs/20261031150524.md`（2327 字符，10 个切片全部有输出） |
| GLM 润色 | 模型 `glm-4.7-flash`，**26.5s** 生成 `outputs/20261031150524_refined.md`（2338 字符） |
| 原文完整性 | 润色后重新读取原文，内容与润色前**完全一致**（`原文未变: True`） |

转写片段（原始 vs 润色，可见错别字被修正而术语保留）：

```
原始：来 这回进入到我们的底吧模式。咱们一行行来看哦。其实这里边咱们就是几买方法,就是一个模式匹配。
润色：来，这回进入到我们的图谱模式。咱们一行行来看哦。其实这里边咱们就是基于方法，就是一个模式匹配。
```

### 2.2 其他真实调用

| # | 项目 | 方法 | 结果 |
| --- | --- | --- | --- |
| 1 | B站分P元数据对比 | yt-dlp 仅取元数据，对比 p=1 与 p=33 | ✅ p=1 返回「p01 1-AI Agent介绍」，p=33 返回「p33 8-加载所有实体数据」 |
| 2 | 智谱 GLM 连通性 | `llm_config.test_connection()` 真实请求 | ✅ 5.2s 返回，响应模型回显 `glm-4.7-flash`（确认非 MiniMax） |
| 3 | AI 润色端到端（小文档） | 临时目录小文档 → `refine_text()` | ✅ 3.8s 生成，术语 AI Agent / MCP / Markdown 保留 |
| 4 | 润色健壮性 | 12.9KB 真实转写 + 真实 429 | ✅ 自动重试后成功；修复前曾复现"空输出"与"句子级重复循环"两类缺陷，修复后输出正常（4721 字，39.9s） |
| 5 | YouTube 元数据 | yt-dlp `extract_info` 经系统代理 `127.0.0.1:7897` | ✅ 取到标题「Big Buck Bunny 60fps 4K…」与时长 635s；直连同一请求失败（本机网络限制） |
| 6 | 界面启动 | `--selftest` 烟雾测试 + 实际启动截图 | ✅ 四分区完整，见 `docs/screenshots/ui-main.png`（2266×1751） |
| 7 | 缓存隔离 | 同一 BV 的不同分P | ✅ 分P使用 `BVxxx_p33` 独立目录；p=1 沿用 `BVxxx` 以兼容旧缓存 |

## 三、未验证项（如实说明）

| 项目 | 原因 | 状态 |
| --- | --- | --- |
| 抖音完整下载 + 转写 | 尚未获得可指定的公开视频链接（需由项目所有者提供）；yt-dlp 已内置 `Douyin` 提取器，但未实际调用 | ⏸ 仅解析层有单元测试覆盖，端到端未验证 |
| YouTube 完整下载 + 转写 | 仅验证了元数据可获取；未实际下载并转写 | ⏸ 部分验证 |
| MiniMax 真实调用 | 为避免产生费用，未调用付费模型 | ⏸ 未验证（仅保留兼容代码与预设，错误分类有单元测试） |
| Windows EXE 打包 | PyInstaller 未安装，且需用户同意后安装 | ⏸ 未执行 |
| 高并发/长文档（>1 小时）转写 | 未构造该规模样本 | ⏸ 未验证 |

## 四、已知限制

- 抖音若遇到需要登录 Cookie 的视频，yt-dlp 会失败，程序给出中文提示且**不会尝试绕过**访问控制；
- 免费模型（`glm-4.7-flash`）高峰期可能连续 429，程序最多自动重试 3 次（10s / 20s 退避），仍失败需稍后重试；
- 抖音短链解析依赖网络可达 `v.douyin.com`，解析失败时会把原短链直接交给 yt-dlp；
- 本机访问 GitHub / YouTube 需经系统代理 `127.0.0.1:7897`，直连被阻断（与程序无关，属网络环境限制）。

## 五、复现方式

```bash
# 单元测试
python -m unittest discover -s tests

# 完整流程（需网络；会写入 outputs/ 与缓存目录）
python window.py
```
