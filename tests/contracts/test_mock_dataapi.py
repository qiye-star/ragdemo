from __future__ import annotations

from ragdemo.dataapi.mock import MockDataApi
from tests.contracts.dataapi_contract import DataApiContract


class TestMockDataApi(DataApiContract):
    def make_api(self) -> MockDataApi:
        return MockDataApi()
