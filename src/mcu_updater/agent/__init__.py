"""The Moonraker agent.

Moonraker supports third-party extensions officially: a separate process connects
to its unix socket, identifies itself as ``type: "agent"``, and registers method
names. Front ends then invoke those methods with
``POST /server/extensions/request`` (or the ``server.extensions.request``
JSON-RPC call), and the agent pushes events back out to every connected client.

Nothing in Moonraker needs patching, and nothing here needs an API key - unix
socket connections are pre-authenticated.
"""

from __future__ import annotations

from .rpc import MoonrakerPeer, RpcError

__all__ = ["MoonrakerPeer", "RpcError", "Agent"]


def __getattr__(name: str):  # pragma: no cover - lazy to avoid a heavy import
    if name == "Agent":
        from .service import Agent

        return Agent
    raise AttributeError(name)
