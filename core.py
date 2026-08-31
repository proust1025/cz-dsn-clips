"""公共工具：配置、日志、去重数据库、Cookie 导出。"""
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "config.json"
DB_PATH = PROJECT_DIR / "data" / "seen.db"
COOKIE_PATH = PROJECT_DIR / "data" / "cookies.txt"
LOG_DIR = PROJECT_DIR / "data" / "logs"
PROFILE_DIR = PROJECT_DIR / "chrome-profile"

_ILLEGAL = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["output_dir"] = str(Path(cfg["output_dir"]).expanduser())
    return cfg


def setup_logger(name: str = "harvest") -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

    fh = logging.FileHandler(LOG_DIR / f"{stamp}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def sanitize(text: str, limit: int = 40) -> str:
    clean = _ILLEGAL.sub("", (text or "").strip())
    clean = re.sub(r"\s+", " ", clean)
    return clean[:limit].strip() or "untitled"


# ---------- 去重数据库 ----------

def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS downloads (
            aweme_id     TEXT PRIMARY KEY,
            title        TEXT,
            author       TEXT,
            likes        INTEGER,
            create_time  INTEGER,
            keyword      TEXT,
            filepath     TEXT,
            status       TEXT,
            downloaded_at TEXT
        )
        """
    )
    # 增量迁移：补齐飞书多维表格所需的统计字段（不影响已有数据）
    for col, ddl in [
        ("play_count", "INTEGER"),
        ("comment_count", "INTEGER"),
        ("share_count", "INTEGER"),
        ("video_url", "TEXT"),
        ("feishu_synced", "INTEGER"),
        ("crawled_at", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE downloads ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError:
            pass  # 列已存在
    # 历史行没有 crawled_at，用已存在的 downloaded_at 兜底（都发生在同一天）
    conn.execute(
        "UPDATE downloads SET crawled_at = downloaded_at WHERE crawled_at IS NULL AND downloaded_at IS NOT NULL"
    )
    conn.commit()
    return conn


def seen_ids(conn: sqlite3.Connection) -> set:
    return {row[0] for row in conn.execute("SELECT aweme_id FROM downloads")}


def mark(conn: sqlite3.Connection, item: dict, filepath: str, status: str):
    now = datetime.now().isoformat(timespec="seconds")
    # 用显式列名，避免表结构迁移后列数/顺序对不上导致 INSERT 报错的隐患
    conn.execute(
        "INSERT OR REPLACE INTO downloads "
        "(aweme_id,title,author,likes,create_time,keyword,filepath,status,downloaded_at,"
        "play_count,comment_count,share_count,video_url,feishu_synced,crawled_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            item["aweme_id"],
            item["title"],
            item["author"],
            item["likes"],
            item.get("create_time", 0),
            item.get("keyword", ""),
            filepath,
            status,
            now,  # downloaded_at
            int(item.get("play_count") or 0),
            int(item.get("comment_count") or 0),
            int(item.get("share_count") or 0),
            item.get("video_url", ""),
            0,  # feishu_synced：新下载的标记为未同步
            now,  # crawled_at（抓取时间）
        ),
    )
    conn.commit()


def update_stats(conn: sqlite3.Connection, aweme_id: str, play=0, comment=0, share=0, video_url=""):
    """补采：只更新统计数据，不破坏已有的下载路径 / 状态。"""
    conn.execute(
        "UPDATE downloads SET play_count=?, comment_count=?, share_count=?, video_url=? WHERE aweme_id=?",
        (int(play or 0), int(comment or 0), int(share or 0), video_url, aweme_id),
    )
    conn.commit()


# ---------- Cookie 导出为 Netscape 格式（供 yt-dlp 使用） ----------

def export_cookies(cookies: list, path: Path = COOKIE_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Netscape HTTP Cookie File", ""]
    for c in cookies:
        domain = c.get("domain", "")
        if not domain:
            continue
        if not domain.startswith("."):
            domain = "." + domain
        expires = c.get("expires", -1)
        try:
            expires = int(float(expires))
        except (TypeError, ValueError):
            expires = -1
        if expires < 0:
            expires = 0
        lines.append(
            "\t".join(
                [
                    domain,
                    "TRUE",
                    c.get("path", "/"),
                    "TRUE" if c.get("secure") else "FALSE",
                    str(expires),
                    c.get("name", ""),
                    c.get("value", ""),
                ]
            )
        )
    path.write_text("\n".join(lines), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


LOGIN_COOKIES = ("sessionid", "sessionid_ss", "sid_tt", "passport_csrf_token")


def is_logged_in(cookies: list) -> bool:
    """抖音登录后会把 sessionid 写入 cookie，据此判断登录态是否还有效。"""
    names = {c.get("name") for c in cookies}
    return any(k in names for k in ("sessionid", "sessionid_ss"))


def launch_browser(playwright, cfg: dict, logger, minimize: bool = False):
    """启动复用登录态的系统 Chrome。

    minimize=True 时把窗口移出屏幕并最小化到后台（macOS），
    这样自动抓取的浏览器不会挡在你正在做的工作前面。
    注意：登录（login.py）不要传 minimize，因为你要扫码看二维码。
    """
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if minimize and not cfg.get("headless", False):
        # 先把窗口推到屏幕外，再用 AppleScript 最小化，双保险
        args.append("--window-position=-32000,-32000")
        args.append("--window-size=1280,800")
    ctx = playwright.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        channel="chrome",
        headless=cfg.get("headless", False),
        viewport={"width": 1440, "height": 900},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        args=args,
        ignore_default_args=["--enable-automation"],
    )
    if minimize and not cfg.get("headless", False):
        try:
            ctx.new_page()  # 确保至少有一个页面，避免后续取 pages[0] 为空
        except Exception:
            pass
        minimize_chrome_window(logger)
    return ctx


def minimize_chrome_window(logger):
    """把刚启动的 Chrome 窗口最小化到后台（仅 macOS）。"""
    try:
        script = (
            'tell application "Google Chrome"\n'
            '  if (count of windows) > 0 then set miniaturized of window 1 to true\n'
            "end tell"
        )
        subprocess.run(["osascript", "-e", script], timeout=10, capture_output=True)
        logger.info("Chrome 窗口已最小化到后台，不会挡你工作")
    except Exception as exc:  # 最小化失败不影响抓取
        logger.warning(f"最小化窗口失败（不影响抓取）：{exc}")
