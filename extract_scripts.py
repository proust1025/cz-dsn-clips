"""批量语音转文字：对 downloaded 状态、本地文件存在的视频用 faster-whisper 提取脚本。

- 模型只加载一次，循环处理所有待提取视频
- 已知误识别词做规整：鞋片→切片、哲哥→泽哥
- 结果写入 data/seen.db 的 script 列，并写到视频同名的 .txt 文件
- 已提取过的（script 非空）会自动跳过，可安全重跑
"""
import os
import sys
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from core import get_conn, setup_logger  # noqa: E402

# 已知误识别词规整（用户确认：鞋片=切片，哲哥=泽哥）
CORRECTIONS = [("鞋片", "切片"), ("哲哥", "泽哥")]


def postprocess(text: str) -> str:
    for wrong, right in CORRECTIONS:
        text = text.replace(wrong, right)
    return text


def ensure_col(conn: sqlite3.Connection):
    try:
        conn.execute("ALTER TABLE downloads ADD COLUMN script TEXT")
    except sqlite3.OperationalError:
        pass  # 已存在


def main():
    log = setup_logger("extract_scripts")
    conn = get_conn()
    ensure_col(conn)
    rows = conn.execute(
        "SELECT aweme_id, filepath, title FROM downloads "
        "WHERE status='ok' AND (script IS NULL OR script='') "
        "ORDER BY likes DESC"
    ).fetchall()
    if not rows:
        log.info("所有视频脚本均已提取，无需重复")
        return
    log.info(f"待提取脚本的视频：{len(rows)} 个")

    # 延迟导入，避免无任务时也加载 torch
    from faster_whisper import WhisperModel

    log.info("加载 faster-whisper small 模型（首次约需十几秒）…")
    model = WhisperModel("small", device="cpu", compute_type="int8")

    done = 0
    for aweme_id, filepath, title in rows:
        p = Path(filepath)
        if not p.exists():
            log.warning(f"本地文件缺失，跳过：{filepath}")
            continue
        log.info(f"▶ 开始转写：{title[:32]}  ({p.name})")
        segments, _info = model.transcribe(
            str(p),
            language="zh",
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )
        lines = [f"[{s.start:.1f}-{s.end:.1f}]  {s.text.strip()}" for s in segments]
        full = postprocess("\n".join(lines))

        out = p.with_suffix(".txt")
        out.write_text(full, encoding="utf-8")
        conn.execute(
            "UPDATE downloads SET script=? WHERE aweme_id=?", (full, aweme_id)
        )
        conn.commit()
        done += 1
        log.info(f"✓ 完成：{len(lines)} 句 / {len(full)} 字 → {out.name}")

    log.info(f"全部脚本提取完成：{done}/{len(rows)} 个视频")


if __name__ == "__main__":
    main()
