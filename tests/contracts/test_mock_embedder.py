from __future__ import annotations

from ragdemo.embed.mock import MockEmbedder
from tests.contracts.embedder_contract import EmbedderContract


class TestMockEmbedder(EmbedderContract):
    def make_embedder(self) -> MockEmbedder:
        return MockEmbedder()
