"""Shared policy contracts for the Open Research and Office domains.

The package deliberately contains no inspection-domain imports.  It is a
small control plane that can be used by the message endpoint as well as by
asynchronous workers before they perform a side effect.
"""

from .contracts import GateContext, GateDecision, GateError, WorkflowEnvelope
from .gate_engine import GateEngine
from .policy_registry import DEFAULT_FEATURE_FLAGS, feature_enabled

__all__ = [
    "DEFAULT_FEATURE_FLAGS",
    "GateContext",
    "GateDecision",
    "GateEngine",
    "GateError",
    "WorkflowEnvelope",
    "feature_enabled",
]
