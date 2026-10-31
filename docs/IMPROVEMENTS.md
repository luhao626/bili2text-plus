# Bili2Text Plus 改造说明（docs/IMPROVEMENTS.md）

## 零、分支基线与提交关系

| 项目 | 值 |
| --- | --- |
| 上游仓库 | `https://github.com/beingaigital/bili2text` |
| 基线提交 | `92e13ca` *Improve video downloads and ignore generated media*（上游 `main`） |
| 功能分支 | `feature/douyin-support`，从 `92e13ca` 直接拉出 |
| 共同祖先 | `92e13ca`（与上游 `main` 一致，无 unrelated-histories 合并） |
| 改动规模 | 21 个文件，+2884 / −595（`git diff --shortstat upstream/main...HEAD`） |

分支提交顺序：

1. `feat: add Douyin support, fix Bilibili multi-page, generalize LLM config`
2. `test: add 56 unit tests for parsing, downloads and config`（后续修复补充到 66）
3. `docs: document new features, test evidence and configuration`
4. `chore: add launchers and standalone refinement tooling`
5. `docs: update install guide and record real end-to-end evidence`
6. `fix: reject Douyin URLs without a video id, add optional browser cookies`
7. `docs: document optional cookie configuration in .env.example`

因此 GitHub 上可以直接用 `main...feature/douyin-support` 比较并创建 PR，不会出现"整个仓库都是新增文件"的虚假 diff。

## 一、与两个项目的关系

- **主项目**：[beingaigital/bili2text](https://github.com/beingaigital/bili2text)（MIT 许可证，Tkinter/ttkbootstrap 桌面版）。本分支以它为基础，保留其全部架构、许可证与版权信息。
- **参考项目**：[jaifiresong/bili2text](https://github.com/jaifiresong/bili2text)（MIT，FastAPI + DDD 的 Web 重写版）。它的整体架构与桌面版完全不同，因此**没有直接复用其代码**，仅借鉴了两个思路：
  1. 用「OpenAI 兼容的通用 LLM 配置」（`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`）替代单一服务商硬编码；
  2. 抖音下载同样基于 yt-dlp 的公开能力，不绕过任何访问控制。
- 参考项目要求以 FastAPI Web 服务运行，这不符合任务书"不要无必要地改写成 Web 应用"的要求，故维持桌面版形态。

## 二、新增/修改的文件

| 文件 | 变更 | 说明 |
| --- | --- | --- |
| `platforms.py` | 新增 | 域名精确判定平台；从中文分享文案提取 URL；解析 BV/av、`?p=`、抖音视频 ID 与短链、YouTube ID；生成互不冲突的缓存键；Windows 安全文件名 |
| `utils.py` | 重构 | 下载入口改用 `platforms`；B站 `?p=` 保留并传入 yt-dlp；WBI 兜底按目标分P的 cid 下载；抖音请求头；yt-dlp 错误翻译为中文提示；短链自动跟随跳转 |
| `llm_config.py` | 新增 | 通用 LLM 配置层（LLM_* 优先，兼容 MINIMAX_*）；密钥脱敏；错误分类（鉴权/模型/限流/过载/欠费/网络）；`test_connection()` 连通测试 |
| `speech2text.py` | 增强 | 接入 llm_config；自动关闭思考模式；429/5xx 自动重试；动态输出上限；重复尾部清理；空结果拒绝写盘；可自定义输出目录 |
| `window.py` | 重构 | 四分区界面、平台实时识别、AI 配置与测试、结果预览/路径/打开/复制、线程安全日志、高分屏 DPI 适配、选择已有 Markdown 润色 |
| `tests/` | 新增 | 66 个单元测试（unittest 编写，兼容 pytest 收集），全部 mock，不依赖网络 |
| `tools/capture_ui.py` | 新增 | 主窗口截图工具（用于文档） |
| `docs/`、`.env.example` | 新增 | 本文档、测试报告、环境变量示例、界面截图 |
| `README.md`、`.gitignore` | 更新 | 新增功能说明；忽略规则补充 |

## 三、关键技术点

### 1. 分P丢失的根因与修复
原版 `_detect_source()` 用正则从 URL 中抽出 BV 号后**重新拼接链接**，`?p=33` 被丢弃；缓存目录只用 BV 号，第 1P 的缓存会被误用于第 33P。修复后：`?p=` 保留在传给 yt-dlp 的 URL 中；分P 使用 `BVxxx_p33` 独立缓存目录（第 1P 保持 `BVxxx` 以兼容旧缓存）；WBI 兜底下载按目标分P的 cid 取流，分P越界时报出总P数的中文错误。

### 2. 平台判定安全
用域名后缀精确匹配（`bilibili.com` / `douyin.com` / `iesdouyin.com` / `youtube.com` / `youtu.be` / `b23.tv` / `v.douyin.com`），`evilbilibili.com` 这类伪装域名不会误判（有对应单元测试）。不实现绕过登录、付费或隐私限制的功能。

### 3. 抖音的两条边界处理

- **无效短链不能当视频下载**：短链 404 会跳到抖音首页，原实现会把首页交给下载器；现在短链展开后若仍拿不到具体视频 ID，直接给出中文错误并中止。
- **缺少视频 ID 的链接直接拒绝**：首页、用户页、话题页等非短链地址一律报错，提示改用 `https://www.douyin.com/video/...`。
- **Cookie 可选注入**：yt-dlp 的抖音提取器当前要求 `Fresh cookies`（即使视频公开）。新增 `YTDLP_COOKIES_FROM_BROWSER` / `YTDLP_COOKIE_FILE`，仅把你本来就能正常观看的浏览器会话交给 yt-dlp，默认关闭，不做任何绕过。

### 4. GLM 免费模型的三个坑
1. **默认开启思考模式**：32000 输出预算被推理耗尽，正文为空 → 显式 `thinking: disabled`；
2. **句子级重复循环**：长文本输出末尾重复同一句话数百遍 → 输出上限按输入长度动态设定 + 重复尾部截断；
3. **1305 平台过载**：免费模型高峰期 429 → 自动退避重试 3 次，错误分类后给出中文提示。

### 5. 界面线程安全
所有后台任务（下载、转写、润色、API 测试）通过 `queue.Queue` 把消息交回主线程消费（`app.after` 轮询），`print` 重定向也走同一队列，避免 Tkinter 跨线程更新导致的假死与崩溃。

## 四、创新点总结

- 输入即识别：粘贴一段中文分享文案即可完成平台识别与短链解析；
- 缓存隔离：平台 + 视频 ID + 分P 三级缓存键，杜绝互相覆盖；
- 错误可读：yt-dlp / LLM 的英文错误统一翻译归类为中文可操作提示；
- 可演进：LLM 配置层与平台解析相互独立，后续接入新平台或新服务商只需扩展 `platforms.py` 或 `llm_config.py`。
