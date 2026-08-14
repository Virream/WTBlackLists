"""导入/导出功能 专项测试(离屏)。运行: .venv\\Scripts\\python.exe tests\\import_export_test.py"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wt81111g.blacklist import BlacklistEntry, BlacklistStore
from wt81111g.import_export import (
    export_zip,
    format_size,
    import_zip,
)

results = []


def test_format_size() -> None:
    assert format_size(500) == "500 B"
    assert format_size(5 * 1024) == "5.0 KB"
    assert format_size(5 * 1024 * 1024) == "5.00 MB"
    assert format_size(2 * 1024 * 1024 * 1024) == "2.00 GB"
    results.append("format_size OK")


def _mk_entry(pid: str, nick: str, eid: str = "") -> BlacklistEntry:
    e = BlacklistEntry(player_id=pid, nickname=nick, event_date="2026-08-01")
    e.entry_id = eid or f"{pid}_20260801_10_00"
    return e


def test_export_include_evidence() -> None:
    tmp = tempfile.mkdtemp()
    import wt81111g.import_export as ie
    ev_root = os.path.join(tmp, "evidences")
    ie.evidences_dir = lambda: ev_root  # 重定向到临时目录
    # 造一条证据文件
    e1 = _mk_entry("111", "张三", "111_20260801_10_00")
    ev_file = os.path.join(ev_root, "111", e1.entry_id, "截图.png")
    os.makedirs(os.path.dirname(ev_file), exist_ok=True)
    with open(ev_file, "wb") as f:
        f.write(b"\x89PNG fake")

    out = os.path.join(tmp, "out.zip")
    stats = export_zip([e1], out, include_evidence=True)
    assert stats["entries"] == 1
    assert stats["evidence"] is True
    assert stats["evidence_files"] == 1
    assert os.path.exists(out)

    import zipfile
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        assert "entries.json" in names
        assert "evidences/111/111_20260801_10_00/截图.png" in names, names
    results.append("export include evidence OK")


def test_export_without_evidence() -> None:
    tmp = tempfile.mkdtemp()
    import wt81111g.import_export as ie
    ie.evidences_dir = lambda: os.path.join(tmp, "evidences")
    e1 = _mk_entry("111", "张三", "111_20260801_10_00")
    out = os.path.join(tmp, "out2.zip")
    stats = export_zip([e1], out, include_evidence=False)
    assert stats["evidence"] is False and stats["evidence_files"] == 0
    results.append("export without evidence OK")


def test_import_append_mode() -> None:
    tmp = tempfile.mkdtemp()
    src = BlacklistStore(os.path.join(tmp, "src.json"))
    src.entries = [_mk_entry("111", "张三"), _mk_entry("222", "李四")]
    out = os.path.join(tmp, "src.zip")
    export_zip(src.entries, out, include_evidence=False)

    store = BlacklistStore(os.path.join(tmp, "dst.json"))
    stats = import_zip(out, store, mode="append")
    assert stats["imported"] == 2
    assert stats["new_ids"] == 2
    assert len(store.entries) == 2
    results.append("import append mode OK")


def test_import_pid_dedup() -> None:
    tmp = tempfile.mkdtemp()
    src = BlacklistStore(os.path.join(tmp, "src.json"))
    src.entries = [
        _mk_entry("111", "张三"),
        _mk_entry("222", "李四"),
        _mk_entry("333", "王五"),
    ]
    out = os.path.join(tmp, "src.zip")
    export_zip(src.entries, out, include_evidence=False)

    store = BlacklistStore(os.path.join(tmp, "dst.json"))
    store.entries = [_mk_entry("111", "已存在张三", "111_20260101_00_00")]
    store.save()
    stats = import_zip(out, store, mode="pid")
    assert stats["imported"] == 2, stats      # 111 已存在 → 跳过
    assert stats["new_ids"] == 2
    assert len(store.entries) == 3            # 1(原有) + 2(新增)
    pids = {(e.player_id or "").strip() for e in store.entries}
    assert pids == {"111", "222", "333"}
    results.append("import pid dedup OK")


def test_import_restore_evidence() -> None:
    tmp = tempfile.mkdtemp()
    import wt81111g.import_export as ie
    ev_root_src = os.path.join(tmp, "ev_src")
    ie.evidences_dir = lambda: ev_root_src
    e1 = _mk_entry("111", "张三", "111_20260801_10_00")
    ev_file = os.path.join(ev_root_src, "111", e1.entry_id, "clip.mp4")
    os.makedirs(os.path.dirname(ev_file), exist_ok=True)
    with open(ev_file, "wb") as f:
        f.write(b"MP4-fake")

    out = os.path.join(tmp, "src.zip")
    export_zip([e1], out, include_evidence=True)

    ev_root_dst = os.path.join(tmp, "ev_dst")
    ie.evidences_dir = lambda: ev_root_dst  # 导入时恢复到新目录
    store = BlacklistStore(os.path.join(tmp, "dst.json"))
    stats = import_zip(out, store, mode="append")
    assert stats["has_evidence"] is True
    assert stats["evidence_restored"] == 1
    restored = os.path.join(ev_root_dst, "111", e1.entry_id, "clip.mp4")
    assert os.path.exists(restored)
    results.append("import restore evidence OK")


def test_import_skip_evidence() -> None:
    """未勾选“导入证据文件”时, 证据文件不应被恢复。"""
    tmp = tempfile.mkdtemp()
    import wt81111g.import_export as ie
    ev_root_src = os.path.join(tmp, "ev_src")
    ie.evidences_dir = lambda: ev_root_src
    e1 = _mk_entry("111", "张三", "111_20260801_10_00")
    ev_file = os.path.join(ev_root_src, "111", e1.entry_id, "clip.mp4")
    os.makedirs(os.path.dirname(ev_file), exist_ok=True)
    with open(ev_file, "wb") as f:
        f.write(b"MP4-fake")
    out = os.path.join(tmp, "src.zip")
    export_zip([e1], out, include_evidence=True)

    ev_root_dst = os.path.join(tmp, "ev_dst")
    ie.evidences_dir = lambda: ev_root_dst
    store = BlacklistStore(os.path.join(tmp, "dst.json"))
    stats = import_zip(out, store, mode="append", restore_evidence=False)
    assert stats["has_evidence"] is True
    assert stats["evidence_restored"] == 0
    assert not os.path.exists(os.path.join(ev_root_dst, "111", e1.entry_id, "clip.mp4"))
    results.append("import skip evidence OK")


def test_import_evidence_truncated() -> None:
    """磁盘空间不足时证据恢复被截断, 应置 evidence_truncated 标志。"""
    tmp = tempfile.mkdtemp()
    import wt81111g.import_export as ie
    ev_root_src = os.path.join(tmp, "ev_src")
    ie.evidences_dir = lambda: ev_root_src
    e1 = _mk_entry("111", "张三", "111_20260801_10_00")
    ev_file = os.path.join(ev_root_src, "111", e1.entry_id, "clip.mp4")
    os.makedirs(os.path.dirname(ev_file), exist_ok=True)
    with open(ev_file, "wb") as f:
        f.write(b"x" * (1024 * 1024))  # 1MB 证据
    out = os.path.join(tmp, "src.zip")
    export_zip([e1], out, include_evidence=True)

    ie.evidences_dir = lambda: os.path.join(tmp, "ev_dst")
    real_disk_usage = ie.shutil.disk_usage

    class _Fake:
        def __init__(self):
            self.free = 100 * 1024  # 模拟只剩 100KB

    ie.shutil.disk_usage = lambda p: _Fake()
    try:
        store = BlacklistStore(os.path.join(tmp, "dst.json"))
        stats = import_zip(out, store, mode="append")
    finally:
        ie.shutil.disk_usage = real_disk_usage
    assert stats["evidence_restored"] == 0
    assert stats["evidence_truncated"] is True, stats
    results.append("import evidence truncated flag OK")


def test_export_compression_mode() -> None:
    """含证据时 zip 用 store(不压缩, 音视频已压缩); 纯条目时才用 deflate。"""
    tmp = tempfile.mkdtemp()
    import wt81111g.import_export as ie
    ev_root = os.path.join(tmp, "ev")
    ie.evidences_dir = lambda: ev_root
    e1 = _mk_entry("111", "张三", "111_20260801_10_00")
    ev_file = os.path.join(ev_root, "111", e1.entry_id, "clip.mp4")
    os.makedirs(os.path.dirname(ev_file), exist_ok=True)
    with open(ev_file, "wb") as f:
        f.write(b"x" * 1000)

    import zipfile
    # 含证据 → STORED
    out1 = os.path.join(tmp, "a.zip")
    export_zip([e1], out1, include_evidence=True)
    with zipfile.ZipFile(out1) as z:
        assert z.getinfo("evidences/111/111_20260801_10_00/clip.mp4").compress_type == zipfile.ZIP_STORED
        assert z.getinfo("entries.json").compress_type == zipfile.ZIP_STORED
    # 纯条目 → DEFLATED
    out2 = os.path.join(tmp, "b.zip")
    export_zip([e1], out2, include_evidence=False)
    with zipfile.ZipFile(out2) as z:
        assert z.getinfo("entries.json").compress_type == zipfile.ZIP_DEFLATED
    results.append("export compression mode OK")


def main() -> int:
    for fn in (
        test_format_size,
        test_export_include_evidence,
        test_export_without_evidence,
        test_export_compression_mode,
        test_import_append_mode,
        test_import_pid_dedup,
        test_import_restore_evidence,
        test_import_skip_evidence,
        test_import_evidence_truncated,
    ):
        fn()
    for r in results:
        print("OK:", r)
    print("IMPORT/EXPORT TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
