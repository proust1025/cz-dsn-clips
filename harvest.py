"""每日自动抓取：按关键词搜索抖音 -> 筛点赞过线 -> 去重 -> 无水印下载归档。"""
import os
import random
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from core import (
    COOKIE_PATH,
    PROJECT_DIR,
    export_cookies,
    is_logged_in,
    load_config,
    launch_browser,
    get_conn,
    mark,
    sanitize,
    seen_ids,
    setup_logger,
)

LOCK_FILE = PROJECT_DIR / "data" / ".lock"
YTDLP = shutil.which("yt-dlp") or "/opt/homebrew/bin/yt-dlp"


def fmt_likes(n: int) -> str:
    if n >= 10000:
        return f"{n / 10000:.1f}w"
    return str(n)


def search_url(keyword: str, cfg: dict) -> str:
    from urllib.parse import quote

    return (
        "https://www.douyin.com/search/"
        + quote(keyword)
        + f"?type=video&publish_time={cfg['publish_time']}&sort_type={cfg['sort_type']}"
    )


def parse_item(raw: dict, keyword: str):
    """从搜索接口返回的条目里提取需要的字段。"""
    aweme = raw.get("aweme_info") or {}
    if not aweme.get("aweme_id"):
        return None
    # 只看真正的视频，跳过图文、直播卡片、广告
    video = aweme.get("video") or {}
    if not video.get("play_addr") and not video.get("play_addr_h264"):
        return None
    if aweme.get("images"):
        return None

    stats = aweme.get("statistics") or {}
    likes = stats.get("digg_count")
    if likes is None:
        likes = (aweme.get("statistics_v2") or {}).get("digg_count") or 0
    play = stats.get("play_count") or stats.get("play_count_raw")
    comment = stats.get("comment_count")
    share = stats.get("share_count")

    return {
        "aweme_id": aweme["aweme_id"],
        "title": (aweme.get("desc") or "").strip(),
        "author": (aweme.get("author") or {}).get("nickname") or "未知作者",
        "likes": int(likes or 0),
        "play_count": int(play or 0),
        "comment_count": int(comment or 0),
        "share_count": int(share or 0),
        "video_url": f"https://www.douyin.com/video/{aweme['aweme_id']}",
        "create_time": int(aweme.get("create_time") or 0),
        "keyword": keyword,
    }


