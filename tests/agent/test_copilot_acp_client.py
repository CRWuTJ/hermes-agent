import subprocess
from unittest.mock import MagicMock, patch

from agent.copilot_acp_client import CopilotACPClient


def test_spawn_prompt_process_uses_attached_worker_unit_in_live_context():
    client = CopilotACPClient(acp_command='copilot', acp_args=['--acp', '--stdio'], acp_cwd='/tmp')
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
    proc.stderr = MagicMock()

    with patch('tools.terminal_tool._has_live_or_origin_messaging_context', return_value=True),          patch('tools.detached_runtime.transient_unit_launcher_available', return_value=True),          patch('tools.detached_runtime.popen_transient_unit', return_value=(proc, 'hermes-acp-123')) as mock_launch:
        started_proc, unit_name = client._spawn_prompt_process()

    assert started_proc is proc
    assert unit_name == 'hermes-acp-123'
    mock_launch.assert_called_once_with(
        unit_prefix='hermes-acp',
        cwd='/tmp',
        argv=['copilot', '--acp', '--stdio'],
    )


def test_spawn_prompt_process_falls_back_to_plain_popen_outside_live_context():
    client = CopilotACPClient(acp_command='copilot', acp_args=['--acp', '--stdio'], acp_cwd='/tmp')
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
    proc.stderr = MagicMock()

    with patch('tools.terminal_tool._has_live_or_origin_messaging_context', return_value=False),          patch('tools.detached_runtime.transient_unit_launcher_available', return_value=True),          patch('agent.copilot_acp_client.subprocess.Popen', return_value=proc) as mock_popen:
        started_proc, unit_name = client._spawn_prompt_process()

    assert started_proc is proc
    assert unit_name is None
    mock_popen.assert_called_once_with(
        ['copilot', '--acp', '--stdio'],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd='/tmp',
    )


def test_close_signals_active_unit_before_terminating_process():
    client = CopilotACPClient(acp_command='copilot', acp_args=['--acp', '--stdio'], acp_cwd='/tmp')
    proc = MagicMock()
    client._active_process = proc
    client._active_unit_name = 'hermes-acp-123'

    with patch('tools.detached_runtime.signal_unit') as mock_signal:
        client.close()

    mock_signal.assert_called_once_with('hermes-acp-123', 'TERM')
    proc.terminate.assert_called_once_with()
    proc.wait.assert_called_once_with(timeout=2)
    assert client._active_process is None
    assert client._active_unit_name is None
    assert client.is_closed is True
