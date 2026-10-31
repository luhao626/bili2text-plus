# Bili2Text Plus 测试报告（docs/TEST_REPORT.md）

测试时间：2026-10-31
测试环境：Windows 11 · Python 3.10（项目 venv）· yt-dlp 2026.10.21 · FFmpeg 8.1.2 · openai-whisper 20240930 · openai 2.28.0
被测分支：`feature/douyin-support`（基线：上游 `92e13ca`）

> 说明：本报告只记录**实际执行并观察到结果**的测试。未执行的项一律列在第三节「未验证项」，不写入通过项。

## 一、自动化单元测试

命令：`python -m unittest discover -s tests`（pytest 兼容收集，未安装 pytest 也能运行）

| 测试文件 | 用例数 | 覆盖内容 |
| --- | --- | --- |
| `tests/test_platforms.py` | 34 | B站 URL/BV/av/分P/短链、抖音分享文案与视频 ID、**抖音首页/话题页应报错**、YouTube、伪装域名、缓存键隔离、安全文件名、**本地文件路径（含引号/音频/同名不同路径/缺失文件/目录拒绝）** |
| `tests/test_llm_config.py` | 18 | 配置优先级（UI > LLM_* > MINIMAX_*）、服务商预设、密钥脱敏、错误分类、思考模式开关 |
| `tests/test_utils_download.py` | 18 | yt-dlp 错误翻译、缓存隔离、WBI 分P越界报错（mock API）、**Cookie 参数默认关闭**、**短链未解析时不下载首页**、非法输入不发起网络请求、**本地文件接入/原文件不变/二次复用** |
| `tests/test_speech2text_units.py` | 8 | 分块与 token 估算、重复尾部清理、无 Key 报错、空内容防护、输出目录切换 |

**结果：78 / 78 通过（`OK`）。** 全部使用 mock，不访问真实平台。

执行原始输出：

```
Ran 78 tests in 0.942s

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

### 2.3 抖音错误路径实测（真实网络，未使用任何真实视频）

用一个**不存在的**抖音视频 ID 与一个**不存在的**短链，验证失败提示与"不绕过限制"的行为：

| 输入 | 实际输出 | 结论 |
| --- | --- | --- |
| `https://www.douyin.com/video/0000000000000000000` | `ERROR: [Douyin] ...: Fresh cookies (not necessarily logged in) are needed` → 「平台拒绝了访问（可能需要登录或存在访问限制），本工具不会绕过权限。」 | ✅ 给出中文可操作提示，返回 `None`，未重试绕过 |
| `https://www.douyin.com/`（首页） | 「该抖音链接中没有单条视频 ID…」 | ✅ 修复前会把首页当视频下载；现已拦截（本轮修复） |
| `https://v.douyin.com/thisShortLinkDoesNotExist/`（短链 404） | 短链跟随跳转 → 落到首页 → 「短链跳转后无法识别…」 | ✅ 修复前会尝试下载 `https://www.douyin.com`；现已拦截（本轮修复） |

**本轮由测试发现并修复的两个缺陷：**

1. 短链 404 时会跳转到抖音首页，原逻辑把首页当作视频继续下载 → 现在展开后若仍无具体视频 ID，直接报错返回；
2. 任意缺少视频 ID 的抖音链接（首页、用户页、话题页）会被解析成 `short_` 占位 ID → 现在非短链域名一律报错，并提示改用详情页链接。

**同时确认的平台约束（非本项目缺陷）：** yt-dlp 的抖音提取器目前要求 `Fresh cookies`，即使是公开视频。因此新增**可选**配置 `YTDLP_COOKIES_FROM_BROWSER=edge|chrome|firefox` 或 `YTDLP_COOKIE_FILE`，把用户自己浏览器里本来就能正常观看的会话交给 yt-dlp；**默认关闭**，不配置时行为与之前完全一致，也不涉及任何绕过登录/付费/隐私限制的手段。

### 2.4 抖音真实视频下载尝试（2026-10-31，使用真实公开链接 + 真实 cookies）

被测链接：`https://www.douyin.com/video/7689305777282415962`（用户提供，公开可播放）

