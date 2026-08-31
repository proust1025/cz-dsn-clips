---
name: douyin-video-harvest
description: 抖音直播切片全自动流水线：Playwright+yt-dlp 按关键词抓取下载（按点赞过滤、SQLite 去重、无水印）→ 自动归档到飞书多维表格（标题/发布日期/点赞/评论/转发/链接/附件/抓取时间/脚本）→ faster-whisper 本地语音识别提取「脚本」并回填飞书。当用户要「每天自动抓取抖音某主播/某话题视频」「下载抖音高赞切片归档」「建抖音素材库 + 飞书表格 + 转写脚本」时使用。
agent_created: true
---

# 抖音直播切片全自动流水线

一条可定时运行的流水线：**抓取下载 → 飞书多维表格归档 → 语音识别提取脚本回填**。
分三层，每一层都可独立复用：

1. **抓取层** Playwright 驱动系统 Chrome + yt-dlp 下载
2. **归档层** 飞书多维表格（Base）存元数据 + 视频附件
3. **脚本层** faster-whisper 本地 ASR 转写语音，回填「脚本」列

---

## 一、为什么不用浏览器下载插件（vtool 之类）

vtool 的「批量下载」是 Chrome **扩展弹窗里的 GUI**，属于浏览器 UI 而非网页 DOM，
Playwright 点不到；且闭源，抖音改版即崩。插件只适合手动补片，**不适合自动化**。

---

## 二、技术选型

| 环节 | 方案 | 理由 |
| --- | --- | --- |
| 浏览器 | `launch_persistent_context` + `channel="chrome"` | 用系统 Chrome，指纹比 Playwright 自带 Chromium 真实，反爬更好过 |
| 登录态 | 持久化 profile 目录 | 扫码一次，长期复用 |
| 取数据 | 拦截搜索接口 JSON 响应 | 比解析 DOM 稳定得多 |
| 下载 | yt-dlp（自带 Douyin 解析器） | 无水印、最高画质、ffmpeg 合流 |
| 去重 | SQLite 存 `aweme_id` | 跨次运行不重复下 |
| 防重入 | lock 文件 | 避免上次没跑完下次又启动 |
| 归档 | 飞书 Base（`lark-cli`） | 用户要表格化浏览 + 附件 |
| 转写 | faster-whisper（small, cpu, int8） | 本地离线、零成本、中文可用 |

---

## 三、必踩的坑（每个都会让程序静默失效）

### 坑 1：搜索接口改过版
- 旧：`/aweme/v1/web/general/search/single/`
- **新：`/aweme/v1/web/search/item/`**

返回体顶层 `data` 是条目数组；旧字段名 `aweme_list` 在新接口里是 `None`。
写成 `data.get("data") or data.get("aweme_list")` 两者都兼容。

条目结构：
```
data[i].aweme_info.{
  aweme_id, desc, create_time,
  author.nickname,
  statistics.digg_count,      # 点赞数
  statistics.comment_count,   # 评论数
  statistics.share_count,     # 转发数
  video.play_addr             # 有这个字段才是真视频
}
```
- 排除图文：`aweme_info.images` 非空则跳过。
- 抖音 web 端**隐藏播放量**，`statistics.play_count` 恒返回 `0`，不要建「播放量」列（已踩过）。

搜索 URL：
```
https://www.douyin.com/search/{关键词}?type=video&publish_time={1|7|182|0}&sort_type={0|1|2}
```
`publish_time`: 1=一天 7=一周 182=半年 0=不限；`sort_type`: 0=综合 1=最多点赞 2=最新。

### 坑 2：Playwright Python 没有 `page.off()`
只有 `page.remove_listener(event, handler)`。用 `off()` 直接 AttributeError。

### 坑 3：验证码检测极易误报
`secsdk` / `captcha` / `verifycenter` 在抖音**正常页面** JS 里本来就有，拿它们判定会 100% 误报。
只能用：
- 页面中文文案：`滑动验证`、`拖动滑块`、`请完成安全验证`、`向右滑动`
- 或 URL 跳转：`/verify`、`verifycenter`、`security-check`

### 坑 4：跨关键词会重复
多个关键词会搜到同一个视频。只做「和历史去重」不够，
**必须先按 `aweme_id` 合并本次结果**（保留点赞数最高的那份），再去重入库。

### 坑 5（飞书）：`record-list` 输出到相对路径会报已存在
`lark-cli base +record-list ... --output _recs.ndjson` 若文件已存在会失败：
```
"message": "output file already exists: _recs.ndjson",
"hint": "Pass --overwrite to replace the ndjson artifact and manifest."
```
**必须加 `--overwrite`**，否则重跑必挂（还会连带生成 `_recs.manifest.json` 一起卡住）。