def collect_keyword(page, cfg, keyword: str, log) -> list:
    """打开一个关键词的搜索页，边滚动边收集接口返回的数据。"""
    bucket = []

    def on_response(resp):
        # 抖音改过版：新接口是 /aweme/v1/web/search/item/，旧接口 general/search/single 保留兼容
        if "search/item" not in resp.url and "general/search/single" not in resp.url:
            return
        try:
            data = resp.json()
        except Exception:
            return
        for raw in (data.get("data") or data.get("aweme_list") or []):
            bucket.append(raw)

    page.on("response", on_response)
    url = search_url(keyword, cfg)
    log.info(f"搜索关键词：{keyword}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:
        log.warning(f"打开搜索页异常：{exc}")
    page.wait_for_timeout(random.randint(3000, 5000))

    for _ in range(int(cfg.get("scroll_rounds", 4))):
        page.mouse.wheel(0, random.randint(2500, 4000))
        page.wait_for_timeout(random.randint(2500, 4500))

    page.remove_listener("response", on_response)

    items, seen = [], set()
    for raw in bucket:
        it = parse_item(raw, keyword)
        if it and it["aweme_id"] not in seen:
            seen.add(it["aweme_id"])
            items.append(it)
    log.info(f"  抓到 {len(items)} 条视频")
    return items


def check_captcha(page, log) -> bool:
    """只在页面跳到验证页、或出现明确验证文案时才判定为真。

    注意：'captcha'、'secsdk'、'verifycenter' 这几个词在抖音正常页面的 JS 脚本里
    本来就有，绝不能拿它们做判定 —— 否则每次都会误报。
    """
    try:
        url = (page.url or "").lower()
        html = page.content()
    except Exception:
        return False

    hit = None
    for flag in ("滑动验证", "拖动滑块", "请完成安全验证", "向右滑动"):
        if flag in html:
            hit = f"页面出现「{flag}」"
            break
    if not hit:
        for flag in ("/verify", "verifycenter", "security-check"):
            if flag in url:
                hit = f"跳转到验证页（URL 含 {flag}）"
                break
    if not hit:
        return False

    shot = PROJECT_DIR / "data" / f"captcha-{int(time.time())}.png"
    try:
        page.screenshot(path=str(shot), full_page=False)
        log.error(f"触发验证码：{hit}，截图已保存 {shot}")
    except Exception:
        log.error(f"触发验证码：{hit}（截图失败）")
    return True


def download_one(item: dict, cfg: dict, cookie_path: Path, log) -> tuple:
    """调用 yt-dlp 下载单个视频，返回 (状态, 文件路径)。"""
    url = f"https://www.douyin.com/video/{item['aweme_id']}"
    ts = datetime.fromtimestamp(item["create_time"]) if item["create_time"] else datetime.now()
    folder = Path(cfg["output_dir"]) / ts.strftime("%Y-%m-%d")
    folder.mkdir(parents=True, exist_ok=True)

    base = f"{ts.strftime('%Y-%m-%d')}_{sanitize(item['author'], 20)}_{fmt_likes(item['likes'])}赞_{sanitize(item['title'], 45)}"
    target = folder / f"{base}.mp4"
    if target.exists():
        return "ok", str(target)

    cmd = [
        YTDLP,
        "--cookies", str(cookie_path),
        "-o", str(folder / f"{base}.%(ext)s"),
        "-f", "best[height<=1080]/best",
        "--merge-output-format", "mp4",
        "--retries", "3",
        "--socket-timeout", "30",
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=int(cfg.get("download_timeout", 300)))
    except subprocess.TimeoutExpired:
        return "failed", ""
    if proc.returncode != 0:
        log.warning(f"  下载失败 {item['aweme_id']}: {(proc.stderr or '').strip()[-200:]}")
        return "failed", ""
    if target.exists():
        return "ok", str(target)
    # yt-dlp 可能因重名自动加后缀，回查真实文件名
    for f in folder.glob(f"{base}.*"):
        if f.suffix.lower() in (".mp4", ".webm", ".mkv", ".mov"):
            return "ok", str(f)
    return "failed", ""


def acquire_lock(log) -> bool:
    if LOCK_FILE.exists():
        age = time.time() - LOCK_FILE.stat().st_mtime
        if age < 3 * 3600:
            log.warning("检测到上次任务仍在运行或异常退出，本次跳过")
            return False
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def main():
    log = setup_logger("harvest")
    cfg = load_config()
    argv = sys.argv[1:]
    dry_run = "--dry-run" in argv
    limit = None
    if "--limit" in argv:
        try:
            limit = int(argv[argv.index("--limit") + 1])
        except (ValueError, IndexError):
            limit = None
    # --days 临时覆盖配置里的时间范围，只对本次生效：1=一天 7=一周 182=半年 0=不限
    if "--days" in argv:
        try:
            days = str(int(argv[argv.index("--days") + 1]))
            cfg["publish_time"] = days
            log.info(f"本次时间范围临时改为：{days}（配置里是 {load_config()['publish_time']}）")
        except (ValueError, IndexError):
            pass
    log.info("=" * 46)
    log.info("开始本次抓取任务" + ("（试运行：只采集不下载）" if dry_run else ""))

    if not acquire_lock(log):
        return
    try:
        if not Path(YTDLP).exists():
            log.error("未找到 yt-dlp，请先安装：brew install yt-dlp")
            return

        with sync_playwright() as p:
            # 后台运行：自动最小化 Chrome，不挡用户正在做的工作
            ctx = launch_browser(p, cfg, log, minimize=True)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            if not is_logged_in(ctx.cookies()):
                log.error("抖音登录态已失效，请先运行 login.py 重新扫码")
                print("\n[需要你操作] 登录态失效了，跑一次下面这行重新扫码：")
                print(f"  cd {PROJECT_DIR} && ./run.sh login\n")
                ctx.close()
                return

            cookie_path = export_cookies(ctx.cookies())
            log.info("登录态有效")

            all_items = []
            for idx, kw in enumerate(cfg["keywords"]):
                items = collect_keyword(page, cfg, kw, log)
                all_items.extend(items)
                if check_captcha(page, log):
                    log.error("遇到验证码，中断本次任务，请手动在浏览器里过一次验证后重跑")
                    ctx.close()
                    return
                if idx < len(cfg["keywords"]) - 1:
                    time.sleep(random.randint(4, 9))

            ctx.close()

        # 跨关键词去重：不同关键词会搜到同一个视频，
        # 必须按 aweme_id 合并（保留点赞数最高的那份），否则同一个视频会被重复下载
        raw_count = len(all_items)
        merged = {}
        for it in all_items:
            prev = merged.get(it["aweme_id"])
            if prev is None or it["likes"] > prev["likes"]:
                merged[it["aweme_id"]] = it
        all_items = list(merged.values())
        log.info(f"跨关键词去重：{raw_count} 条 → {len(all_items)} 条不重复")

        # 过滤：点赞门槛
        threshold = int(cfg["min_likes"])
        hot = [i for i in all_items if i["likes"] >= threshold]
        log.info(f"共采集 {len(all_items)} 条，其中点赞 ≥ {threshold} 的有 {len(hot)} 条")

        if cfg.get("require_both_names"):
            both = [i for i in hot if "陈泽" in i["title"] and "迪士尼" in i["title"]]
            log.info(f"标题同时含两人的有 {len(both)} 条")
            hot = both

        # 过滤：历史去重
        conn = get_conn()
        known = seen_ids(conn)
        fresh = [i for i in hot if i["aweme_id"] not in known]
        log.info(f"剔除已下载的 {len(hot) - len(fresh)} 条，待下载 {len(fresh)} 条")

        fresh.sort(key=lambda x: x["likes"], reverse=True)
        cap = limit or int(cfg.get("max_download_per_run", 50))
        fresh = fresh[:cap]

        ok_list, fail_list = [], []
        for i, item in enumerate(fresh, 1):
            pub = datetime.fromtimestamp(item["create_time"]).strftime("%m-%d") if item["create_time"] else "??"
            log.info(f"[{i}/{len(fresh)}] {fmt_likes(item['likes'])}赞 {item['author']} | {pub} | {item['title'][:32]}")
            if dry_run:
                ok_list.append(item)
                continue
            status, path = download_one(item, cfg, cookie_path, log)
            mark(conn, item, path, status)
            (ok_list if status == "ok" else fail_list).append(item)
            time.sleep(random.randint(2, 5))

        write_report(cfg, ok_list, fail_list, log, dry_run)
        log.info(f"任务完成：成功 {len(ok_list)} 个，失败 {len(fail_list)} 个")
        log.info("=" * 46)
    finally:
        LOCK_FILE.unlink(missing_ok=True)


def write_report(cfg, ok_list, fail_list, log, dry_run=False):
    outdir = Path(cfg["output_dir"])
    rep_dir = outdir / "_每日报告"
    rep_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"# 抖音切片抓取报告 {today}", ""]
    if dry_run:
        lines.append(f"- 试运行，命中 **{len(ok_list)}** 个（未下载）")
    else:
        if ok_list:
            lines.append(f"- 本次成功下载：**{len(ok_list)}** 个")
        if fail_list:
            lines.append(f"- 本次失败：{len(fail_list)} 个")
    lines.append(f"- 保存位置：`{outdir}`")
    lines.append("")
    if ok_list:
        lines.append("## 本次命中（未下载）" if dry_run else "## 本次下载")
        lines.append("")
        lines.append("| 点赞 | 作者 | 标题 | 关键词 |")
        lines.append("| --- | --- | --- | --- |")
        for it in ok_list:
            title = it["title"].replace("|", "／").replace("\n", " ")[:36]
            lines.append(f"| {fmt_likes(it['likes'])} | {it['author']} | {title} | {it['keyword']} |")
        lines.append("")
    if fail_list:
        lines.append("## 下载失败（下次会重试）")
        lines.append("")
        for it in fail_list:
            lines.append(f"- {it['aweme_id']} · {fmt_likes(it['likes'])}赞 · {it['title'][:32]}")
        lines.append("")

    # 今日累计：一天可能跑多次，报告里补上当天全部战果，避免被最后一次运行覆盖
    conn = get_conn()
    rows = conn.execute(
        "SELECT likes, author, title FROM downloads "
        "WHERE status='ok' AND downloaded_at LIKE ? ORDER BY likes DESC",
        (f"{today}%",),
    ).fetchall()
    conn.close()
    if rows:
        lines.append(f"## 今日累计（{len(rows)} 个）")
        lines.append("")
        lines.append("| 点赞 | 作者 | 标题 |")
        lines.append("| --- | --- | --- |")
        for likes, author, title in rows:
            t = (title or "").replace("|", "／").replace("\n", " ")[:36]
            lines.append(f"| {fmt_likes(likes)} | {author} | {t} |")
        lines.append("")
    path = rep_dir / (f"{today}-试运行.md" if dry_run else f"{today}.md")
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info(f"报告已生成：{path}")


if __name__ == "__main__":
    main()
