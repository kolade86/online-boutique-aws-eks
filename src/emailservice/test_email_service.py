import unittest
from unittest.mock import MagicMock
import sys
import os
import types

# Add service directory to path
sys.path.insert(0, os.path.dirname(__file__))

# Create proper base classes so that class inheritance in email_server.py
# produces real classes, not MagicMocks.
_mock_demo_pb2_grpc = types.ModuleType('demo_pb2_grpc')
_mock_demo_pb2_grpc.EmailServiceServicer = type('EmailServiceServicer', (), {})
_mock_demo_pb2_grpc.add_EmailServiceServicer_to_server = MagicMock()

_mock_demo_pb2 = types.ModuleType('demo_pb2')
_mock_demo_pb2.Empty = MagicMock(return_value='empty')

_mock_health_pb2 = types.ModuleType('health_pb2')
_mock_health_pb2.HealthCheckResponse = MagicMock()
_mock_health_pb2.HealthCheckResponse.SERVING = 'SERVING'
_mock_health_pb2.HealthCheckResponse.UNIMPLEMENTED = 'UNIMPLEMENTED'

# Mock all heavy dependencies BEFORE importing email_server.
_MOCKED_MODULES = {
    'grpc': MagicMock(),
    'jinja2': MagicMock(),
    'google.api_core.exceptions': MagicMock(),
    'google.auth.exceptions': MagicMock(),
    'demo_pb2': _mock_demo_pb2,
    'demo_pb2_grpc': _mock_demo_pb2_grpc,
    'grpc_health.v1': MagicMock(),
    'grpc_health.v1.health_pb2': _mock_health_pb2,
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
    sys.modules[mod_name] = mock_obj

import email_server


class TestDummyEmailService(unittest.TestCase):
    """Tests for the DummyEmailService used in non-production mode."""

    def test_send_order_confirmation_returns_empty(self):
        service = email_server.DummyEmailService()
        request = MagicMock()
        request.email = 'test@example.com'
        context = MagicMock()

        result = service.SendOrderConfirmation(request, context)
        self.assertIsNotNone(result)

    def test_send_order_confirmation_does_not_set_error(self):
        service = email_server.DummyEmailService()
        request = MagicMock()
        request.email = 'customer@shop.com'
        context = MagicMock()

        service.SendOrderConfirmation(request, context)
        context.set_code.assert_not_called()

    def test_health_check(self):
        service = email_server.DummyEmailService()
        result = service.Check(MagicMock(), MagicMock())
        self.assertIsNotNone(result)

    def test_watch(self):
        service = email_server.DummyEmailService()
        result = service.Watch(MagicMock(), MagicMock())
        self.assertIsNotNone(result)


class TestEmailServiceProduction(unittest.TestCase):
    def test_production_email_service_raises(self):
        with self.assertRaises(Exception):
            email_server.EmailService()


class TestStartFunction(unittest.TestCase):
    def test_non_dummy_mode_raises(self):
        with self.assertRaises(Exception):
            email_server.start(dummy_mode=False)


if __name__ == '__main__':
    unittest.main()
