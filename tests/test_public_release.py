from __future__ import annotations

import os
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_FILES = (
    "src",
    "ops",
    "config",
    "tests",
    ".github",
    ".gitignore",
    "LICENSE",
    "README.md",
    "SPEC.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "docs/OPERATIONS.md",
    "docs/ACCEPTANCE.md",
    "pyproject.toml",
    "uv.lock",
)
FORBIDDEN_PATTERNS = (
    re.compile(r"BEGIN (?:RSA|OPENSSH|EC|DSA)? ?PRIVATE KEY"),
    re.compile(r"[0-9]{8,10}:[A-Za-z0-9_-]{30,}"),
    re.compile(r"gsk_[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
)


class PublicReleaseTests(unittest.TestCase):
    def test_exportable_tree_has_required_files_and_no_private_markers(self):
        for required in ("LICENSE", "SECURITY.md", "CONTRIBUTING.md"):
            self.assertTrue((ROOT / required).is_file(), required)
        extra_markers = [m for m in os.environ.get("TPB_TEST_PRIVATE_MARKERS", "").split(",") if m]
        for entry in PUBLIC_FILES:
            path = ROOT / entry
            paths = path.rglob("*") if path.is_dir() else (path,)
            for candidate in paths:
                if not candidate.is_file() or candidate.suffix in {".pyc"}:
                    continue
                text = candidate.read_text(encoding="utf-8")
                for pattern in FORBIDDEN_PATTERNS:
                    self.assertIsNone(
                        pattern.search(text),
                        f"Forbidden secret pattern matched in {candidate.relative_to(ROOT)}",
                    )
                for marker in extra_markers:
                    self.assertNotIn(marker, text, str(candidate.relative_to(ROOT)))


if __name__ == "__main__":
    unittest.main()
