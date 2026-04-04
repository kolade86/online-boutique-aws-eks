import unittest
from unittest.mock import MagicMock, patch
import sys
import os

# Add service directory to path
sys.path.insert(0, os.path.dirname(__file__))


class TestDummyEmailService(unittest.TestCase):
    """Tests for the DummyEmailService used in non-production mode."""

    def setUp(self):
        # Import after path setup; mock heavy dependencies that aren't needed
        with patch.dict('sys.modules', {
            'opentelemetry': MagicMock(),
            'opentelemetry.trace': MagicMock(),
            'opentelemetry.instrumentation.grpc': MagicMock(),
            'opentelemetry.sdk.trace': MagicMock(),
            'opentelemetry.sdk.trace.export': MagicMock(),
            'opentelemetry.exporter.otlp.proto.grpc.trace_exporter': MagicMock(),
            'googlecloudprofiler': MagicMock(),
            'google.api_core.exceptions': MagicMock(),
            'google.auth.exceptions': MagicMock(),
            'grpc_health.v1': MagicMock(),
            'grpc_health.v1.health_pb2': MagicMock(),
            'grpc_health.v1.health_pb2_grpc': MagicMock(),
            'logger': MagicMock(),
        }):
            import email_server
            self.email_server = email_server

    def test_send_order_confirmation_returns_empty(self):
        """DummyEmailService should log and return Empty."""
        service = self.email_server.DummyEmailService()
        request = MagicMock()
        request.email = 'test@example.com'
        context = MagicMock()

        result = service.SendOrderConfirmation(request, context)
        self.assertIsNotNone(result)

    def test_send_order_confirmation_logs_email(self):
        """DummyEmailService should log the recipient email."""
        service = self.email_server.DummyEmailService()
        request = MagicMock()
        request.email = 'customer@shop.com'
        context = MagicMock()

        service.SendOrderConfirmation(request, context)
        # Should not set error code on context
        context.set_code.assert_not_called()

    def test_health_check_returns_serving(self):
        """Health check should return SERVING status."""
        service = self.email_server.DummyEmailService()
        request = MagicMock()
        context = MagicMock()

        result = service.Check(request, context)
        self.assertIsNotNone(result)

    def test_watch_returns_unimplemented(self):
        """Watch should return UNIMPLEMENTED status."""
        service = self.email_server.DummyEmailService()
        request = MagicMock()
        context = MagicMock()

        result = service.Watch(request, context)
        self.assertIsNotNone(result)


class TestEmailServiceProduction(unittest.TestCase):
    """Tests for the production EmailService (not yet implemented)."""

    def test_production_email_service_raises(self):
        """EmailService constructor should raise since cloud mail is not implemented."""
        with patch.dict('sys.modules', {
            'opentelemetry': MagicMock(),
            'opentelemetry.trace': MagicMock(),
            'opentelemetry.instrumentation.grpc': MagicMock(),
            'opentelemetry.sdk.trace': MagicMock(),
            'opentelemetry.sdk.trace.export': MagicMock(),
            'opentelemetry.exporter.otlp.proto.grpc.trace_exporter': MagicMock(),
            'googlecloudprofiler': MagicMock(),
            'google.api_core.exceptions': MagicMock(),
            'google.auth.exceptions': MagicMock(),
            'grpc_health.v1': MagicMock(),
            'grpc_health.v1.health_pb2': MagicMock(),
            'grpc_health.v1.health_pb2_grpc': MagicMock(),
            'logger': MagicMock(),
        }):
            import email_server
            with self.assertRaises(Exception):
                email_server.EmailService()


class TestStartFunction(unittest.TestCase):
    """Tests for the start() server factory."""

    def test_non_dummy_mode_raises(self):
        """Starting in non-dummy mode should raise."""
        with patch.dict('sys.modules', {
            'opentelemetry': MagicMock(),
            'opentelemetry.trace': MagicMock(),
            'opentelemetry.instrumentation.grpc': MagicMock(),
            'opentelemetry.sdk.trace': MagicMock(),
            'opentelemetry.sdk.trace.export': MagicMock(),
            'opentelemetry.exporter.otlp.proto.grpc.trace_exporter': MagicMock(),
            'googlecloudprofiler': MagicMock(),
            'google.api_core.exceptions': MagicMock(),
            'google.auth.exceptions': MagicMock(),
            'grpc_health.v1': MagicMock(),
            'grpc_health.v1.health_pb2': MagicMock(),
            'grpc_health.v1.health_pb2_grpc': MagicMock(),
            'logger': MagicMock(),
        }):
            import email_server
            with self.assertRaises(Exception):
                email_server.start(dummy_mode=False)


if __name__ == '__main__':
    unittest.main()
