import unittest
from pathlib import Path

from tests.fakes import (
    extension_command,
    model,
    runtime_with_commands,
    runtime_with_models,
    skill_command,
)


class PiRuntimeMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_policy_admits_all_ollama_and_only_allowed_antigravity(self):
        runtime, fake = runtime_with_models(
            [
                model("ollama", "qwen3.8-orcarouter:latest", ["text", "image"]),
                model("ollama", "glm-5.3:cloud", ["text"]),
                model("antigravity", "gemini-3.7-flash", ["text", "image"]),
                model("antigravity", "claude-opus-4-6", ["text"]),
                model("openrouter", "openai/gpt-6.1-sol", ["text"]),
            ]
        )
        snapshot = await runtime.inspect(None)
        self.assertEqual(
            [(item.provider, item.model_id, item.location) for item in snapshot.models],
            [
                ("ollama", "qwen3.8-orcarouter:latest", "local"),
                ("ollama", "glm-5.3:cloud", "ollama_cloud"),
                ("antigravity", "gemini-3.7-flash", "remote"),
            ],
        )
        self.assertEqual(fake.prompt_count, 0)
        self.assertNotIn("ollama_list", [command["type"] for command in fake.commands])
        self.assertEqual(fake.cwd, [Path("/home/alice")])
        self.assertIn("--no-session", fake.argv[0])
        self.assertIn("qwen3.8-orcarouter:latest", fake.argv[0])

    async def test_skill_catalog_uses_exact_source_and_name_without_prompt(self):
        runtime, fake = runtime_with_commands(
            [
                skill_command("advisor", "Research skill"),
                extension_command("mcp", "Extension command"),
                skill_command("advisory", "Different exact name"),
            ]
        )
        snapshot = await runtime.inspect(None)
        self.assertEqual([skill.name for skill in snapshot.skills], ["advisor", "advisory"])
        self.assertEqual((await runtime.resolve_skill("advisor")).name, "advisor")
        with self.assertRaises((ValueError, LookupError)):
            await runtime.resolve_skill("adv")
        self.assertEqual(fake.prompt_count, 0)
        self.assertNotIn("prompt", [command["type"] for command in fake.commands])

    async def test_new_pending_session_uses_configured_pi_defaults_without_starting_pi(self):
        runtime, fake = runtime_with_models(
            [model("antigravity", "gemini-3.7-flash")],
            global_default=("openrouter", "unrelated-global-default"),
            default_model=("antigravity", "gemini-3.7-flash"),
            default_thinking="high",
        )
        pending = runtime.new_pending_session("review")
        self.assertEqual(pending.name, "review")
        self.assertEqual(
            (pending.config.model.provider, pending.config.model.model_id),
            ("antigravity", "gemini-3.7-flash"),
        )
        self.assertTrue(pending.ref.id)
        self.assertEqual(pending.config.thinking, "high")
        self.assertEqual(fake.processes, [])

    async def test_no_session_metadata_uses_configured_pi_thinking_default(self):
        runtime, fake = runtime_with_models(
            [model("antigravity", "gemini-3.7-flash")],
            default_model=("antigravity", "gemini-3.7-flash"),
            default_thinking="high",
        )

        await runtime.inspect(None)

        self.assertIn("--thinking", fake.argv[0])
        self.assertEqual(fake.argv[0][fake.argv[0].index("--thinking") + 1], "high")


if __name__ == "__main__":
    unittest.main()
