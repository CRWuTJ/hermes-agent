import signal
from types import SimpleNamespace

import gateway.run as gateway_run


def test_signal_process_tree_signals_descendants_before_root(monkeypatch):
    class FakeProcess:
        def __init__(self, pid):
            self.pid = pid

        def children(self, recursive=False):
            assert recursive is True
            return [SimpleNamespace(pid=101), SimpleNamespace(pid=202)]

    fake_psutil = SimpleNamespace(Process=lambda pid: FakeProcess(pid))
    kills = []

    monkeypatch.setitem(__import__('sys').modules, 'psutil', fake_psutil)
    monkeypatch.setattr(gateway_run.os, 'kill', lambda pid, sig: kills.append((pid, sig)))

    gateway_run._signal_process_tree(42, signal.SIGTERM, strict_root=True)

    assert kills == [
        (101, signal.SIGTERM),
        (202, signal.SIGTERM),
        (42, signal.SIGTERM),
    ]


def test_replace_existing_gateway_instance_force_kills_entire_tree(monkeypatch):
    tree_signals = []
    zero_checks = []
    remove_calls = []
    release_calls = []

    monkeypatch.setattr(
        gateway_run,
        '_signal_process_tree',
        lambda pid, sig, strict_root=False: tree_signals.append((pid, sig, strict_root)),
    )

    def fake_kill(pid, sig):
        if sig == 0:
            zero_checks.append(pid)
            return None
        raise AssertionError(f'unexpected direct kill: {(pid, sig)}')

    monkeypatch.setattr(gateway_run.os, 'kill', fake_kill)
    monkeypatch.setattr(gateway_run.time, 'sleep', lambda _: None)

    import gateway.status as status_mod
    monkeypatch.setattr(status_mod, 'remove_pid_file', lambda: remove_calls.append('removed'))
    monkeypatch.setattr(status_mod, 'release_all_scoped_locks', lambda: release_calls.append('released') or 0)

    assert gateway_run._replace_existing_gateway_instance(4242) is True
    assert tree_signals == [
        (4242, signal.SIGTERM, True),
        (4242, signal.SIGKILL, False),
    ]
    assert len(zero_checks) == 20
    assert remove_calls == ['removed']
    assert release_calls == ['released']