### 坑 6（ASR）：faster-whisper 模型下载报 502/401
默认 HuggingFace 源会被墙/限流。必须：
```bash
export HF_ENDPOINT=https://hf-mirror.com   # 走国内镜像
export HF_HUB_DISABLE_XET=1                # 禁用 Xet 下载（否则 401）
```
否则 `WhisperModel("small")` 加载阶段直接失败。

---

## 四、项目结构（本机实际落地的目录）

```
/Users/proust/WorkBuddy/2026-08-31-16-43-48/douyin-harvest/
├── config.json          # min_likes=1000, max_download_per_run=3, publish_time=7, keywords[5]
├── core.py             # get_conn() / launch_browser(minimize=True) / setup_logger / mark()
├── harvest.py          # 搜索→解析→过滤→去重→导出cookie→yt-dlp 下载
├── login.py            # 扫码登录（不最小化，方便用户操作）
├── sync_feishu.py      # 读 seen.db 未同步记录 → 建行 + 上传附件
├── extract_scripts.py  # faster-whisper 对未转写视频做 ASR → 写 DB script 列 + .txt
├── sync_scripts.py     # 按视频链接匹配飞书记录 → 批量回填「脚本」
├── run.sh              # 入口：login / run / check；run 串联 harvest→sync→extract→sync
├── data/
│   ├── seen.db         # SQLite，downloads 表 16 列（见下）
│   └── logs/
└── （视频存 ~/Movies/抖音切片/<发布日期>/ 按日分子目录）
```

### `downloads` 表结构（16 列）
`aweme_id, title, author, likes, create_time, keyword, filepath, status,
downloaded_at, play_count, comment_count, share_count, video_url,
feishu_synced, crawled_at, script`

> `play_count` 故意保留但恒为 0（抖音不返回）；`script` 列由 `extract_scripts.py` 的
> `ensure_col()` 懒添加：`ALTER TABLE downloads ADD COLUMN script TEXT`。

---

## 五、飞书多维表格格式（核心交付物）

> 本项目真实表格，可直接照搬字段与排序。

- **Base**：`https://my.feishu.cn/base/YAdIbvvwraxTRDsPtDwcV51tnbe`
- `base_token`：`YAdIbvvwraxTRDsPtDwcV51tnbe`
- `table_id`：`tbl4opt82L9xZEuX`
- 排序视图 `vew7MJelTU`：**按「抓取时间」倒序**（最新的在最上面）

### 字段清单（9 列，顺序即表格列序）

| # | 列名 | 类型 | field_id | 来源 |
| --- | --- | --- | --- | --- |
| 1 | 标题 | 文本 | `fldiDGt1ML` | `desc`（去话题标签前 60 字或完整） |
| 2 | 发布日期 | 日期时间 | `fldC2Byl1x` | `create_time` 转 datetime |
| 3 | 点赞量 | 数字 | `fldd19RxbN` | `statistics.digg_count` |
| 4 | 评论数 | 数字 | `fldGu8vIyg` | `statistics.comment_count` |
| 5 | 转发量 | 数字 | `fld8lBKw1I` | `statistics.share_count` |
| 6 | 视频链接 | 链接/文本 | `fld4gRTopv` | `video_url`（飞书存为 `[url](url)`） |
| 7 | 视频附件 | 附件 | `fldSdiEgHA` | 上传本地 .mp4 |
| 8 | 抓取时间 | 日期时间 | `fld3dIP4mQ` | 写入飞书时的 `now()`（排序键） |
| 9 | 脚本 | 文本 | （field-create 生成） | ASR 转写全文 |

> 历史变更：原「播放量」列因抖音不返回已**删除**；「抓取时间」为后补；「脚本」最后加。
> 加列命令：
> ```bash
> lark-cli base +field-create --base-token YAdIbvvwraxTRDsPtDwcV51tnbe \
>   --table-id tbl4opt82L9xZEuX --json '{"name":"脚本","type":"text"}' --format json
> ```
> 倒序视图：
> ```bash
> lark-cli base +view-set-sort --base-token YAdIbvvwraxTRDsPtDwcV51tnbe \
>   --table-id tbl4opt82L9xZEuX --view-id vew7MJelTU \
>   --json '{"sort":[{"field_name":"抓取时间","desc":true}]}' --format json
> ```

### 同步逻辑要点（`sync_feishu.py` / `sync_scripts.py`）
- **拉记录**：`record-list --field-id 视频链接 --format ndjson --output _recs.ndjson --overwrite`，
  逐行解析，`record_id` 行里「视频链接」值是 markdown `[url](url)`，用正则
  `\]\((https?://[^)]+)\)` 抽出纯 URL 作匹配键。
- **匹配**：DB 的 `video_url` 与飞书 URL 对齐；匹配不到的 `warning` 跳过。
- **回填脚本**：`record-batch-update --json @_script_batch.json`，payload 形如
  `{"update_records": {"<record_id>": {"脚本": "<全文>"}}}`——**字段名可直接用中文「脚本」**。
