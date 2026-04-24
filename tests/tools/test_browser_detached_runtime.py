import json
import re
from pathlib import Path
from unittest.mock import patch

import tools.browser_tool as browser_tool


def test_run_browser_command_uses_detached_unit_for_live_local_session(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_SESSION_PLATFORM', 'telegram')
    monkeypatch.setattr(browser_tool, '_find_agent_browser', lambda: '/opt/agent-browser')
    monkeypatch.setattr(browser_tool, '_get_session_info', lambda task_id: {
        'session_name': 'sess123',
        'bb_session_id': None,
        'cdp_url': None,
        'features': {'local': True},
    })
    monkeypatch.setattr(browser_tool, '_socket_safe_tmpdir', lambda: str(tmp_path))
    monkeypatch.setattr(browser_tool, '_discover_homebrew_node_dirs', lambda: [])

    seen = {}

    def fake_launch_transient_unit(**kwargs):
        seen.update(kwargs)
        socket_dir = kwargs['extra_env']['AGENT_BROWSER_SOCKET_DIR']
        Path(socket_dir).mkdir(parents=True, exist_ok=True)
        Path(socket_dir, '_stdout_open').write_text(json.dumps({
            'success': True,
            'data': {'url': 'https://example.com/'},
            'error': None,
        }))
        Path(socket_dir, '_stderr_open').write_text('')
        match = re.search(r'_exit_detached_[A-Za-z0-9]+', kwargs['shell_script'])
        assert match is not None
        Path(socket_dir, match.group(0)).write_text('0')
        return 'hermes-browser-unit'

    with patch('tools.detached_runtime.transient_unit_launcher_available', return_value=True),          patch('tools.detached_runtime.launch_transient_unit', side_effect=fake_launch_transient_unit),          patch('tools.browser_tool.subprocess.Popen') as mock_popen:
        result = browser_tool._run_browser_command('task-1', 'open', ['https://example.com'], timeout=5)

    assert result['success'] is True
    assert result['data']['url'] == 'https://example.com/'
    assert seen['extra_properties'] == ['ExitType=cgroup']
    assert seen['extra_env']['AGENT_BROWSER_SOCKET_DIR'].startswith(str(tmp_path))
    assert '/opt/agent-browser' in seen['shell_script']
    mock_popen.assert_not_called()


def test_run_browser_command_timeout_signals_detached_unit(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_SESSION_PLATFORM', 'telegram')
    monkeypatch.setattr(browser_tool, '_find_agent_browser', lambda: '/opt/agent-browser')
    monkeypatch.setattr(browser_tool, '_get_session_info', lambda task_id: {
        'session_name': 'sess456',
        'bb_session_id': None,
        'cdp_url': None,
        'features': {'local': True},
    })
    monkeypatch.setattr(browser_tool, '_socket_safe_tmpdir', lambda: str(tmp_path))
    monkeypatch.setattr(browser_tool, '_discover_homebrew_node_dirs', lambda: [])

    with patch('tools.detached_runtime.transient_unit_launcher_available', return_value=True),          patch('tools.detached_runtime.launch_transient_unit', return_value='hermes-browser-timeout'),          patch('tools.detached_runtime.signal_unit') as mock_signal:
        result = browser_tool._run_browser_command('task-2', 'open', ['https://example.com'], timeout=1)

    assert result['success'] is False
    assert 'timed out' in result['error']
    assert mock_signal.call_args_list[0].args == ('hermes-browser-timeout', 'TERM')
    assert mock_signal.call_args_list[1].args == ('hermes-browser-timeout', 'KILL')
