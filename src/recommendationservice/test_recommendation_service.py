import unittest
from unittest.mock import MagicMock, patch
import sys
import os

# Add service directory to path
sys.path.insert(0, os.path.dirname(__file__))


class TestRecommendationService(unittest.TestCase):
    """Tests for the RecommendationService.ListRecommendations logic."""

    def _get_service_class(self):
        """Import with mocked heavy dependencies."""
        with patch.dict('sys.modules', {
            'opentelemetry': MagicMock(),
            'opentelemetry.trace': MagicMock(),
            'opentelemetry.instrumentation.grpc': MagicMock(),
            'opentelemetry.sdk.trace': MagicMock(),
            'opentelemetry.sdk.trace.export': MagicMock(),
            'opentelemetry.exporter.otlp.proto.grpc.trace_exporter': MagicMock(),
            'googlecloudprofiler': MagicMock(),
            'google.auth.exceptions': MagicMock(),
            'grpc_health.v1': MagicMock(),
            'grpc_health.v1.health_pb2': MagicMock(),
            'grpc_health.v1.health_pb2_grpc': MagicMock(),
            'logger': MagicMock(),
        }):
            import recommendation_server
            return recommendation_server.RecommendationService

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
        ServiceClass = self._get_service_class()
        service = ServiceClass()

        all_products = [f'PROD{i}' for i in range(10)]
        import recommendation_server
        recommendation_server.product_catalog_stub = self._mock_catalog(all_products)

        request = MagicMock()
        request.product_ids = []
        context = MagicMock()

        response = service.ListRecommendations(request, context)
        self.assertEqual(len(response.product_ids.extend.call_args[0][0]), 5)

    def test_filters_out_requested_products(self):
        ServiceClass = self._get_service_class()
        service = ServiceClass()

        all_products = ['A', 'B', 'C', 'D', 'E', 'F']
        import recommendation_server
        recommendation_server.product_catalog_stub = self._mock_catalog(all_products)

        request = MagicMock()
        request.product_ids = ['A', 'B']
        context = MagicMock()

        response = service.ListRecommendations(request, context)
        recommended = response.product_ids.extend.call_args[0][0]
        for pid in recommended:
            self.assertNotIn(pid, ['A', 'B'])

    def test_returns_fewer_when_catalog_is_small(self):
        ServiceClass = self._get_service_class()
        service = ServiceClass()

        all_products = ['A', 'B']
        import recommendation_server
        recommendation_server.product_catalog_stub = self._mock_catalog(all_products)

        request = MagicMock()
        request.product_ids = []
        context = MagicMock()

        response = service.ListRecommendations(request, context)
        recommended = response.product_ids.extend.call_args[0][0]
        self.assertEqual(len(recommended), 2)

    def test_empty_catalog_returns_empty(self):
        ServiceClass = self._get_service_class()
        service = ServiceClass()

        import recommendation_server
        recommendation_server.product_catalog_stub = self._mock_catalog([])

        request = MagicMock()
        request.product_ids = []
        context = MagicMock()

        response = service.ListRecommendations(request, context)
        recommended = response.product_ids.extend.call_args[0][0]
        self.assertEqual(len(recommended), 0)

    def test_all_products_filtered_returns_empty(self):
        ServiceClass = self._get_service_class()
        service = ServiceClass()

        all_products = ['A', 'B']
        import recommendation_server
        recommendation_server.product_catalog_stub = self._mock_catalog(all_products)

        request = MagicMock()
        request.product_ids = ['A', 'B']
        context = MagicMock()

        response = service.ListRecommendations(request, context)
        recommended = response.product_ids.extend.call_args[0][0]
        self.assertEqual(len(recommended), 0)

    def test_health_check(self):
        ServiceClass = self._get_service_class()
        service = ServiceClass()
        request = MagicMock()
        context = MagicMock()

        result = service.Check(request, context)
        self.assertIsNotNone(result)


if __name__ == '__main__':
    unittest.main()
