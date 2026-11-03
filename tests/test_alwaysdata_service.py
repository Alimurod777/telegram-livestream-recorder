import asyncio
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch


class ShutdownSignalsTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_restart_signal_requests_graceful_shutdown(self):
        import bot
        loop = Mock()
        event = asyncio.Event()
        bot.install_shutdown_handlers(loop, event)
        handlers = {call.args[0]: call.args[1] for call in loop.add_signal_handler.call_args_list}
        expected = {signal.SIGINT, signal.SIGTERM}
        if hasattr(signal, 'SIGHUP'):
            expected.add(signal.SIGHUP)
        self.assertEqual(set(handlers), expected)
        for callback in handlers.values():
            event.clear()
            callback()
            self.assertTrue(event.is_set())

    async def test_platform_without_loop_signal_support_uses_signal_fallback(self):
        import bot
        loop = Mock()
        loop.add_signal_handler.side_effect = NotImplementedError()
        event = asyncio.Event()
        with patch.object(bot.signal, 'signal') as register:
            bot.install_shutdown_handlers(loop, event)
            self.assertEqual(register.call_count, loop.add_signal_handler.call_count)
            register.call_args.args[1](signal.SIGTERM, None)
            self.assertTrue(event.is_set())


@unittest.skipUnless(shutil.which('sh'), 'POSIX shell unavailable')
class ServiceLauncherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='recorder service ')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        directory = self.root / 'deploy' / 'alwaysdata'
        directory.mkdir(parents=True)
        self.launcher = directory / 'run_service.sh'
        shutil.copyfile(Path(__file__).resolve().parents[1] / 'deploy/alwaysdata/run_service.sh', self.launcher)

    def fake_python(self):
        path = self.root / 'venv/bin/python'
        path.parent.mkdir(parents=True)
        path.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$1" "$2" "$$"\numask\n')
        path.chmod(0o700)

    def test_foreground_exec_correct_directory_arguments_and_private_umask(self):
        self.fake_python()
        # This content must be read by dotenv, never executed by the shell.
        marker = self.root / 'should-not-exist'
        (self.root / '.env').write_text('DO_NOT_EXECUTE=$(touch "' + str(marker) + '")\n')
        process = subprocess.Popen(['sh', str(self.launcher)], cwd='/', stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        output, errors = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, errors)
        lines = output.splitlines()
        self.assertEqual(lines[:3], [str(self.root), '-u', str(self.root / 'bot.py')])
        self.assertEqual(int(lines[3]), process.pid)
        self.assertEqual(int(lines[4], 8), 0o77)
        self.assertFalse(marker.exists())

    def test_missing_virtualenv_fails_with_actionable_message(self):
        result = subprocess.run(['sh', str(self.launcher)], capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('create venv', result.stderr)

    def test_missing_env_fails_without_starting_python(self):
        self.fake_python()
        result = subprocess.run(['sh', str(self.launcher)], capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('copy .env.example', result.stderr)
        self.assertFalse(result.stdout)


if __name__ == '__main__':
    unittest.main()
