# Module Adaptateurs Hardware

import os

from .base import RobotAdapter, AdapterConfig
from .pepper_adapter import PepperAdapter
from .choregraphe_adapter import ChoregrapheAdapter
from .mock_adapter import MockAdapter
from .ssh_bridge_adapter import SSHBridgeAdapter


def get_adapter(config=None) -> RobotAdapter:
    # Retourne l'adaptateur approprie selon la configuration.
    if config and hasattr(config, 'pepper') and config.pepper.ip:
        force_ssh = bool(getattr(config.pepper, "force_ssh_bridge", False))
        force_ssh = force_ssh or os.getenv("PEPPER_FORCE_SSH_BRIDGE", "0") == "1"
        # Sur cette branche, Choregraphe est le mode de contrôle par défaut.
        control_mode = os.getenv("PEPPER_CONTROL_MODE", "choregraphe").strip().lower()

        if control_mode in {"", "auto", "choregraphe"}:
            return ChoregrapheAdapter(
                ip=config.pepper.ip,
                port=config.pepper.port,
            )
        if control_mode == "pepper" and not force_ssh:
            try:
                import qi  # noqa: F401
                return PepperAdapter(
                    ip=config.pepper.ip,
                    port=config.pepper.port
                )
            except Exception:
                pass

        if control_mode in {"ssh", "bridge"}:
            return SSHBridgeAdapter(
                ip=config.pepper.ip,
                port=config.pepper.port,
                ssh_user=getattr(config.pepper, "ssh_user", "nao"),
                ssh_port=int(getattr(config.pepper, "ssh_port", 22)),
                ssh_python=getattr(config.pepper, "ssh_python", "python"),
            )

        return SSHBridgeAdapter(
            ip=config.pepper.ip,
            port=config.pepper.port,
            ssh_user=getattr(config.pepper, "ssh_user", "nao"),
            ssh_port=int(getattr(config.pepper, "ssh_port", 22)),
            ssh_python=getattr(config.pepper, "ssh_python", "python"),
        )
    return MockAdapter()


__all__ = [
    "RobotAdapter",
    "AdapterConfig",
    "PepperAdapter",
    "ChoregrapheAdapter",
    "SSHBridgeAdapter",
    "MockAdapter",
    "get_adapter",
]
