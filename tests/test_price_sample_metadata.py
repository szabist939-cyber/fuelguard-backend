import asyncio
import unittest
from unittest.mock import MagicMock, patch
from routers import prices


class PriceSampleMetadataTests(unittest.TestCase):
    def test_comparison_preserves_sample_price_marker(self):
        db = MagicMock()
        collection = db.collection.return_value
        collection.where.return_value = collection
        price = MagicMock()
        price.to_dict.return_value = {
            'station_id': 'PSO-I8', 'price_per_litre': 282.76, 'is_sample': True,
        }
        collection.get.return_value = [price]
        station = collection.document.return_value.get.return_value
        station.exists = True
        station.to_dict.return_value = {'name': 'PSO Marwat', 'latitude': 33.668048, 'longitude': 73.077308}
        with patch.object(prices, 'get_db', return_value=db):
            result = asyncio.run(prices.compare_prices('petrol', None, None, 20, {}))
        self.assertIs(result[0].get('is_sample'), True)


if __name__ == '__main__':
    unittest.main()
