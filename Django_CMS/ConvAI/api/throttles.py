# ConvAI/api/throttles.py
from rest_framework.throttling import ScopedRateThrottle

class MessageThrottle(ScopedRateThrottle):
    scope = "messages"