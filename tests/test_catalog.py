import unittest
from app.catalog import load_services, service_by_id


class CatalogTests(unittest.TestCase):
    def test_configured_catalog_has_unique_ids_and_required_fields(self):
        services = load_services()
        ids = [item["id"] for item in services]
        self.assertGreaterEqual(len(services), 5)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(item["title"] and item["description"] and item["price"] for item in services))

    def test_service_lookup(self):
        services = load_services()
        self.assertIsNotNone(service_by_id(services, "telegram_bot"))
        self.assertIsNone(service_by_id(services, "missing"))


if __name__ == "__main__":
    unittest.main()
