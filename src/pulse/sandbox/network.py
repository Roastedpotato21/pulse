"""Network policy and isolation engine for Pulse sandbox execution.

Implements backend-independent network security controls including DNS
rebinding protection, proxy configuration, and connection allowlisting.

Security architecture:
    - Default policy is DENY_ALL.
    - ALLOWLIST dynamically resolves hostnames to prevent DNS rebinding
      to internal/private IPs.
    - PROXY mode securely isolates credentials from direct container exposure.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import socket
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class NetworkMode(str, Enum):
    """Execution network isolation modes."""
    DENY_ALL = "deny_all"
    LOCALHOST_ONLY = "localhost_only"
    ALLOWLIST = "allowlist"
    PROXY = "proxy"
    ALLOW_ALL = "allow_all"


class NetworkEnforcementLevel(str, Enum):
    """Strength of network enforcement provided by a backend.
    
    Security: Only STRONGLY_ENFORCED is accepted by the Sandbox API for strict modes.
    ADVISORY is rejected to prevent silent downgrades of security policies.
    """
    STRONGLY_ENFORCED = "strongly_enforced"
    PARTIALLY_ENFORCED = "partially_enforced"
    ADVISORY = "advisory"
    UNSUPPORTED = "unsupported"


class Protocol(str, Enum):
    """Network protocols."""
    TCP = "tcp"
    UDP = "udp"
    ANY = "any"


@dataclass(frozen=True, slots=True)
class NetworkRule:
    """A rule defining an allowed network destination."""
    
    destination: str  # Can be an IP, a hostname, or a wildcard domain (e.g. *.example.com)
    port: int | None = None  # None means any port
    protocol: Protocol = Protocol.ANY
    
    def matches(self, host: str, port: int, protocol: Protocol) -> bool:
        """Check if a specific request matches this rule."""
        if self.port is not None and self.port != port:
            return False
        if self.protocol != Protocol.ANY and self.protocol != protocol:
            return False
            
        # Exact match or IP match
        if self.destination.lower() == host.lower():
            return True
            
        # Wildcard domain match (e.g. *.example.com)
        return fnmatch.fnmatch(host.lower(), self.destination.lower())


@dataclass(frozen=True, slots=True)
class NetworkPolicy:
    """Backend-independent network security policy.
    
    Security architecture:
        - Mode defaults to DENY_ALL.
        - Rules only apply if mode is ALLOWLIST.
        - Proxy URL is used if mode is PROXY.
    """
    
    mode: NetworkMode = NetworkMode.DENY_ALL
    rules: list[NetworkRule] = field(default_factory=list)
    proxy_url: str | None = None
    
    def is_safe_ip(self, ip_str: str) -> bool:
        """Determine if an IP address is a safe external address.
        
        Security: Protects against SSRF and DNS rebinding by explicitly
        blocking RFC1918 private addresses, loopback, link-local, and multicast
        from being accessed when a domain resolves to them.
        """
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
            
        # Explicitly block internal/private ranges
        if ip.is_loopback:
            return False
        if ip.is_private:
            return False
        if ip.is_link_local:
            return False
        return not ip.is_multicast

    def validate_destination(self, host: str, port: int, protocol: Protocol = Protocol.TCP) -> bool:
        """Validate an outbound connection attempt against the policy.
        
        This is primarily used by backends that can intercept connections
        or by API layers wrapping specific network requests.
        """
        if self.mode == NetworkMode.DENY_ALL:
            return False
            
        if self.mode == NetworkMode.LOCALHOST_ONLY:
            try:
                ip = ipaddress.ip_address(host)
                return ip.is_loopback
            except ValueError:
                return host.lower() == "localhost"
                
        if self.mode == NetworkMode.PROXY:
            # In proxy mode, direct connections are denied; they must go through the proxy URL.
            # This validation returns False for direct requests unless it's the proxy itself.
            return False
            
        if self.mode == NetworkMode.ALLOWLIST:
            rule_matched = False
            for rule in self.rules:
                if rule.matches(host, port, protocol):
                    rule_matched = True
                    break
                    
            if not rule_matched:
                return False
                
            # DNS Rebinding Protection:
            # If the host is a hostname (not a raw IP), we must resolve it locally
            # and verify it does not resolve to an internal/private IP.
            try:
                ipaddress.ip_address(host)
                # It's already a raw IP, and it was in the allowlist.
                return True
            except ValueError:
                pass
                
            try:
                # Resolve hostname
                # Use getaddrinfo to handle both IPv4 and IPv6
                addr_info = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
                for family, type, proto, canonname, sockaddr in addr_info:
                    ip_str = sockaddr[0]
                    if not self.is_safe_ip(ip_str):
                        return False  # Deny if ANY resolved IP is private/unsafe
                return True
            except socket.gaierror:
                # If we can't resolve it, fail closed.
                return False
                
        return False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NetworkPolicy:
        mode_str = str(data.get("mode", "deny_all")).lower()
        try:
            mode = NetworkMode(mode_str)
        except ValueError:
            mode = NetworkMode.DENY_ALL
            
        rules = []
        for raw_rule in data.get("rules", []):
            try:
                protocol_str = str(raw_rule.get("protocol", "any")).lower()
                rules.append(
                    NetworkRule(
                        destination=str(raw_rule.get("destination")),
                        port=int(raw_rule["port"]) if raw_rule.get("port") is not None else None,
                        protocol=Protocol(protocol_str),
                    )
                )
            except (ValueError, KeyError):
                continue
                
        return cls(
            mode=mode,
            rules=rules,
            proxy_url=data.get("proxy_url")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "rules": [
                {
                    "destination": r.destination,
                    "port": r.port,
                    "protocol": r.protocol.value,
                }
                for r in self.rules
            ],
            "proxy_url": self.proxy_url,
        }
