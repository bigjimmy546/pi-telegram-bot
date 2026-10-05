"""Deterministic JSONL child process used by test_pi_protocol."""

from __future__ import annotations

import json
import os
import sys
import time


def emit(value: object) -> None:
    sys.stdout.buffer.write(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    sys.stdout.buffer.flush()


def emit_split(value: object) -> None:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    ) + b"\n"
    split_at = payload.index("雪".encode("utf-8")) + 1
    sys.stdout.buffer.write(payload[:split_at])
    sys.stdout.buffer.flush()
    time.sleep(0.01)
    sys.stdout.buffer.write(payload[split_at:])
    sys.stdout.buffer.flush()


def read_request() -> dict[str, object] | None:
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    return json.loads(line)


def response(request: dict[str, object]) -> dict[str, object]:
    return {
        "type": "response",
        "id": request.get("id"),
        "command": request.get("type"),
        "success": True,
        "data": {"command": request.get("type")},
    }


def run(scenario: str) -> None:
    if scenario == "invalid-utf8":
        sys.stdout.buffer.write(b"\xff\n")
        sys.stdout.buffer.flush()
        return
    if scenario == "invalid-json":
        sys.stdout.buffer.write(b"{not-json}\n")
        sys.stdout.buffer.flush()
        return
    if scenario == "invalid-shape":
        emit(["response", "not", "an", "object"])
        return
    if scenario == "invalid-constant":
        sys.stdout.buffer.write(b'{"type":"event","value":NaN}\n')
        sys.stdout.buffer.flush()
        return
    if scenario == "overlong-stdout":
        sys.stdout.buffer.write(b"x" * (16 * 1024 * 1024))
        sys.stdout.buffer.flush()
        time.sleep(30)
        return
    if scenario == "hang-with-secret" or scenario == "bounded-stderr":
        size = 1024 * 1024 if scenario == "bounded-stderr" else 128
        sys.stderr.write("token-sentinel " + ("diagnostic " * (size // 12)) + "\n")
        sys.stderr.flush()
        if scenario == "hang-with-secret":
            while True:
                time.sleep(1)
    if scenario == "out-of-order":
        first = read_request()
        second = read_request()
        if first is None or second is None:
            return
        emit_split(
            {
                "type": "event",
                "event": "agent_start",
                "text": "snow 雪 and separators \u2028 and \u2029",
            }
        )
        emit(response(second))
        emit(response(first))
        emit({"type": "event", "event": "agent_end", "text": "after responses"})
        for _line in sys.stdin.buffer:
            pass
        return
    if scenario == "events-around-response":
        request = read_request()
        if request is not None:
            emit({"type": "event", "event": "agent_start", "text": "before"})
            emit(response(request))
            emit({"type": "event", "event": "agent_end", "text": "after"})
        for _line in sys.stdin.buffer:
            pass
        return
    if scenario == "extension-ui":
        request = read_request()
        if request is not None:
            emit(
                {
                    "type": "extension_ui_request",
                    "id": "dialog-1",
                    "method": "select",
                    "title": "Choose one",
                    "options": ["one", "two"],
                }
            )
            emit(response(request))
        for _line in sys.stdin.buffer:
            pass
        return
    if scenario == "cancel-dialog":
        cancelled = read_request()
        if cancelled != {
            "type": "extension_ui_response",
            "id": "dialog-1",
            "cancelled": True,
        }:
            return
        next_request = read_request()
        if next_request is not None:
            emit(response(next_request))
        for _line in sys.stdin.buffer:
            pass
        return
    if scenario == "answer-dialogs":
        selected = read_request()
        confirmed = read_request()
        if selected != {
            "type": "extension_ui_response",
            "id": "select-1",
            "value": "Allow",
        }:
            return
        if confirmed != {
            "type": "extension_ui_response",
            "id": "confirm-1",
            "confirmed": False,
        }:
            return
        next_request = read_request()
        if next_request is not None:
            emit(response(next_request))
        for _line in sys.stdin.buffer:
            pass
        return
    if scenario == "unexpected-eof":
        read_request()
        read_request()
        return
    if scenario == "mismatched-command":
        request = read_request()
        if request is not None:
            record = response(request)
            record["command"] = "get_commands"
            emit(record)
        return
    if scenario == "late-after-timeout":
        first = read_request()
        if first is None:
            return
        time.sleep(0.05)
        emit(response(first))
        second = read_request()
        if second is not None:
            emit(response(second))
        for _line in sys.stdin.buffer:
            pass
        return

    while True:
        request = read_request()
        if request is None:
            return
        if scenario == "close" and request.get("type") == "exit":
            return
        record = response(request)
        if scenario == "child-environment":
            record["data"] = {
                "telegram_token_present": "TELEGRAM_BOT_TOKEN" in os.environ,
                "artifact_capability": os.environ.get("TELEGRAM_PI_ARTIFACT_CAPABILITY"),
            }
        emit(record)


if __name__ == "__main__":
    run(sys.argv[1])
