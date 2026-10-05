import dataclasses
import unittest

from telegram_pi_bot.model import (
    ArtifactReceipt,
    ArtifactRequest,
    BotState,
    ConversationAction,
    Effect,
    ModelRef,
    NativeSession,
    NativeSessionRef,
    PendingSession,
    RuntimeEvent,
    RuntimeSnapshot,
    SessionConfig,
    SessionConfigChange,
    SessionRef,
    SkillRef,
    Transition,
    TurnContent,
    TurnRequest,
    TurnResult,
    TurnStatus,
    UiRequest,
    UiResponse,
)


class ModelTests(unittest.TestCase):
    def test_bot_state_requires_explicit_positive_chat_id(self):
        with self.assertRaises(TypeError):
            BotState(version=0)
        self.assertEqual(BotState(version=0, chat_id=123456789).chat_id, 123456789)

    def test_model_values_are_frozen_and_validate_required_ids(self):
        model = ModelRef("ollama", "qwen3.8-orcarouter:latest")
        session_ref = SessionRef("pending", "12345678-1234-5678-1234-567812345678")
        pending = PendingSession(session_ref, None, SessionConfig(model, "medium"), 1)
        native_ref = NativeSessionRef("0123456789abcdef0123456789abcdef")
        native = NativeSession(native_ref, "work", 2, 3, SessionConfig(model, "medium"))

        self.assertEqual(pending.ref.kind, "pending")
        self.assertEqual(native.ref.id, native_ref.id)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            model.provider = "antigravity"
        for args in (("", "model"), ("ollama", "")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                ModelRef(*args)
        with self.assertRaises(ValueError):
            NativeSessionRef("")
        with self.assertRaises(ValueError):
            PendingSession(SessionRef("native", "id"), None, pending.config, 1)

    def test_nested_sequences_and_mappings_are_immutable(self):
        model = ModelRef("ollama", "model", "ollama_cloud", ["text", "image"])
        snapshot_models = [model]
        stats = {"tokens": 12}
        snapshot = RuntimeSnapshot(
            tuple(snapshot_models), ("low", "medium"), (SkillRef("review"),), stats
        )
        snapshot_models.append(ModelRef("ollama", "other"))
        stats["tokens"] = 99
        self.assertEqual(len(snapshot.models), 1)
        self.assertEqual(snapshot.session_stats["tokens"], 12)
        self.assertEqual(snapshot.models[0].location, "ollama_cloud")
        self.assertEqual(snapshot.models[0].capabilities, ("text", "image"))
        self.assertEqual(snapshot.skills[0].name, "review")
        with self.assertRaises(TypeError):
            snapshot.session_stats["tokens"] = 0

        content_items = ["photo-1"]
        content = TurnContent("hello", content_items)
        content_items.append("photo-2")
        self.assertEqual(content.attachments, ("photo-1",))

    def test_turn_results_cover_all_terminal_statuses(self):
        statuses = (
            "rejected",
            "completed",
            "handled",
            "aborted",
            "failed",
            "uncertain",
        )
        for status in statuses:
            with self.subTest(status=status):
                result = TurnResult(TurnStatus(status))
                self.assertEqual(result.status.value, status)
                self.assertEqual(result.retryable, status == "rejected")

    def test_all_contracts_are_constructible_without_raw_protocol_objects(self):
        model = ModelRef("ollama", "model", "ollama_cloud", ["text", "image"])
        ref = SessionRef("pending", "id")
        config = SessionConfig(model, "medium")
        pending = PendingSession(ref, None, config, 1)
        content = TurnContent("hello")
        values = (
            SessionConfigChange(name="work", thinking="high"),
            SkillRef("review"),
            TurnRequest(pending, content),
            RuntimeEvent("progress", text="working"),
            TurnResult(TurnStatus.COMPLETED, text="done"),
            UiRequest("request", "confirm", "Continue?"),
            UiResponse("yes"),
            ArtifactRequest("/home/alice/output.txt", "file"),
            ArtifactReceipt("artifact", "output.txt", "file", 4),
            ConversationAction("input", text="hello"),
            Effect("dispatch", "turn"),
            BotState(0, chat_id=123456789),
            Transition(BotState(0, chat_id=123456789), (Effect("dispatch", "turn"),)),
            NativeSession(NativeSessionRef("native"), None, 2, 3, config),
        )
        self.assertEqual(len(values), 14)


if __name__ == "__main__":
    unittest.main()
