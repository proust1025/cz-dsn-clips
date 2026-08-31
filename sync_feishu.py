"""把 SQLite 里已下载、且尚未同步到飞书的视频，写入飞书多维表格并上传附件。

- 复用 core.get_conn()（会自动迁移 feishu_synced 列）
- 只在 feishu_synced=0 的记录上工作，避免重复建行
- 批量建记录后逐个上传本地 mp4 附件（lark-cli 只接受相对路径，故 chdir 到视频根目录）
"""
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from core import load_config, get_conn, setup_logger  # noqa: E402

BASE_TOKEN = "YAdIbvvwraxTRDsPtDwcV51tnbe"
TABLE_ID = "tbl4opt82L9xZEuX"
ATTACH_FIELD = "视频附件"
LARK = "/opt/homebrew/bin/lark-cli"
VIDEO_ROOT = Path(os.path.expanduser("~/Movies/抖音切片"))


def run_lark(args, cwd=None):
    return subprocess.run(
        [LARK, *args], capture_output=True, text=True, cwd=cwd, timeout=180
    )


def main():
    log = setup_logger("feishu_sync")
    cfg = load_config()
    conn = get_conn()
    rows = conn.execute(
        "SELECT aweme_id,title,author,likes,create_time,play_count,comment_count,"
        "share_count,video_url,filepath,feishu_synced,crawled_at FROM downloads "
        "WHERE status='ok' AND (feishu_synced IS NULL OR feishu_synced=0) "
        "ORDER BY likes DESC"
    ).fetchall()
    if not rows:
        log.info("没有需要同步到飞书的新视频")
        return
    log.info(f"准备同步 {len(rows)} 个视频到飞书多维表格")

    records = []
    for r in rows:
        (aweme_id, title, author, likes, create_time, play, comment, share,
         url, filepath, synced, crawled_at) = r
        pub = datetime.datetime.fromtimestamp(create_time).strftime("%Y-%m-%d") if create_time else ""
        rec = {
            "标题": (title or "(无标题)")[:300],
            "发布日期": pub,
            "点赞量": int(likes or 0),
            "评论数": int(comment or 0),
            "转发量": int(share or 0),
        }
        # 抓取时间：来自 SQLite 的 crawled_at（ISO 格式），转成飞书认的 yyyy-MM-dd HH:mm:ss
        if crawled_at:
            rec["抓取时间"] = crawled_at.replace("T", " ").replace("Z", "")[:19]
        if url:
            rec["视频链接"] = url
        records.append(rec)

    payload = {"create_records": records}
    batch_json = VIDEO_ROOT / "_feishu_batch.json"
    batch_json.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    res = run_lark(
        ["base", "+record-batch-create", "--base-token", BASE_TOKEN,
         "--table-id", TABLE_ID, "--json", f"@{batch_json.name}", "--as", "user",
         "--format", "json"],
        cwd=str(VIDEO_ROOT),
    )
    if res.returncode != 0 or "error" in (res.stdout or ""):
        log.error(f"批量建记录失败: {res.stdout[:300]} {res.stderr[:200]}")
        return
    data = json.loads(res.stdout)
    rids = (data.get("data") or {}).get("record_id_list") or data.get("record_id_list")
    if not rids:
        log.error(f"未拿到 record_id_list，返回: {res.stdout[:400]}")
        return
    log.info(f"已创建 {len(rids)} 条飞书记录")

    ok = 0
    for rid, r in zip(rids, rows):
        aweme_id = r[0]
        filepath = Path(r[9])
        if not filepath.exists():
            log.warning(f"本地文件缺失，跳过附件: {filepath}")
        else:
            rel = os.path.relpath(filepath, VIDEO_ROOT)
            ar = run_lark(
                ["base", "+record-upload-attachment", "--base-token", BASE_TOKEN,
                 "--table-id", TABLE_ID, "--record-id", rid, "--field-id", ATTACH_FIELD,
                 "--file", rel, "--as", "user", "--format", "json"],
                cwd=str(VIDEO_ROOT),
            )
            if ar.returncode != 0 or "error" in (ar.stdout or ""):
                log.warning(f"附件上传失败 {aweme_id}: {ar.stdout[:200]}")
                continue
        conn.execute("UPDATE downloads SET feishu_synced=1 WHERE aweme_id=?", (aweme_id,))
        conn.commit()
        ok += 1
        log.info(f"已同步并上传附件: {aweme_id}")

    batch_json.unlink(missing_ok=True)
    log.info(f"本次同步完成：{ok}/{len(rows)} 个视频已写入飞书并标记")


if __name__ == "__main__":
    main()
