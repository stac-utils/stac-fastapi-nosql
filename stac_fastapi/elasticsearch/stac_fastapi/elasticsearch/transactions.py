"""transactions extension client."""

import logging
from datetime import datetime, timezone

import attr
import elasticsearch
from elasticsearch import helpers
from overrides import overrides

from stac_fastapi.elasticsearch.config import ElasticsearchSettings
from stac_fastapi.elasticsearch.serializers import CollectionSerializer, ItemSerializer
from stac_fastapi.elasticsearch.session import Session
from stac_fastapi.extensions.third_party.bulk_transactions import (
    BaseBulkTransactionsClient,
    Items,
)
from stac_fastapi.types import stac as stac_types
from stac_fastapi.types.core import BaseTransactionsClient
from stac_fastapi.types.errors import ConflictError, ForeignKeyError, NotFoundError
from stac_fastapi.types.links import CollectionLinks

logger = logging.getLogger(__name__)


@attr.s
class TransactionsClient(BaseTransactionsClient):
    """Transactions extension specific CRUD operations."""

    session: Session = attr.ib(default=attr.Factory(Session.create_from_env))
    settings = ElasticsearchSettings()
    client = settings.create_client

    dynamicTemplates = [
        # Common https://github.com/radiantearth/stac-spec/blob/master/item-spec/common-metadata.md
        {
            "descriptions": {
                "match_mapping_type": "string",
                "match": "description",
                "mapping": {"type": "text"},
            }
        },
        {
            "titles": {
                "match_mapping_type": "string",
                "match": "title",
                "mapping": {"type": "text"},
            }
        },
        # Projection Extension https://github.com/stac-extensions/projection
        {"proj_epsg": {"match": "proj:epsg", "mapping": {"type": "integer"}}},
        {
            "proj_projjson": {
                "match": "proj:projjson",
                "mapping": {"type": "object", "enabled": False},
            }
        },
        {
            "proj_centroid": {
                "match_mapping_type": "string",
                "match": "proj:centroid",
                "mapping": {"type": "geo_point"},
            }
        },
        {
            "proj_geometry": {
                "match_mapping_type": "string",
                "match": "proj:geometry",
                "mapping": {"type": "geo_shape"},
            }
        },
        {
            "no_index_href": {
                "match": "href",
                "mapping": {"type": "text", "index": False},
            }
        },
        # Default all other strings not otherwise specified to keyword
        {"strings": {"match_mapping_type": "string", "mapping": {"type": "keyword"}}},
        {"numerics": {"match_mapping_type": "long", "mapping": {"type": "float"}}},
    ]

    mappings = {
        "numeric_detection": False,
        "dynamic_templates": dynamicTemplates,
        "properties": {
            "geometry": {"type": "geo_shape"},
            "assets": {"type": "object", "enabled": False},
            "links": {"type": "object", "enabled": False},
            "properties": {
                "type": "object",
                "properties": {
                    # Common https://github.com/radiantearth/stac-spec/blob/master/item-spec/common-metadata.md
                    "datetime": {"type": "date"},
                    "start_datetime": {"type": "date"},
                    "end_datetime": {"type": "date"},
                    "created": {"type": "date"},
                    "updated": {"type": "date"},
                    # Satellite Extension https://github.com/stac-extensions/sat
                    "sat:absolute_orbit": {"type": "integer"},
                    "sat:relative_orbit": {"type": "integer"},
                },
            },
        },
    }

    def _create_item_index(self):
        self.client.indices.create(
            index="stac_items",
            body=self.mappings,
            ignore=400,  # ignore 400 already exists code
        )

    @overrides
    def create_item(self, model: stac_types.Item, **kwargs):
        """Create item."""
        base_url = str(kwargs["request"].base_url)
        self._create_item_index()

        # If a feature collection is posted
        if model["type"] == "FeatureCollection":
            bulk_client = BulkTransactionsClient()
            processed_items = [
                bulk_client._preprocess_item(item, base_url)
                for item in model["features"]
            ]
            return_msg = f"Successfully added {len(processed_items)} items."
            bulk_client.bulk_sync(processed_items)

            return return_msg

        # If a single item is posted
        if not self.client.exists(index="stac_collections", id=model["collection"]):
            raise ForeignKeyError(f"Collection {model['collection']} does not exist")

        if self.client.exists(index="stac_items", id=model["id"]):
            raise ConflictError(
                f"Item {model['id']} in collection {model['collection']} already exists"
            )

        data = ItemSerializer.stac_to_db(model, base_url)

        self.client.index(
            index="stac_items", doc_type="_doc", id=model["id"], document=data
        )
        return ItemSerializer.db_to_stac(model, base_url)

    @overrides
    def create_collection(self, model: stac_types.Collection, **kwargs):
        """Create collection."""
        base_url = str(kwargs["request"].base_url)
        collection_links = CollectionLinks(
            collection_id=model["id"], base_url=base_url
        ).create_links()
        model["links"] = collection_links

        self._create_item_index()

        if self.client.exists(index="stac_collections", id=model["id"]):
            raise ConflictError(f"Collection {model['id']} already exists")
        self.client.index(
            index="stac_collections", doc_type="_doc", id=model["id"], document=model
        )
        return CollectionSerializer.db_to_stac(model, base_url)

    @overrides
    def update_item(self, model: stac_types.Item, **kwargs):
        """Update item."""
        base_url = str(kwargs["request"].base_url)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        model["properties"]["updated"] = str(now)

        if not self.client.exists(index="stac_collections", id=model["collection"]):
            raise ForeignKeyError(f"Collection {model['collection']} does not exist")
        if not self.client.exists(index="stac_items", id=model["id"]):
            raise NotFoundError(
                f"Item {model['id']} in collection {model['collection']} doesn't exist"
            )
        self.delete_item(model["id"], model["collection"])
        self.create_item(model, **kwargs)
        # self.client.update(index="stac_items",doc_type='_doc',id=model["id"],
        #         body=model)
        return ItemSerializer.db_to_stac(model, base_url)

    @overrides
    def update_collection(self, model: stac_types.Collection, **kwargs):
        """Update collection."""
        base_url = str(kwargs["request"].base_url)
        try:
            _ = self.client.get(index="stac_collections", id=model["id"])
        except elasticsearch.exceptions.NotFoundError:
            raise NotFoundError(f"Collection {model['id']} not found")
        self.delete_collection(model["id"])
        self.create_collection(model, **kwargs)

        return CollectionSerializer.db_to_stac(model, base_url)

    @overrides
    def delete_item(self, item_id: str, collection_id: str, **kwargs):
        """Delete item."""
        try:
            _ = self.client.get(index="stac_items", id=item_id)
        except elasticsearch.exceptions.NotFoundError:
            raise NotFoundError(f"Item {item_id} not found")
        self.client.delete(index="stac_items", doc_type="_doc", id=item_id)

    @overrides
    def delete_collection(self, collection_id: str, **kwargs):
        """Delete collection."""
        try:
            _ = self.client.get(index="stac_collections", id=collection_id)
        except elasticsearch.exceptions.NotFoundError:
            raise NotFoundError(f"Collection {collection_id} not found")
        self.client.delete(index="stac_collections", doc_type="_doc", id=collection_id)


