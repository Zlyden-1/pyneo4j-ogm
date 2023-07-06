"""
This module holds the `RelationshipProperty` which can be used to define relationships between
nodes on node models.
"""
import logging
from typing import TypeVar

from pydantic import BaseModel, PrivateAttr

from neo4j_ogm.exceptions import InvalidRelationshipModelOrType, InvalidTargetModel, UnregisteredModel
from neo4j_ogm.node import Neo4jNode
from neo4j_ogm.relationship import Neo4jRelationship
from neo4j_ogm.utils import RelationshipDirection

T = TypeVar("T")


class RelationshipProperty(BaseModel):
    """
    Property for defining relationships on node models.
    """

    _client = PrivateAttr()
    _target_model: Neo4jNode = PrivateAttr()
    _relationship_model: Neo4jRelationship | None = PrivateAttr(default=None)
    _type: str = PrivateAttr()
    _direction: RelationshipDirection = PrivateAttr()

    def __init_subclass__(cls) -> None:
        """
        Initializes the client for this relationship property
        """
        from neo4j_ogm.client import Neo4jClient

        cls._client = Neo4jClient()

        return super().__init_subclass__()

    def __init__(
        self,
        target_model: str | Neo4jNode,
        relationship_or_type: str | Neo4jRelationship,
        direction: RelationshipDirection,
    ) -> None:
        # Check if model has been registered
        if isinstance(str, target_model):
            model_class = [model for model in self._client.database_models if model.__class__.__name__ == target_model]

            if len(model_class) == 0:
                raise UnregisteredModel(unregistered_model=target_model)

            self._target_model = model_class[0]
        elif issubclass(Neo4jNode, target_model):
            self._target_model = target_model
        else:
            raise InvalidTargetModel(target_model=target_model)

        # If relationship model has been registered, use model, else use provided string as type
        if isinstance(str, relationship_or_type):
            model_class = [model for model in self._client.database_models if model.__class__.__name__ == target_model]

            if len(model_class) == 0:
                self._type = relationship_or_type

            relationship_model = model_class[0]
            self._relationship_model = relationship_model
            self._type = relationship_model.__type__
        elif issubclass(Neo4jRelationship, target_model):
            self._relationship_model = relationship_or_type
            self._type = relationship_or_type.__type__
        else:
            raise InvalidRelationshipModelOrType(relationship_or_type=relationship_or_type)

        self._direction = direction.value
