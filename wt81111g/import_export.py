"""黑名单导入/导出: 导出选中条目为 zip 包(可选含证据文件), 从 zip 包导入。

导出包结构:
  entries.json            # 黑名单条目列表(asdict 序列化)
  evidences/<pid>/<eid>/  # 证据文件(仅在导出时勾选“导出证据文件”时写入)

导入模式:
  append: 追加模式 — 直接把文件所有条目追加到软件
  pid:    玩家ID导入 — 按玩家ID查重, 已有相同玩家ID则跳过该条
"""
from __future__ import annotations

import json
import os
import shutil
import zipfile
from dataclasses import asdict

from .blacklist import MAX_PREVIOUS_NICKNAMES, BlacklistEntry, BlacklistStore
from .config import evidences_dir

ENTRIES_NAME = "entries.json"
EVIDENCE_PREFIX = "evidences/"

# 导入防护: 不设单文件/文件数/解压总量上限(超高清录像单文件可超4GB),
# 仅保留"磁盘写满前自动停止"的动态防护。
MAX_ENTRIES_JSON = 32 * 1024 * 1024       # entries.json 上限 32MB
SAFETY_MARGIN = 256 * 1024 * 1024         # 写入后至少保留 256MB 磁盘余量(防恶意撑爆)


def format_size(n: float) -> str:
    """文件大小格式化; 超过 1GB 自动换算为 GB。"""
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.2f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{int(n)} B"


def _collect_evidence_files(entries: list[BlacklistEntry]) -> list[tuple[str, str, int]]:
    """收集待导出证据文件: 返回 [(zip内路径, 绝对路径, 大小)]。"""
    files: list[tuple[str, str, int]] = []
    root = evidences_dir()
    for e in entries:
        pid = (e.player_id or "").strip()
        eid = (e.entry_id or "").strip()
        if not pid or not eid:
            continue
        folder = os.path.join(root, pid, eid)
        if not os.path.isdir(folder):
            continue
        for dirpath, _dirs, names in os.walk(folder):
            for f in names:
                full = os.path.join(dirpath, f)
                rel = os.path.relpath(full, root)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                files.append((EVIDENCE_PREFIX + rel, full, size))
    return files


def export_zip(entries: list[BlacklistEntry], out_path: str,
               include_evidence: bool = False,
               progress_callback=None) -> dict:
    """把条目导出为 zip 包。返回统计 dict。

    progress_callback(done, total, name, size_bytes) 用于进度显示。
    原子写: 先写 <目标>.tmp, 完成后 os.replace, 避免中断留下损坏的半截文件。
    """
    data = [asdict(e) for e in entries]
    evidence_files = _collect_evidence_files(entries) if include_evidence else []
    total = 1 + len(evidence_files)  # entries.json + 证据文件
    done = 0
    # 音视频/图片已是压缩数据, 重压缩无效且耗时: 含证据时直接存储; 仅条目时才压缩
    compression = zipfile.ZIP_STORED if include_evidence else zipfile.ZIP_DEFLATED
    tmp_path = out_path + ".tmp"
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    try:
        with zipfile.ZipFile(tmp_path, "w", compression) as z:
            payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            z.writestr(ENTRIES_NAME, payload)
            done += 1
            if progress_callback:
                progress_callback(done, total, ENTRIES_NAME, len(payload))
            for zipname, full, size in evidence_files:
                z.write(full, zipname)
                done += 1
                if progress_callback:
                    progress_callback(done, total, zipname, size)
        os.replace(tmp_path, out_path)  # 原子替换
    finally:
        # 清理未完成的临时文件(异常/强杀后下次覆盖)
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
    return {
        "entries": len(entries),
        "evidence": include_evidence,
        "evidence_files": len(evidence_files),
        "size": os.path.getsize(out_path),
    }


# 导出/导入 速度估算(B/s), 仅用于“预计耗时”提示
_IO_SPEED_BPS = 60 * 1024 * 1024  # 约 60MB/s


def estimate_export(entries: list[BlacklistEntry], include_evidence: bool):
    """预估导出: 返回 (条目数, 总字节, 证据文件数, 预计秒数)。"""
    entry_count = len(entries)
    total = sum(len(json.dumps(asdict(e), ensure_ascii=False).encode()) for e in entries)
    files = _collect_evidence_files(entries) if include_evidence else []
    for _zipname, _full, size in files:
        total += size
    est = total / _IO_SPEED_BPS if total else 0.0
    return entry_count, total, len(files), est


def preview_import(zip_path: str) -> dict:
    """预览导入包(不写入): 返回统计 dict, 供导入设置窗口展示。"""
    file_size = os.path.getsize(zip_path)
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        if ENTRIES_NAME not in names:
            raise ValueError("文件中没有 entries.json, 不是有效的黑名单导出包")
        info = z.getinfo(ENTRIES_NAME)
        if info.file_size > MAX_ENTRIES_JSON:
            raise ValueError("entries.json 过大, 已取消")
        data = json.loads(z.read(ENTRIES_NAME).decode("utf-8"))
        evidence_names = [n for n in names if n.startswith(EVIDENCE_PREFIX)]
        evidence_bytes = sum(z.getinfo(n).file_size for n in evidence_names)
    entries = _parse_entries(data)
    est = file_size / _IO_SPEED_BPS if file_size else 0.0
    return {
        "entry_count": len(entries),
        "file_size": file_size,
        "evidence_count": len(evidence_names),
        "evidence_bytes": evidence_bytes,
        "est_seconds": est,
    }