- 脚本为空则跳过，可安全重跑。

---

## 六、语音转脚本（脚本层）

抖音视频**无字幕轨**，只能本地 ASR。流程在 `extract_scripts.py`：

```python
from faster_whisper import WhisperModel
model = WhisperModel("small", device="cpu", compute_type="int8")
segments, _ = model.transcribe(path, language="zh",
                               vad_filter=True,
                               vad_parameters=dict(min_silence_duration_ms=500))
lines = [f"[{s.start:.1f}-{s.end:.1f}]  {s.text.strip()}" for s in segments]
```
- 输出带 `[起-止]` 时间戳，方便定位；全文写入 DB `script` 列 + 视频同名 `.txt`。
- **误识别规整**（用户专属，按项目补）：`CORRECTIONS = [("鞋片","切片"), ("哲哥","泽哥")]`，
  转写后 `text.replace` 一遍。换主播时改这个列表即可。
- **已提取自动跳过**：`WHERE status='ok' AND (script IS NULL OR script='')`，每日只转新视频。
- 模型只加载一次循环处理；`small` 对长回放（34 分钟）约 10 分钟（CPU）。要更准换 `medium`
  （多一倍模型体积、更慢），但首次需重新下载。
- 运行前务必 `export HF_ENDPOINT=https://hf-mirror.com && export HF_HUB_DISABLE_XET=1`。

> 实测：23 秒测试视频转写准确；small 模型对口语/梗词偶有听错，属正常误差。

---

## 七、浏览器后台最小化（不打扰用户）

`launch_browser(minimize=True)`：Playwright 启动后，用 **AppleScript** 把 Chrome 窗口最小化：
```applescript
tell application "System Events" to set miniaturized of (windows of process "Chrome" whose visible is true) to true
```
- 抓取任务用 `minimize=True`（后台静默跑）；
- `login.py` 用 `minimize=False`（用户要扫码，必须可见）。

---

## 八、落地步骤

1. 建 venv 装依赖（**pip 默认源只有 40kB/s，改用清华镜像**）：
   ```bash
   python -m venv envs/douyin && envs/douyin/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
     playwright faster-whisper
   envs/douyin/bin/python -m playwright install chromium
   ```
2. 系统装 `yt-dlp` + `ffmpeg`（`brew install yt-dlp ffmpeg`）。
3. `login.py`：启动 Chrome 打开抖音，轮询 cookie 等用户扫码，
   检测到 `sessionid` / `sessionid_ss` 即成功。
4. `harvest.py`：搜索 → 解析 → 过滤（按 `min_likes`、`publish_time`）→ 按 `aweme_id` 合并去重
   → 导出 cookie（**Netscape 格式**：domain 前加点 `.douyin.com`，session cookie 的 expiry 写 0）
   → `yt-dlp --cookies` 下载到 `~/Movies/抖音切片/<日期>/`。
5. `sync_feishu.py`：读未同步记录 → `record-batch-create` 建行 → 上传附件到「视频附件」。
6. `extract_scripts.py`：ASR 转写（设好 HF 环境变量）→ 写 DB + `.txt`。
7. `sync_scripts.py`：回填「脚本」列（务必 `--overwrite` 拉记录）。
8. 用 `automation` 建**每日定时任务**调 `./run.sh run`（20:00 等低谷时段）。

---

## 九、实用的命令行开关

给脚本加这三个参数，调试效率极高且不污染配置：
- `--dry-run` 只采集不下载，验证抓取效果
- `--limit N` 本次最多下 N 个
- `--days N` 临时覆盖时间范围

**新需求先跑 `--dry-run` 看抓到什么，确认内容相关了再真下载。**

---

## 十、风控与合规

- 用**有头模式**（headless 抖音查得严），定时任务跑时会弹几秒窗口，属正常。
- 关键词之间 sleep 4~9 秒，下载间隔 sleep 2~5 秒，随机化。
- 每天只跑一次；历史补抓用 `--days` 手动触发，别放进定时任务。
- 合规：下载内容仅限个人归档与素材研究，版权归原作者；提醒用户不要二次传播或商用。

---

## 十一、一键串联入口（`run.sh`）

```bash
./run.sh login   # 首次/失效时扫码
./run.sh run     # 抓取→同步飞书→提取脚本→回填脚本（定时任务调这个）
./run.sh check   # 检查登录态是否还有效
```
`run` 分支：
```bash
"$PY" harvest.py "$@"
"$PY" sync_feishu.py || echo "⚠️ 飞书同步失败，下次自动重试"
"$PY" extract_scripts.py || echo "⚠️ 脚本提取失败，下次自动重试"
"$PY" sync_scripts.py || echo "⚠️ 脚本同步失败，下次自动重试"
```
