"""
broker package
==============
Defines a small Broker interface so the monitoring/trigger logic in
monitor.py never has to know whether it's talking to a real Angel One
SmartAPI session or a fake position book used for testing.

Two implementations ship here:
  - mock_broker.MockBroker    -> in-memory positions + a simulated price
                                  walk, used to satisfy the client's
                                  "prove it with dummy positions" test.
  - angelone_broker.AngelOneBroker -> thin wrapper over the official
                                  smartapi-python SDK.
"""
from .base import Broker, Position

__all__ = ["Broker", "Position"]
