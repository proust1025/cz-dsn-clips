"""把 SQLite 里已提取的脚本同步到飞书多维表格的「脚本」列。

- 按「视频链接」把 DB 记录匹配到飞书记录（链接在飞书里是 markdown 形式，需解析出纯 URL）
- 用 record-batch-update 一次性写入，可安全重跑（脚本为空则跳过）
"""
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from core import get_conn, setup_logger  # noqa: E402

BASE_TOKEN = "YAdIbvvwraxTRDsPtDwcV51tnbe"
TABLE_ID = "tbl4opt82L9xZEuX"
LARK = "/opt/homebrew/bin/lark-cli"
FIELD = "脚本"


def run_lark(args):
    return subprocess.run(
        [LARK, *args], capture_output=True, text=True, timeout=300, cwd=str(HERE)
    )


def main():
    log = setup_logger("sync_scripts")
    conn = get_conn()
    rows = conn.execute(
        "SELECT aweme_id, video_url, script FROM downloads "
        "WHERE status='ok' AND script IS NOT NULL AND script<>''"
    ).fetchall()
    if not rows:
        log.info("没有已提取的脚本可同步")
        return

    # 1) 拉取飞书记录，建立 视频链接 -> record_id 映射
    out = HERE / "_recs.ndjson"
    r = run_lark(
        ["base", "+record-list", "--base-token", BASE_TOKEN, "--table-id", TABLE_ID,
         "--field-id", "视频链接", "--format", "ndjson", "--output", out.name, "--limit", "200",
         "--overwrite"]
    )
    if r.returncode != 0:
        log.error(f"拉取飞书记录失败: {r.stderr[:300]}")
        return

    url_to_rid = {}
    for line in out.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(o, dict) and "record_id" in o:
            val = o.get("视频链接") or ""
            m = re.search(r"\]\((https?://[^)]+)\)", val)  # 飞书链接是 [url](url) 形式
            url = m.group(1) if m else val.strip()
            url_to_rid[url] = o["record_id"]
    log.info(f"飞书记录 {len(url_to_rid)} 条")

    # 2) 匹配并组装批量更新
    update = {}
    matched = 0
    for aweme_id, url, script in rows:
        rid = url_to_rid.get(url)
        if not rid:
            log.warning(f"未匹配到飞书记录（跳过）：{url}")
            continue
        update[rid] = {FIELD: script}
        matched += 1

    if not update:
        log.info("没有可写入的脚本")
        return

    payload = {"update_records": update}
    p = HERE / "_script_batch.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    r2 = run_lark(
        ["base", "+record-batch-update", "--base-token", BASE_TOKEN, "--table-id", TABLE_ID,
         "--json", f"@{p.name}", "--format", "json"]
    )
    if r2.returncode != 0 or "error" in (r2.stdout or ""):
        log.error(f"批量更新失败: {r2.stdout[:400]} {r2.stderr[:200]}")
    else:
        log.info(f"已写入 {matched} 条脚本到飞书「脚本」列")

    p.unlink(missing_ok=True)
    out.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