@attr.s
class BulkTransactionsClient(BaseBulkTransactionsClient):
    """Postgres bulk transactions."""

    session: Session = attr.ib(default=attr.Factory(Session.create_from_env))

    def __attrs_post_init__(self):
        """Create es engine."""
        settings = ElasticsearchSettings()
        self.client = settings.create_client

    def _preprocess_item(self, model: stac_types.Item, base_url) -> stac_types.Item:
        """Preprocess items to match data model."""
        if not self.client.exists(index="stac_collections", id=model["collection"]):
            raise ForeignKeyError(f"Collection {model['collection']} does not exist")

        if self.client.exists(index="stac_items", id=model["id"]):
            raise ConflictError(
                f"Item {model['id']} in collection {model['collection']} already exists"
            )

        item = ItemSerializer.stac_to_db(model, base_url)
        return item

    def bulk_sync(self, processed_items):
        """Elasticsearch bulk insertion."""
        actions = [
            {"_index": "stac_items", "_source": item} for item in processed_items
        ]
        helpers.bulk(self.client, actions)

    @overrides
    def bulk_item_insert(self, items: Items, **kwargs) -> str:
        """Bulk item insertion using es."""
        transactions_client = TransactionsClient()
        transactions_client._create_item_index()
        try:
            base_url = str(kwargs["request"].base_url)
        except Exception:
            base_url = ""
        processed_items = [
            self._preprocess_item(item, base_url) for item in items.items.values()
        ]
        return_msg = f"Successfully added {len(processed_items)} items."

        self.bulk_sync(processed_items)

        return return_msg
