import random

from relevanceai import Client
from relevanceai.utils.datasets import (
    get_iris_dataset,
    get_online_ecommerce_dataset,
    get_palmer_penguins_dataset,
)

from tests.globals.constants import SAMPLE_DATASET_DATASET_PREFIX


class TestVectorizeOps:
    def test_vectorize(self, test_client: Client):
        dataset = test_client.Dataset(SAMPLE_DATASET_DATASET_PREFIX + "_ecom")
        dataset.insert_documents(
            documents=get_online_ecommerce_dataset(),
            create_id=True,
        )

        dataset.vectorize(feature_vector=True)

        assert "_dim4_feature_vector_" in dataset.schema

    def test_numeric_vectorize(self, test_client: Client):
        dataset = test_client.Dataset(SAMPLE_DATASET_DATASET_PREFIX + "_iris")
        dataset.insert_documents(
            documents=get_iris_dataset(),
            create_id=True,
        )

        dataset.vectorize(feature_vector=True)

        assert "_dim4_feature_vector_" in dataset.schema

        dataset.vectorize(fields=["numeric"], feature_vector=True)

        assert "_dim4_feature_vector_" in dataset.schema

    def test_custom_vectorize(self, test_client: Client):
        dataset = test_client.Dataset(SAMPLE_DATASET_DATASET_PREFIX + "_penguins")
        dataset.insert_documents(
            documents=get_palmer_penguins_dataset(),
            create_id=True,
        )

        from relevanceai.operations.vector import Base2Vec

        class CustomTextEncoder(Base2Vec):
            __name__ = "CustomTextEncoder1".lower()

            def __init__(self, *args, **kwargs):
                super().__init__()

                self.vector_length = 128
                self.model = lambda x: [
                    random.random() if "None" not in str(x) else random.random() + 10
                    for _ in range(self.vector_length)
                ]

            def encode(self, value):
                vector = self.model(value)
                return vector

        dataset.vectorize(
            encoders=dict(
                text=[
                    CustomTextEncoder(),
                ],
            ),
            feature_vector=True,
        )

        vectors = [
            "Comments_customtextencoder_vector_",
            "Species_customtextencoder_vector_",
            "Stage_customtextencoder_vector_",
        ]
        assert all(vector in dataset.schema for vector in vectors)
