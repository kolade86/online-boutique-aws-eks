import unittest
from unittest.mock import MagicMock
import sys
import os
import types

# Add service directory to path
sys.path.insert(0, os.path.dirname(__file__))

# Create proper base classes so that class inheritance in
# recommendation_server.py produces real classes, not MagicMocks.
_mock_demo_pb2_grpc = types.ModuleType('demo_pb2_grpc')
_mock_demo_pb2_grpc.RecommendationServiceServicer = type('RecommendationServiceServicer', (), {})
_mock_demo_pb2_grpc.ProductCatalogServiceStub = MagicMock()
_mock_demo_pb2_grpc.add_RecommendationServiceServicer_to_server = MagicMock()

_mock_demo_pb2 = types.ModuleType('demo_pb2')
_mock_demo_pb2.Empty = MagicMock(return_value='empty')
# ListRecommendationsResponse needs a real class with product_ids
class _MockListRecommendationsResponse:
    def __init__(self):
        self.product_ids = []
_mock_demo_pb2.ListRecommendationsResponse = _MockListRecommendationsResponse

_mock_health_pb2 = types.ModuleType('health_pb2')
_mock_health_pb2.HealthCheckResponse = MagicMock()
_mock_health_pb2.HealthCheckResponse.SERVING = 'SERVING'
_mock_health_pb2.HealthCheckResponse.UNIMPLEMENTED = 'UNIMPLEMENTED'

# Mock all heavy dependencies BEFORE importing recommendation_server.
_MOCKED_MODULES = {
    'grpc': MagicMock(),
    'googlecloudprofiler': MagicMock(),
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
    'logger': MagicMock(),
}

for mod_name, mock_obj in _MOCKED_MODULES.items():
    sys.modules[mod_name] = mock_obj

import recommendation_server


class TestRecommendationService(unittest.TestCase):

    def _make_product(self, product_id):
        p = MagicMock()
        p.id = product_id
        return p

    def _mock_catalog(self, product_ids):
        catalog_response = MagicMock()
        catalog_response.products = [self._make_product(pid) for pid in product_ids]
        stub = MagicMock()
        stub.ListProducts.return_value = catalog_response
        return stub

    def test_returns_max_5_recommendations(self):
        all_products = [f'PROD{i}' for i in range(10)]
        recommendation_server.product_catalog_stub = self._mock_catalog(all_products)

        request = MagicMock()
        request.product_ids = []
        context = MagicMock()

        service = recommendation_server.RecommendationService()
        response = service.ListRecommendations(request, context)
        self.assertEqual(len(response.product_ids), 5)

    def test_filters_out_requested_products(self):
        all_products = ['A', 'B', 'C', 'D', 'E', 'F']
        recommendation_server.product_catalog_stub = self._mock_catalog(all_products)

        request = MagicMock()
        request.product_ids = ['A', 'B']
        context = MagicMock()

        service = recommendation_server.RecommendationService()
        response = service.ListRecommendations(request, context)
        for pid in response.product_ids:
            self.assertNotIn(pid, ['A', 'B'])

    def test_returns_fewer_when_catalog_is_small(self):
        all_products = ['A', 'B']
        recommendation_server.product_catalog_stub = self._mock_catalog(all_products)

        request = MagicMock()
        request.product_ids = []
        context = MagicMock()

        service = recommendation_server.RecommendationService()
        response = service.ListRecommendations(request, context)
        self.assertEqual(len(response.product_ids), 2)

    def test_empty_catalog_returns_empty(self):
        recommendation_server.product_catalog_stub = self._mock_catalog([])

        request = MagicMock()
        request.product_ids = []
        context = MagicMock()

        service = recommendation_server.RecommendationService()
        response = service.ListRecommendations(request, context)
        self.assertEqual(len(response.product_ids), 0)

    def test_all_products_filtered_returns_empty(self):
        all_products = ['A', 'B']
        recommendation_server.product_catalog_stub = self._mock_catalog(all_products)

        request = MagicMock()
        request.product_ids = ['A', 'B']
        context = MagicMock()

        service = recommendation_server.RecommendationService()
        response = service.ListRecommendations(request, context)
        self.assertEqual(len(response.product_ids), 0)

    def test_health_check(self):
        service = recommendation_server.RecommendationService()
        result = service.Check(MagicMock(), MagicMock())
        self.assertIsNotNone(result)


if __name__ == '__main__':
    unittest.main()
