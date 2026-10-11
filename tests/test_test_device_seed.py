import unittest

from services.test_device_seed import seed_test_device


class FakeDoc:
    def __init__(self, ref):
        self._ref = ref

    @property
    def exists(self):
        return self._ref.path in self._ref.store


class FakeDocumentRef:
    def __init__(self, store, path):
        self.store = store
        self.path = path

    def get(self):
        return FakeDoc(self)

    def set(self, data):
        self.store[self.path] = data


class FakeCollection:
    def __init__(self, store, name):
        self.store = store
        self.name = name

    def document(self, doc_id):
        return FakeDocumentRef(self.store, f"{self.name}/{doc_id}")


class FakeDb:
    def __init__(self):
        self.store = {}

    def collection(self, name):
        return FakeCollection(self.store, name)


class SeedTestDeviceTests(unittest.TestCase):
    def test_seed_test_device_creates_expected_hardware_records(self):
        db = FakeDb()

        result = seed_test_device(db)

        self.assertEqual(result["station"]["id"], "STATION001")
        self.assertEqual(result["station"]["status"], "created")
        self.assertEqual(result["nozzle"]["id"], "NZ001")
        self.assertEqual(result["nozzle"]["status"], "created")
        self.assertEqual(result["price"]["id"], "petrol_global")
        self.assertEqual(result["price"]["status"], "created")

        self.assertIs(db.store["stations/STATION001"]["is_active"], True)
        self.assertEqual(db.store["nozzles/NZ001"]["station_id"], "STATION001")
        self.assertEqual(db.store["nozzles/NZ001"]["hardware_serial"], "NZ001")
        self.assertEqual(db.store["prices/petrol_global"]["fuel_type"], "petrol")

    def test_seed_test_device_is_idempotent(self):
        db = FakeDb()
        seed_test_device(db)
        first_nozzle = db.store["nozzles/NZ001"]

        result = seed_test_device(db)

        self.assertEqual(result["station"]["status"], "already_exists")
        self.assertEqual(result["nozzle"]["status"], "already_exists")
        self.assertEqual(result["price"]["status"], "already_exists")
        self.assertIs(db.store["nozzles/NZ001"], first_nozzle)


if __name__ == "__main__":
    unittest.main()
