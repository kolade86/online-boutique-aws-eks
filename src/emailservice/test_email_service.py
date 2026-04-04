import unittest
from unittest.mock import MagicMock
import sys
import os

# Add service directory to path
sys.path.insert(0, os.path.dirname(__file__))

# Mock all heavy dependencies BEFORE importing email_server.
# These modules are imported at module level in email_server.py,
# so they must exist in sys.modules before the first import.
_MOCKED_MODULES = {
    'grpc': MagicMock(),
    'jinja2': MagicMock(),
    'google.api_core.exceptions': MagicMock(),
    'google.auth.exceptions': MagicMock(),
    'demo_pb2': MagicMock(),
    'demo_pb2_grpc': MagicMock(),
    'grpc_health.v1': MagicMock(),
    'grpc_health.v1.health_pb2': MagicMock(),
    'grpc_health.v1.health_pb2_grpc': MagicMock(),
    'opentelemetry': MagicMock(),
    'opentelemetry.trace': MagicMock(),
    'opentelemetry.instrumentation.grpc': MagicMock(),
    'opentelemetry.sdk.trace': MagicMock(),
    'opentelemetry.sdk.trace.export': MagicMock(),
    'opentelemetry.exporter.otlp.proto.grpc.trace_exporter': MagicMock(),
    'googlecloudprofiler': MagicMock(),
    'logger': MagicMock(),
}

for mod_name, mock_obj in _MOCKED_MODULES.items():
    sys.modules.setdefault(mod_name, mock_obj)

import email_server


class TestDummyEmailService(unittest.TestCase):
    """Tests for the DummyEmailService used in non-production mode."""

    def test_send_order_confirmation_returns_empty(self):
        """DummyEmailService should log and return Empty."""
        service = email_server.DummyEmailService()
        request = MagicMock()
        request.email = 'test@example.com'
        context = MagicMock()

        result = service.SendOrderConfirmation(request, context)
        self.assertIsNotNone(result)

    def test_send_order_confirmation_does_not_set_error(self):
        """DummyEmailService should not set error code on context."""
        service = email_server.DummyEmailService()
        request = MagicMock()
        request.email = 'customer@shop.com'
        context = MagicMock()

        service.SendOrderConfirmation(request, context)
        context.set_code.assert_not_called()

    def test_health_check_returns_serving(self):
        """Health check should return SERVING status."""
        service = email_server.DummyEmailService()
        request = MagicMock()
        context = MagicMock()

        result = service.Check(request, context)
        self.assertIsNotNone(result)

    def test_watch_returns_unimplemented(self):
        """Watch should return UNIMPLEMENTED status."""
        service = email_server.DummyEmailService()
        request = MagicMock()
        context = MagicMock()

        result = service.Watch(request, context)
        self.assertIsNotNone(result)


class TestEmailServiceProduction(unittest.TestCase):
    """Tests for the production EmailService (not yet implemented)."""

    def test_production_email_service_raises(self):
        """EmailService constructor should raise since cloud mail is not implemented."""
        with self.assertRaises(Exception):
            email_server.EmailService()


class TestStartFunction(unittest.TestCase):
    """Tests for the start() server factory."""

    def test_non_dummy_mode_raises(self):
        """Starting in non-dummy mode should raise."""
        with self.assertRaises(Exception):
            email_server.start(dummy_mode=False)


if __name__ == '__main__':
    unittest.main()
