import unittest
from unittest.mock import MagicMock
import sys
import os

# Add service directory to path
sys.path.insert(0, os.path.dirname(__file__))

# Mock all heavy dependencies BEFORE importing recommendation_server.
# These modules are imported at module level, so they must exist in
# sys.modules before the first import.
_MOCKED_MODULES = {
    'grpc': MagicMock(),
    'googlecloudprofiler': MagicMock(),
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
    'logger': MagicMock(),
}

for mod_name, mock_obj in _MOCKED_MODULES.items():
    sys.modules.setdefault(mod_name, mock_obj)

import recommendation_server


class TestRecommendationService(unittest.TestCase):
    """Tests for the RecommendationService.ListRecommendations logic."""

    def _make_product(self, product_id):
        p = MagicMock()
        p.id = product_id
        return p

    def _mock_catalog(self, product_ids):
        """Create a mock product_catalog_stub with given product IDs."""
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

        response = recommendation_server.RecommendationService().ListRecommendations(request, context)
        recommended = response.product_ids.extend.call_args[0][0]
        self.assertEqual(len(recommended), 5)

    def test_filters_out_requested_products(self):
        all_products = ['A', 'B', 'C', 'D', 'E', 'F']
        recommendation_server.product_catalog_stub = self._mock_catalog(all_products)

        request = MagicMock()
        request.product_ids = ['A', 'B']
        context = MagicMock()

        response = recommendation_server.RecommendationService().ListRecommendations(request, context)
        recommended = response.product_ids.extend.call_args[0][0]
        for pid in recommended:
            self.assertNotIn(pid, ['A', 'B'])

    def test_returns_fewer_when_catalog_is_small(self):
        all_products = ['A', 'B']
        recommendation_server.product_catalog_stub = self._mock_catalog(all_products)

        request = MagicMock()
        request.product_ids = []
        context = MagicMock()

        response = recommendation_server.RecommendationService().ListRecommendations(request, context)
        recommended = response.product_ids.extend.call_args[0][0]
        self.assertEqual(len(recommended), 2)

    def test_empty_catalog_returns_empty(self):
        recommendation_server.product_catalog_stub = self._mock_catalog([])

        request = MagicMock()
        request.product_ids = []
        context = MagicMock()

        response = recommendation_server.RecommendationService().ListRecommendations(request, context)
        recommended = response.product_ids.extend.call_args[0][0]
        self.assertEqual(len(recommended), 0)

    def test_all_products_filtered_returns_empty(self):
        all_products = ['A', 'B']
        recommendation_server.product_catalog_stub = self._mock_catalog(all_products)

        request = MagicMock()
        request.product_ids = ['A', 'B']
        context = MagicMock()

        response = recommendation_server.RecommendationService().ListRecommendations(request, context)
        recommended = response.product_ids.extend.call_args[0][0]
        self.assertEqual(len(recommended), 0)

    def test_health_check(self):
        service = recommendation_server.RecommendationService()
        request = MagicMock()
        context = MagicMock()

        result = service.Check(request, context)
        self.assertIsNotNone(result)


if __name__ == '__main__':
    unittest.main()