def _parse_entries(data) -> list[BlacklistEntry]:
    """把反序列化后的列表转换为 BlacklistEntry, 容错跳过无效项。"""
    out: list[BlacklistEntry] = []
    for d in data if isinstance(data, list) else []:
        if not isinstance(d, dict):
            continue
        e = BlacklistEntry()
        for key in BlacklistEntry.__dataclass_fields__:
            if key in d:
                setattr(e, key, d[key])
        if not isinstance(e.previous_nicknames, list):
            e.previous_nicknames = []
        e.previous_nicknames = [str(x) for x in e.previous_nicknames][:MAX_PREVIOUS_NICKNAMES]
        try:
            e.fetched_at = float(e.fetched_at or 0)
        except (TypeError, ValueError):
            e.fetched_at = 0.0
        out.append(e)
    return out


def _restore_evidence(z: zipfile.ZipFile, names: list[str], done: int = 0,
                      total: int = 0, progress_callback=None) -> tuple[int, bool, int, int]:
    """把 zip 中的证据文件恢复到本地证据目录。
    返回 (恢复文件数, 是否被截断, 失败文件数, 累计完成数)。
    单个文件失败不中断; 累计写入量与磁盘余量检查, 写满前自动停止。"""
    root = os.path.normpath(evidences_dir())
    os.makedirs(root, exist_ok=True)  # 确保证据根目录存在, 供 disk_usage 查询
    restored = 0
    failed = 0
    written = 0
    truncated = False
    for n in names:
        rel = n[len(EVIDENCE_PREFIX):]
        if not rel:
            continue
        parts = rel.split("/")
        if len(parts) < 3 or not parts[0].isdigit():
            continue  # 玩家ID 目录必须为数字
        target = os.path.normpath(os.path.join(root, rel))
        if not target.startswith(root + os.sep):
            continue  # 防路径穿越
        info = z.getinfo(n)
        try:
            free = shutil.disk_usage(root).free
        except OSError:
            free = 0
        if written + info.file_size > free - SAFETY_MARGIN:
            truncated = True
            done += 1
            if progress_callback:
                progress_callback(done, total, n, info.file_size)
            break  # 磁盘余量不足, 停止恢复
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            with z.open(n) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
        except Exception:  # noqa: BLE001 单个文件损坏/写失败: 跳过, 不影响其余
            failed += 1
            done += 1
            if progress_callback:
                progress_callback(done, total, n, info.file_size)
            continue
        restored += 1
        written += info.file_size
        done += 1
        if progress_callback:
            progress_callback(done, total, n, info.file_size)
    return restored, truncated, failed, done


def import_zip(zip_path: str, store: BlacklistStore, mode: str = "append",
               restore_evidence: bool = True, progress_callback=None) -> dict:
    """从 zip 包导入黑名单条目。

    mode: "append" 追加全部 / "pid" 按玩家ID查重(已有相同玩家ID则跳过)。
    restore_evidence: 是否把文件中的证据文件恢复到本地证据目录。
    progress_callback(done, total, name, size_bytes) 用于进度显示。
    返回统计 dict: imported 导入条目数, new_ids 新增玩家ID数,
    has_evidence 文件是否含证据, evidence_restored 恢复的证据文件数, size 文件大小。
    """
    size = os.path.getsize(zip_path)
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        if ENTRIES_NAME not in names:
            raise ValueError("文件中没有 entries.json, 不是有效的黑名单导出包")
        try:
            info = z.getinfo(ENTRIES_NAME)
            if info.file_size > MAX_ENTRIES_JSON:
                raise ValueError("entries.json 过大, 已取消")
            raw = z.read(ENTRIES_NAME).decode("utf-8")
            data = json.loads(raw)
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"读取 entries.json 失败: {exc}") from exc
        evidence_names = [n for n in names if n.startswith(EVIDENCE_PREFIX)]

    entries = _parse_entries(data)
    existing_pids = {(e.player_id or "").strip() for e in store.entries}
    total = len(entries) + len(evidence_names)
    done = 0

    imported = 0
    new_ids = 0
    for e in entries:
        pid = (e.player_id or "").strip()
        if mode == "pid":
            # 玩家ID导入: 已有相同玩家ID则跳过
            if pid and pid in existing_pids:
                done += 1
                continue
            if pid:
                existing_pids.add(pid)
                new_ids += 1
        else:
            # 追加模式: 直接全部追加
            if pid and pid not in existing_pids:
                new_ids += 1
                existing_pids.add(pid)
        store.entries.append(e)
        imported += 1
        done += 1
        if progress_callback:
            name = (e.nickname or e.player_id or "条目").strip()
            progress_callback(done, total, f"条目: {name}", 0)
    store.save()

    restored = 0
    truncated = False
    failed = 0
    if evidence_names and restore_evidence:
        with zipfile.ZipFile(zip_path) as z:
            restored, truncated, failed, done = _restore_evidence(
                z, evidence_names, done=done, total=total,
                progress_callback=progress_callback,
            )

    return {
        "imported": imported,
        "new_ids": new_ids,
        "has_evidence": bool(evidence_names),
        "evidence_restored": restored,
        "evidence_truncated": truncated,
        "evidence_failed": failed,
        "size": size,
    }
