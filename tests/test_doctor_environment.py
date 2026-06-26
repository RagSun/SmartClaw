"""doctor 环境检测（KVM / Docker / 沙箱后端）。"""

from smartclaw.config.loader import Config, SandboxConfig
from smartclaw.diagnostics import (
    check_docker_daemon,
    check_firecracker_binary,
    check_kvm_environment,
    sandbox_backend_doctor_check,
)


def test_check_kvm_linux_no_dev_kvm(monkeypatch) -> None:
    monkeypatch.setattr("smartclaw.diagnostics.sys.platform", "linux")

    class _P:
        def exists(self):
            return False

        def is_char_device(self):
            return False

    monkeypatch.setattr("smartclaw.diagnostics.Path", lambda x: _P() if x == "/dev/kvm" else __import__("pathlib").Path(x))
    st, det = check_kvm_environment()
    assert "Windows" not in det
    assert "docker" in det.lower() or "嵌套" in det


def test_sandbox_backend_docker_mismatch() -> None:
    cfg = Config(sandbox=SandboxConfig(enabled=True, backend="docker"))
    name, st, det = sandbox_backend_doctor_check(cfg)
    assert name == "配置: 沙箱后端"
    # docker 未安装或 daemon 不可用时应为不匹配/未安装
    assert "backend=docker" in det


def test_sandbox_backend_firecracker_warns_without_kvm(monkeypatch) -> None:
    monkeypatch.setattr("smartclaw.diagnostics.sys.platform", "linux")

    class _P:
        def exists(self):
            return False

        def is_char_device(self):
            return False

    monkeypatch.setattr("smartclaw.diagnostics.Path", lambda x: _P() if x == "/dev/kvm" else __import__("pathlib").Path(x))
    monkeypatch.setattr("smartclaw.diagnostics.shutil.which", lambda _: None)
    cfg = Config(sandbox=SandboxConfig(enabled=True, backend="firecracker"))
    _name, st, det = sandbox_backend_doctor_check(cfg)
    assert "注意" in st or "降级" in det


def test_firecracker_binary_optional() -> None:
    st, det = check_firecracker_binary()
    assert st  # 有状态即可
    assert det
