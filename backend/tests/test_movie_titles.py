"""Ticket presentation metadata must not change movie/sequel identity."""
import pytest

from backend.movie_titles import clean_movie_title


@pytest.mark.parametrize("title,expected", [
    ("奥德赛 (2D)", "奥德赛"),
    ("奥德赛（国语 3D）", "奥德赛"),
    ("奥德赛 (2D)（英文版）", "奥德赛"),
    ("奥德赛 2D 中文版", "奥德赛"),
    ("奥德赛 - 英语版", "奥德赛"),
    ("奥德赛/中文配音", "奥德赛"),
    ("流浪地球2（普通话版）", "流浪地球2"),
    ("Example Part II (英语 ２Ｄ)", "Example Part II"),
    ("Example (1999)（2D 中文版）", "Example (1999)"),
    ("Example (2D) (1999)", "Example (1999)"),
    ("Example (1999 film) 3D", "Example (1999 film)"),
    ("电影第二部（中文版）", "电影第二部"),
    ("电影第三部 英文版", "电影第三部"),
    ("电影第四部 (3D)", "电影第四部"),
    ("电影 4 (2D)", "电影 4"),
    ("(2D)", ""),
    ("", ""),
])
def test_explicit_presentation_suffixes_are_removed_and_cleanup_is_idempotent(title, expected):
    assert clean_movie_title(title) == expected
    assert clean_movie_title(expected) == expected


@pytest.mark.parametrize("title", [
    "英语课", "中文老师", "Johnny English", "国语", "英语", "第二部",
    "Example Part II", "Example2", "Example 2D Adventure", "Example (Part 2)",
    "Example（第二部 英语课）", "Example（2D影厅）", "Example（2DX）",
    "Example英语", "Example (1999)",
])
def test_real_names_unknown_blocks_and_unseparated_words_are_preserved(title):
    assert clean_movie_title(title) == title
