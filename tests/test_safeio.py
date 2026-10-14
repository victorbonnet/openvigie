import json
import os
import sqlite3
from pathlib import Path

import pytest

from openvigie.safeio import (
    FileCursor,
    ReadStats,
    UnsafeFileError,
    iter_jsonl,
    load_json_bounded,
    load_toml_bounded,
    open_sqlite_readonly,
    read_text_bounded,
    resolve_within,
    tail_jsonl,
    walk_bounded,
)


def jl(*objs: object) -> str:
    return "".join(json.dumps(o) + "\n" for o in objs)


def test_read_text_bounded_limits_size(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"x" * 100)
    assert read_text_bounded(path, max_bytes=100) == "x" * 100
    with pytest.raises(UnsafeFileError, match="larger"):
        read_text_bounded(path, max_bytes=99)


def test_invalid_utf8_is_replaced(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"ok \xff\xfe end")
    assert read_text_bounded(path) == "ok �� end"


def test_symlink_refused(tmp_path: Path) -> None:
    link = tmp_path / "passwd"
    link.symlink_to("/etc/passwd")
    with pytest.raises(UnsafeFileError, match="symlink"):
        read_text_bounded(link)


def test_symlink_loop_refused(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.symlink_to(b)
    b.symlink_to(a)
    with pytest.raises(OSError):
        read_text_bounded(a)


def test_fifo_refused_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(UnsafeFileError, match="not a regular file"):
        read_text_bounded(fifo)


def test_directory_refused(tmp_path: Path) -> None:
    with pytest.raises((UnsafeFileError, IsADirectoryError)):
        read_text_bounded(tmp_path)


def test_json_and_toml(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text('{"x": 1}')
    (tmp_path / "a.toml").write_text('model = "o3"\n')
    (tmp_path / "bad.json").write_text("{nope")
    assert load_json_bounded(tmp_path / "a.json") == {"x": 1}
    assert load_toml_bounded(tmp_path / "a.toml") == {"model": "o3"}
    with pytest.raises(ValueError):
        load_json_bounded(tmp_path / "bad.json")


def test_iter_jsonl_skips_and_counts_bad_lines(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    big = json.dumps({"blob": "x" * 5000})
    path.write_bytes(
        (jl({"a": 1}) + "not json\n" + big + "\n\n" + jl({"b": 2})).encode() + b"\xff\xfe\n"
    )
    stats = ReadStats()
    values = list(iter_jsonl(path, max_line_bytes=1000, stats=stats))
    assert values == [{"a": 1}, {"b": 2}]
    assert stats.oversized == 1
    assert stats.parse_errors == 2
    assert list(iter_jsonl(path, max_lines=1)) == [{"a": 1}]


def test_iter_jsonl_huge_single_line(tmp_path: Path) -> None:
    path = tmp_path / "huge.jsonl"
    with path.open("wb") as fh:
        fh.write(b'{"x":"' + b"y" * (50 * 1024 * 1024) + b'"}\n')
        fh.write(jl({"ok": True}).encode())
    stats = ReadStats()
    assert list(iter_jsonl(path, stats=stats)) == [{"ok": True}]
    assert stats.oversized == 1


def test_tail_reads_only_appended_complete_lines(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text(jl({"n": 1}) + '{"n": 2')
    first = tail_jsonl(path, None)
    assert first.values == [{"n": 1}]
    with path.open("a") as fh:
        fh.write("}\n" + jl({"n": 3}))
    second = tail_jsonl(path, first.cursor)
    assert second.values == [{"n": 2}, {"n": 3}]
    third = tail_jsonl(path, second.cursor)
    assert third.values == [] and third.cursor == second.cursor


def test_tail_detects_truncation(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text(jl({"n": 1}, {"n": 2}))
    cursor = tail_jsonl(path, None).cursor
    path.write_text(jl({"n": 9}))  # truncated and rewritten, same inode
    result = tail_jsonl(path, cursor)
    assert result.reset is True
    assert result.values == [{"n": 9}]


def test_tail_detects_rotation(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text(jl({"n": 1}))
    cursor = tail_jsonl(path, None).cursor
    path.rename(tmp_path / "old.jsonl")
    path.write_text(jl({"n": 1}, {"n": 2}, {"n": 3}))
    result = tail_jsonl(path, cursor)
    assert result.reset is True
    assert [v["n"] for v in result.values] == [1, 2, 3]


def test_tail_budget_spreads_backlog_over_calls(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text(jl(*({"n": i} for i in range(1000))))
    cursor: FileCursor | None = None
    seen: list[int] = []
    calls = 0
    while True:
        result = tail_jsonl(path, cursor, budget=2048, max_line_bytes=512)
        seen.extend(v["n"] for v in result.values)
        cursor, calls = result.cursor, calls + 1
        if not result.more:
            break
    assert seen == list(range(1000))
    assert calls > 1


def test_tail_skips_oversized_line_across_calls(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text(jl({"n": 1}) + '{"blob": "' + "x" * 10_000 + '"}\n' + jl({"n": 2}))
    cursor: FileCursor | None = None
    values: list[object] = []
    oversized = 0
    for _ in range(20):
        result = tail_jsonl(path, cursor, budget=1024, max_line_bytes=512)
        values += result.values
        oversized += result.stats.oversized
        cursor = result.cursor
    assert values == [{"n": 1}, {"n": 2}]
    assert oversized == 1


def test_cursor_round_trip() -> None:
    cursor = FileCursor(1, 2, 3, True)
    assert FileCursor.from_dict(cursor.to_dict()) == cursor


def test_sqlite_readonly(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        conn.execute("create table t (x int)")
        conn.execute("insert into t values (1)")
    ro = open_sqlite_readonly(db)
    try:
        assert ro.execute("select x from t").fetchall() == [(1,)]
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("insert into t values (2)")
    finally:
        ro.close()


def test_resolve_within(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "escape").symlink_to("/etc")
    assert resolve_within(root, "sub") == (root / "sub").resolve()
    for bad in ["../x", "/etc/passwd", "escape/passwd"]:
        with pytest.raises(UnsafeFileError):
            resolve_within(root, bad)


def test_walk_bounded(tmp_path: Path) -> None:
    (tmp_path / "a" / "b" / "c").mkdir(parents=True)
    (tmp_path / "a" / "1.jsonl").write_text("")
    (tmp_path / "a" / "b" / "2.jsonl").write_text("")
    (tmp_path / "a" / "b" / "c" / "3.jsonl").write_text("")
    (tmp_path / "a" / "x.txt").write_text("")
    (tmp_path / "a" / "link.jsonl").symlink_to(tmp_path / "a" / "1.jsonl")
    (tmp_path / "a" / "loop").symlink_to(tmp_path)
    names = [p.name for p in walk_bounded(tmp_path, "*.jsonl")]
    assert names == ["1.jsonl", "2.jsonl", "3.jsonl"]
    assert [p.name for p in walk_bounded(tmp_path, "*.jsonl", max_depth=2)] == [
        "1.jsonl",
        "2.jsonl",
    ]
    assert len(list(walk_bounded(tmp_path, "*", max_entries=2))) <= 2
