import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from livestream import recorder as module


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.user_dir = self.root / "123"
        self.user_dir.mkdir()
        self.manifest = self.user_dir / "capture.recording.json"
        self.manifest.write_text(json.dumps({
            "user_id": 123, "channel_id": -10042, "channel_title": "capture",
            "started_at": "2026-11-02T17:00:00", "recording_id": 42,
        }))
        self.raw = self.user_dir / "capture.e0.audio.mka"
        self.raw.write_bytes(b"empty container")
        self.patch_output = patch.object(module, "OUTPUT_DIR", str(self.root))
        self.patch_output.start()
        self.addCleanup(self.patch_output.stop)

    async def test_failed_recovery_survives_restart_and_retries_changed_media(self):
        with patch.object(module.MuxerManager, "recover_parts", return_value=[]) as recover, \
                patch.object(module, "valid_media", return_value=False):
            await module.StreamRecorder().recover_incomplete()
            await module.StreamRecorder().recover_incomplete()
            self.assertEqual(recover.call_count, 1)
            self.assertEqual(self.raw.read_bytes(), b"empty container")
            self.assertEqual(json.loads(self.manifest.read_text())["recording_id"], 42)
            self.raw.write_bytes(b"repaired container with media")
            await module.StreamRecorder().recover_incomplete()
            self.assertEqual(recover.call_count, 2)

    async def test_new_output_after_failed_recovery_is_delivered(self):
        with patch.object(module.MuxerManager, "recover_parts", return_value=[]), \
                patch.object(module, "valid_media", return_value=False):
            await module.StreamRecorder().recover_incomplete()

        output = self.user_dir / "capture.m4a"
        output.write_bytes(b"recovered audio")
        recorder = module.StreamRecorder()
        recorder.set_completion_callback(AsyncMock())
        with patch.object(module.MuxerManager, "recover_parts", return_value=[]), \
                patch.object(module, "valid_media", side_effect=lambda path: path == str(output)), \
                patch.object(module.db, "finish_recording"):
            await recorder.recover_incomplete()
        recorder._completion_callback.assert_awaited_once()
        session = recorder._completion_callback.await_args.args[0]
        self.assertEqual(session.archive_files, [str(output)])
        self.assertTrue(self.raw.exists())
        self.assertTrue(self.manifest.exists())


if __name__ == "__main__":
    unittest.main()
