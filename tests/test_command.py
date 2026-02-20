"""Tests for the command parser module."""

from ncat.command import parse_command, parse_new_dir


def test_parse_command_new() -> None:
    assert parse_command("/new") == "new"
    assert parse_command("/NEW") == "new"
    assert parse_command("/new extra args") == "new"


def test_parse_command_stop() -> None:
    assert parse_command("/stop") == "stop"
    assert parse_command("/STOP") == "stop"
    assert parse_command("/stop now") == "stop"


def test_parse_command_help() -> None:
    assert parse_command("/help") == "help"


def test_parse_command_unknown() -> None:
    assert parse_command("/foo") == "unknown"
    assert parse_command("/") == "unknown"


def test_parse_command_not_command() -> None:
    assert parse_command("hello") is None
    assert parse_command("not a /command") is None
    assert parse_command("") is None


def test_parse_new_dir_no_dir() -> None:
    assert parse_new_dir("/new") is None
    assert parse_new_dir("/NEW") is None
    assert parse_new_dir("/new   ") is None
    assert parse_new_dir("/new  \t  ") is None


def test_parse_new_dir_with_dir() -> None:
    assert parse_new_dir("/new projectA") == "projectA"
    assert parse_new_dir("/new a/b") == "a/b"
    assert parse_new_dir("/new  projectA") == "projectA"
    assert parse_new_dir("/new projectA extra") == "projectA extra"


def test_parse_new_dir_not_new() -> None:
    assert parse_new_dir("/stop") is None
    assert parse_new_dir("hello") is None
    assert parse_new_dir("") is None
