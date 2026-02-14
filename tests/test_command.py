"""Tests for the command parser module."""

from ncat.command import parse_command


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
