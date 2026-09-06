"""Representative, real-world-inspired link profiles.

These are NOT measurements of any specific real network. They are
documented, order-of-magnitude-plausible defaults for four common
deployment scenarios, so experiments don't rely on arbitrary hand-picked
numbers. Every value is overridable -- a profile is a starting point, a
``ndn_sim.topology.Link`` built from one can still be customized.

Sources for the ranges (documented, not cited as exact): typical data
center / LAN / regional-WAN / long-haul link engineering figures used in
networking coursework and vendor whitepapers. Propagation speed defaults
to ~2x10^8 m/s (representative fiber), expressed as
``topology.DEFAULT_PROPAGATION_SPEED_KM_PER_MS``.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..topology import Link


@dataclass(frozen=True)
class NetworkProfile:
    name: str
    description: str
    bandwidth_mbps: float
    propagation_delay_ms: float
    processing_delay_ms: float
    loss_probability: float
    queue_capacity_packets: int

    def to_link(self, *, active: bool = True, **overrides) -> Link:
        """Build a concrete ``Link`` from this profile, with optional overrides."""
        fields = dict(
            propagation_delay_ms=self.propagation_delay_ms,
            bandwidth_mbps=self.bandwidth_mbps,
            queue_limit_packets=self.queue_capacity_packets,
            active=active,
            processing_delay_ms=self.processing_delay_ms,
            loss_probability=self.loss_probability,
        )
        fields.update(overrides)
        return Link(**fields)


# Representative defaults. See module docstring: order-of-magnitude
# plausible, not measured. All are overridable per-experiment.
DATA_CENTER = NetworkProfile(
    name="DATA_CENTER",
    description="Server-to-server / rack-to-rack communication.",
    bandwidth_mbps=10_000.0,
    propagation_delay_ms=0.02,
    processing_delay_ms=0.02,
    loss_probability=1e-6,
    queue_capacity_packets=200,
)

LAN = NetworkProfile(
    name="LAN",
    description="Local / campus network.",
    bandwidth_mbps=1_000.0,
    propagation_delay_ms=0.05,
    processing_delay_ms=0.10,
    loss_probability=1e-5,
    queue_capacity_packets=100,
)

REGIONAL_WAN = NetworkProfile(
    name="REGIONAL_WAN",
    description="Metropolitan / regional inter-city communication.",
    bandwidth_mbps=500.0,
    propagation_delay_ms=5.0,
    processing_delay_ms=0.2,
    loss_probability=1e-4,
    queue_capacity_packets=100,
)

INTERNET_WAN = NetworkProfile(
    name="INTERNET_WAN",
    description="Long-distance Internet communication.",
    bandwidth_mbps=500.0,
    propagation_delay_ms=20.0,
    processing_delay_ms=0.5,
    loss_probability=1e-4,
    queue_capacity_packets=100,
)

INTERCONTINENTAL = NetworkProfile(
    name="INTERCONTINENTAL",
    description="Very long-distance (intercontinental) communication.",
    bandwidth_mbps=500.0,
    propagation_delay_ms=50.0,
    processing_delay_ms=0.5,
    loss_probability=1e-4,
    queue_capacity_packets=100,
)

PROFILES = {
    p.name: p
    for p in (DATA_CENTER, LAN, REGIONAL_WAN, INTERNET_WAN, INTERCONTINENTAL)
}


def get_profile(name: str) -> NetworkProfile:
    key = name.strip().upper()
    if key not in PROFILES:
        raise ValueError(f"unknown network profile: {name!r} (choices: {sorted(PROFILES)})")
    return PROFILES[key]


# Representative protocol/message sizes (bytes). Not tied to a profile --
# these describe the *message*, profiles describe the *link*.
DEFAULT_PACKET_SIZES_BYTES = {
    "ip_packet": 1500,
    "ndn_interest": 200,
    "ndn_data": 4096,
    "sdn_control_message": 256,
}
