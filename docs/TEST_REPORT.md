# Bili2Text Plus 测试报告（docs/TEST_REPORT.md）

测试时间：2026-10-31
测试环境：Windows 11 · Python 3.10（项目 venv）· yt-dlp 2026.10.21 · FFmpeg 8.1.2 · openai-whisper 20240930 · openai 2.28.0
被测分支：`feature/douyin-support`

## 一、自动化单元测试

命令：`python -m unittest discover -s tests`（pytest 兼容收集，未安装 pytest 也能运行）

| 测试文件 | 用例数 | 覆盖内容 |
| --- | --- | --- |
| `tests/test_platforms.py` | 19 | B站 URL/BV/av/分P/短链、抖音分享文案与视频 ID、YouTube、伪装域名、缓存键隔离、安全文件名 |
| `tests/test_llm_config.py` | 17 | 配置优先级（UI > LLM_* > MINIMAX_*）、服务商预设、密钥脱敏、错误分类、思考模式开关 |
| `tests/test_utils_download.py` | 9 | yt-dlp 错误翻译、缓存隔离、WBI 分P越界报错（mock API）、非法输入不发起网络请求 |
| `tests/test_speech2text_units.py` | 11 | 分块与 token 估算、重复尾部清理、无 Key 报错、空内容防护、输出目录切换 |

**结果：56 / 56 通过（`OK`）。** 全部使用 mock，不访问真实平台。

## 二、真实网络验证（有真实调用证据）

| # | 项目 | 方法 | 结果 |
| --- | --- | --- | --- |
| 1 | B站分P选择（`BV1vwME6AEpd?p=33`） | yt-dlp 仅取元数据（不下载视频），对比 p=1 与 p=33 的标题 | ✅ p=1 返回「p01 1-AI Agent介绍」，p=33 返回「p33 8-加载所有实体数据」，`?p=` 参数被正确保留 |
| 2 | 智谱 GLM 连通性 | `llm_config.test_connection()` 真实请求 | ✅ 5.2s 返回，模型回显 `glm-4.7-flash` |
| 3 | AI 润色端到端（Plus 代码） | 临时目录写入小文档 → `speech2text.refine_text()` → 校验输出 | ✅ 3.8s 生成 `e2e_refined.md`（199 字节），术语 AI Agent / MCP / Markdown 保留，原始文档未改动 |
| 4 | 润色健壮性 | 12.9KB 真实转写，人为制造 429 | ✅ 自动重试后成功；未关思考模式时曾复现"空输出/重复循环"两类缺陷，修复后输出长度恢复正常（4721 字，39.9s） |
| 5 | 界面启动 | `--selftest` 烟雾测试 + 实际启动截图 | ✅ 窗口正常，四分区完整，见 `docs/screenshots/ui-main.png` |

## 三、未验证项（如实说明）

| 项目 | 原因 | 状态 |
| --- | --- | --- |
| 抖音视频完整下载+转写 | 未获得可用的公开视频短链；yt-dlp 的抖音提取器对部分视频要求登录 Cookie | ⏸ 待提供链接后补充端到端验证（解析层已有单元测试覆盖） |
| YouTube 下载回归 | 本机网络无法直连 YouTube，且系统代理不可用 | ⏸ 未验证（代码路径与原版一致，仅格式参数沿用） |
| Whisper small 完整转写 | 转写代码本轮未改动；同环境原版在当日 13:51 转写成功（outputs/20261031133846.md） | ⚠️ Plus 代码未重跑完整转写，借用同引擎的近期成功记录佐证 |
| Windows EXE 打包 | PyInstaller 未安装（需另行确认安装）；优先保证 Python 版可用 | ⏸ 未执行 |
| MiniMax 真实调用 | 按任务要求未调用付费模型，仅保留兼容代码与预设 | ⏸ 未验证（错误分类有单元测试） |

## 四、已知限制

- 抖音需要登录 Cookie 的视频无法下载，程序会给出中文提示且不会尝试绕过；
- 免费模型在高峰期可能多次 429，程序最多重试 3 次，仍失败时需稍后再试；
- 抖音短链解析依赖网络可达 `v.douyin.com`，解析失败时会把短链直接交给 yt-dlp 处理。