| 步骤 | 实际操作 | 结果 |
| --- | --- | --- |
| 链接解析 | `resolve_video_input()` | ✅ `platform=douyin` `video_id=7689305777282415962` `cache_key=dy_7689305777282415962` |
| 无 cookies 直连 | `yt-dlp` | ❌ `HTTP Error 403` → `Fresh cookies (not necessarily logged in) are needed` |
| 读取 Edge cookies | `YTDLP_COOKIES_FROM_BROWSER=edge` | ❌ Edge 运行中：`Could not copy Chrome cookie database`（数据库被锁） |
| 关闭 Edge 后重试 | 同上 | ❌ `Failed to decrypt with DPAPI`（App-Bound Encryption，yt-dlp issue #10927） |
| 读取 Chrome cookies | `YTDLP_COOKIES_FROM_BROWSER=chrome` | ❌ 同样 DPAPI 失败（新版 Chromium 系浏览器已启用应用绑定加密，外部程序无法解密） |
| 用户导出 cookies.txt | Cookie-Editor 导出 Netscape 格式，36 条抖音 cookies（含 `ttwid`、`s_v_web_id`、`passport_csrf_token`），`YTDLP_COOKIE_FILE` 注入 | ❌ 仍为 `HTTP Error 403`（详情接口拒绝） |
| yt-dlp nightly | `2026.10.21.202512`（临时目录安装，未改动项目环境） | ❌ 同上，非版本问题 |
| 交叉验证另两个公开视频 | `7525709501892644132`、`7531942590553673012` | ❌ 两个都 `HTTP Error 403` |
| 页面可访问性 | `curl` 带 cookies + 浏览器 UA | ⚠️ `HTTP 200`、72,914 字节，但页面不含 `_ROUTER_DATA`/`aweme` 数据（纯 JS 壳），视频数据只能由浏览器内签名请求获取 |

**结论：抖音自 2026 年起对非浏览器访问收紧风控，命令行直连（含有效性 cookies）会被详情接口拒绝。** 这与链接是否有效、cookies 是否配置无关，属于平台侧限制；本项目**不构造签名参数绕过**风控。据此新增「本地文件兜底」：用户在浏览器里正常下载视频后，用本地文件路径即可完成转写（见 2.5）。

### 2.5 本地文件兜底：完整流程实测

测试素材：从已下载的 B站 p=33 视频截取 60 秒（`local_e2e_60s.mp4`，3,296,948 字节，真实音轨），模拟"浏览器下载抖音视频后交给本工具"的场景。

| 步骤 | 观察到的结果 |
| --- | --- |
| 路径解析 | `platform=local` `cache_key=local_local_e2e_60s_3c446df1`，`display=本地文件 · local_e2e_60s.mp4` |
| 缓存接入 | 在 `bilibili_video/local_local_e2e_60s_3c446df1/` 下以**硬链接**接入（`ls -l` 链接数为 2，无额外磁盘占用） |
| 原文件保护 | 源文件 SHA256 前 16 位 `70f7c645ba609fb4`、大小 3,296,948 字节，转写前后一致，未被修改 |
| 音频切分 | 45s 一片，共 2 个切片 |
| Whisper small | 全流程 **57.0s**（含模型加载），生成 `outputs/20261031155014.md`（348 字符） |
| 输出正确性 | 内容与源视频前 60 秒语音一致（"来这回进入到我们的底吧模式…"），与 2.1 的完整版开头吻合 |
| 界面兼容 | `python window.py --selftest` 退出码 **0**，新增「选择文件…」按钮正常构建 |

## 三、未验证项（如实说明）

| 项目 | 原因 | 状态 |
| --- | --- | --- |
| 抖音**在线链接**成功下载 | 平台侧拦截：真实链接 + 有效 cookies + nightly 版本 + 另两个公开视频均返回 403（详见 2.4） | ❌ 无法验证（平台限制，已提供本地文件兜底） |
| 抖音**本地文件**转写 | 用真实 60 秒素材完成下载替代路径全流程（详见 2.5） | ✅ 已验证 |
| YouTube 完整下载 + 转写 | 仅验证了元数据可获取；未实际下载并转写 | ⏸ 部分验证 |
| MiniMax 真实调用 | 为避免产生费用，未调用付费模型 | ⏸ 未验证（仅保留兼容代码与预设，错误分类有单元测试） |
| Windows EXE 打包 | PyInstaller 未安装，且需用户同意后安装 | ⏸ 未执行 |
| 高并发/长文档（>1 小时）转写 | 未构造该规模样本 | ⏸ 未验证 |

## 四、已知限制

- 抖音**在线链接**：平台自 2026 年起拒绝非浏览器访问，实测有效 cookies 也无法通过（2.4）。程序会给出中文提示并**引导改用本地文件**，「选择文件…」入口可完成同样的转写流程；
- 浏览器 cookies 读取：新版 Chrome / Edge 启用了 App-Bound Encryption，`--cookies-from-browser` 在 Windows 上普遍失败（yt-dlp issue #10927），需要用户用扩展导出 `cookies.txt`；即便如此，抖音仍可能返回 403；
- 短时间内连续调用免费模型（`glm-4.7-flash`）可能连续 429，程序最多自动重试 3 次（10s / 20s 退避），仍失败需稍后重试；
- 抖音短链解析依赖网络可达 `v.douyin.com`；短链 404 不再被误当作视频下载，会直接报错；
- 本地文件兜底依赖硬链接：跨磁盘时会退回复制（大文件会多占一份空间），原文件始终不会被修改；
- 本机访问 GitHub / YouTube 需经系统代理 `127.0.0.1:7897`，直连被阻断（属网络环境限制，与程序无关）。

## 五、复现方式

```bash
# 单元测试
python -m unittest discover -s tests

# 完整流程（需网络；会写入 outputs/ 与缓存目录）
python window.py
```
