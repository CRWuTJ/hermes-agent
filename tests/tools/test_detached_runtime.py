import subprocess
import unittest
from unittest.mock import patch, MagicMock


class TestDetachedRuntime(unittest.TestCase):
    def test_launch_transient_unit_builds_expected_systemd_run_command(self):
        from tools.detached_runtime import launch_transient_unit

        completed = MagicMock()
        completed.stdout = ''
        completed.returncode = 0

        with patch('tools.detached_runtime.shutil.which', return_value='/usr/bin/systemd-run'),              patch('tools.detached_runtime.os.path.exists', return_value=True),              patch('tools.detached_runtime.subprocess.run', return_value=completed) as mock_run,              patch.dict('tools.detached_runtime.os.environ', {
                 'WSL_DISTRO_NAME': 'Ubuntu',
                 'USER': 'wutj',
                 'LOGNAME': 'wutj',
                 'PATH': '/usr/bin:/bin',
                 'VIRTUAL_ENV': '/tmp/venv',
             }, clear=False):
            unit_name = launch_transient_unit(
                unit_prefix='hermes-test',
                cwd='/tmp/work',
                shell_script='echo ok',
                extra_env={'FOO': 'bar'},
                extra_properties=['ExitType=cgroup'],
            )

        self.assertTrue(unit_name.startswith('hermes-test-'))
        cmd = mock_run.call_args.args[0]
        self.assertIn('--quiet', cmd)
        self.assertIn('--property=Slice=hermes-worker.slice', cmd)
        self.assertIn('--property=User=wutj', cmd)
        self.assertIn('--property=Group=wutj', cmd)
        self.assertIn('--setenv=PATH=/usr/bin:/bin', cmd)
        self.assertIn('--setenv=VIRTUAL_ENV=/tmp/venv', cmd)
        self.assertIn('--setenv=FOO=bar', cmd)
        self.assertIn('--property=ExitType=cgroup', cmd)
        self.assertIn('--working-directory=/tmp/work', cmd)
        self.assertEqual(cmd[-3:], ['/bin/bash', '-lc', 'echo ok'])

    def test_build_piped_transient_unit_command_returns_expected_wrapper(self):
        from tools.detached_runtime import build_piped_transient_unit_command

        def _which(name, path=None):
            if name == 'systemd-run':
                return '/usr/bin/systemd-run'
            if name == 'websearch':
                return '/opt/websearch/bin/websearch'
            return None

        with patch('tools.detached_runtime.shutil.which', side_effect=_which),              patch('tools.detached_runtime.os.path.exists', return_value=True),              patch.dict('tools.detached_runtime.os.environ', {
                 'WSL_DISTRO_NAME': 'Ubuntu',
                 'USER': 'wutj',
                 'LOGNAME': 'wutj',
                 'PATH': '/usr/bin:/bin',
                 'VIRTUAL_ENV': '/tmp/venv',
             }, clear=False):
            cmd, unit_name = build_piped_transient_unit_command(
                unit_prefix='hermes-mcp',
                cwd='/tmp/work',
                argv=['websearch', '--stdio'],
                extra_env={'FOO': 'bar'},
            )

        self.assertTrue(unit_name.startswith('hermes-mcp-'))
        self.assertIn('--pipe', cmd)
        self.assertIn('--quiet', cmd)
        self.assertIn('--collect', cmd)
        self.assertIn('--working-directory=/tmp/work', cmd)
        self.assertIn('--setenv=FOO=bar', cmd)
        self.assertEqual(cmd[-2:], ['/opt/websearch/bin/websearch', '--stdio'])

    def test_popen_transient_unit_builds_expected_pipe_command(self):
        from tools.detached_runtime import popen_transient_unit

        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.stderr = MagicMock()

        def _which(name, path=None):
            if name == 'systemd-run':
                return '/usr/bin/systemd-run'
            if name == 'copilot':
                return '/opt/copilot/bin/copilot'
            return None

        with patch('tools.detached_runtime.shutil.which', side_effect=_which),              patch('tools.detached_runtime.os.path.exists', return_value=True),              patch('tools.detached_runtime.subprocess.Popen', return_value=proc) as mock_popen,              patch.dict('tools.detached_runtime.os.environ', {
                 'WSL_DISTRO_NAME': 'Ubuntu',
                 'USER': 'wutj',
                 'LOGNAME': 'wutj',
                 'PATH': '/usr/bin:/bin',
                 'VIRTUAL_ENV': '/tmp/venv',
             }, clear=False):
            started_proc, unit_name = popen_transient_unit(
                unit_prefix='hermes-acp',
                cwd='/tmp/work',
                argv=['copilot', '--acp', '--stdio'],
                extra_env={'FOO': 'bar'},
            )

        self.assertIs(started_proc, proc)
        self.assertTrue(unit_name.startswith('hermes-acp-'))
        cmd = mock_popen.call_args.args[0]
        self.assertIn('--pipe', cmd)
        self.assertIn('--quiet', cmd)
        self.assertIn('--collect', cmd)
        self.assertIn('--property=Slice=hermes-worker.slice', cmd)
        self.assertIn('--working-directory=/tmp/work', cmd)
        self.assertIn('--setenv=FOO=bar', cmd)
        self.assertEqual(cmd[-3:], ['/opt/copilot/bin/copilot', '--acp', '--stdio'])
        self.assertEqual(mock_popen.call_args.kwargs['stdin'], subprocess.PIPE)
        self.assertEqual(mock_popen.call_args.kwargs['stdout'], subprocess.PIPE)
        self.assertEqual(mock_popen.call_args.kwargs['stderr'], subprocess.PIPE)
        self.assertEqual(mock_popen.call_args.kwargs['text'], True)
        self.assertEqual(mock_popen.call_args.kwargs['bufsize'], 1)

    def test_wait_for_main_pid_polls_until_pid(self):
        from tools.detached_runtime import wait_for_main_pid

        responses = [
            {'MainPID': '0'},
            {'MainPID': ''},
            {'MainPID': '4321'},
        ]

        with patch('tools.detached_runtime.systemctl_show_properties', side_effect=responses) as mock_show,              patch('tools.detached_runtime.time.sleep', return_value=None):
            pid = wait_for_main_pid('hermes-test-123', retries=3, delay=0)

        self.assertEqual(pid, 4321)
        self.assertEqual(mock_show.call_count, 3)


if __name__ == '__main__':
    unittest.main()
