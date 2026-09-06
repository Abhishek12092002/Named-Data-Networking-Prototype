"""Shared, protocol-agnostic discrete-event simulation infrastructure.

Everything in this package is used by *both* the IP and NDN data planes:
link/queue timing (:mod:`ndn_sim.core.timing`), representative network
profiles (:mod:`ndn_sim.core.network_profiles`), and the latency-breakdown
dataclass. Protocol-specific behavior (PIT/FIB semantics, IP routing
tables, forwarding rules) lives in the data-plane modules, not here.
"""

__all__ = ["timing", "network_profiles"]
