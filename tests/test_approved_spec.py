"""QA-004: the reviewed snapshot must keep its exact approved bytes in Git."""
import hashlib
import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = "docs/specs/005-reader-relevance/"
EXPECTED_SHA256 = "7186F69E8D9CF1B3E1530267783B10D32D182FA25BAC624DE5960F25E50FA3D6"


class ApprovedSpecificationTests(unittest.TestCase):
    def verify(self, metadata, snapshot):
        self.assertEqual(metadata["utterance"], "утверждаю")
        self.assertEqual(metadata["snapshot"], "approved-spec.md")
        self.assertEqual(metadata["approved_spec_sha256"], EXPECTED_SHA256)
        self.assertEqual(hashlib.sha256(snapshot).hexdigest().upper(), EXPECTED_SHA256)
        self.assertEqual(set(metadata["scope"]), {"FIX-001", "FIX-007", "FIX-009", "FIX-008", "DIG-002", "UI-001", "QA-004"})
        for key in ("publishing_authorized", "deployment_authorized", "bulk_paid_reprocessing_authorized"):
            self.assertIs(metadata[key], False)

    def test_working_immutable_snapshot_matches_explicit_approval(self):
        metadata = json.loads((ROOT / DIRECTORY / "approval.json").read_text(encoding="utf-8-sig"))
        self.verify(metadata, (ROOT / DIRECTORY / "approved-spec.md").read_bytes())

    def test_staged_snapshot_matches_approval_before_commit_not_only_working_tree(self):
        def index_blob(name):
            return subprocess.run(["git", "-c", "safe.directory=" + ROOT.as_posix(), "show", ":" + DIRECTORY + name],
                                  cwd=ROOT, capture_output=True, check=True).stdout
        metadata = json.loads(index_blob("approval.json").decode("utf-8-sig"))
        self.verify(metadata, index_blob("approved-spec.md"))
