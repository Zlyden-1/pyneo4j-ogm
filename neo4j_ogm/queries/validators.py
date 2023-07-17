"""
This module contains pydantic models for runtime validation of operators in query expressions
and query options.
"""
import logging
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Extra, Field, ValidationError, root_validator

from neo4j_ogm.queries.types import TAnyExcludeListDict


def _validate_properties(cls, values: dict[str, Any]) -> dict[str, Any]:
    for property_name, property_value in values.items():
        if not property_name.startswith("$"):
            try:
                validated = ExpressionValidator.parse_obj(property_value)
                values[property_name] = validated.dict(by_alias=True, exclude_none=True, exclude_unset=True)
                logging.debug("Validated property %s", property_name)
            except ValidationError:
                logging.debug("Omitting %s", property_name)

    return values


class PatternDirection(str, Enum):
    """
    Available relationship directions for pattern expressions.
    """

    INCOMING = "INCOMING"
    OUTGOING = "OUTGOING"
    BOTH = "BOTH"


class QueryOrder(str, Enum):
    """
    Available query orders.
    """

    ASC = "ASC"
    DESC = "DESC"


class QueryOptionsValidator(BaseModel):
    """
    Validation model for query options.
    """

    limit: Optional[int] = Field(default=None, ge=1)
    skip: Optional[int] = Field(default=None, ge=0)
    sort: Optional[Union[str, list[str]]] = None
    order: Optional[QueryOrder] = None

    class Config:
        """
        Pydantic configuration options.
        """

        use_enum_values = True


# Basic expression validators shared for both nodes and relationships
class StringComparisonValidator(BaseModel):
    """
    Validation model for string comparison operators defined in expressions.
    """

    contains_operator: Optional[str] = Field(alias="$contains", extra={"parser": "{property_name} CONTAINS ${value}"})
    starts_with_operator: Optional[str] = Field(
        alias="$startsWith", extra={"parser": "{property_name} STARTS WITH ${value}"}
    )
    ends_with_operator: Optional[str] = Field(alias="$endsWith", extra={"parser": "{property_name} ENDS WITH ${value}"})
    regex_operator: Optional[str] = Field(alias="$regex", extra={"parser": "{property_name} =~ ${value}"})


class ListComparisonValidator(BaseModel):
    """
    Validation model for list comparison operators defined in expressions.
    """

    in_operator: Optional[Union[List[TAnyExcludeListDict], TAnyExcludeListDict]] = Field(
        alias="$in", extra={"parser": "{property_name} IN {value}"}
    )


class NumericComparisonValidator(BaseModel):
    """
    Validation model for numerical comparison operators defined in expressions.
    """

    gt_operator: Optional[Union[int, float]] = Field(alias="$gt", extra={"parser": "{property_name} > ${value}"})
    gte_operator: Optional[Union[int, float]] = Field(alias="$gte", extra={"parser": "{property_name} >= ${value}"})
    lt_operator: Optional[Union[int, float]] = Field(alias="$lt", extra={"parser": "{property_name} < ${value}"})
    lte_operator: Optional[Union[int, float]] = Field(alias="$lte", extra={"parser": "{property_name} <= ${value}"})


class BaseComparisonValidator(BaseModel):
    """
    Validation model for general comparison operators defined in expressions.
    """

    eq_operator: Optional[TAnyExcludeListDict] = Field(alias="$eq", extra={"parser": "{property_name} = ${value}"})
    ne_operator: Optional[TAnyExcludeListDict] = Field(alias="$ne", extra={"parser": "NOT({property_name} = ${value})"})


class ComparisonValidator(
    NumericComparisonValidator,
    StringComparisonValidator,
    ListComparisonValidator,
    BaseComparisonValidator,
):
    """
    Validation model which combines all other comparison operators.
    """


class LogicalValidator(BaseModel):
    """
    Validation model for logical operators defined in expressions.
    """

    and_operator: Optional[List[Union["ExpressionValidator", "LogicalValidator"]]] = Field(
        alias="$and", extra={"parser": "AND"}
    )
    or_operator: Optional[List[Union["ExpressionValidator", "LogicalValidator"]]] = Field(
        alias="$or", extra={"parser": "OR"}
    )
    xor_operator: Optional[List[Union["ExpressionValidator", "LogicalValidator"]]] = Field(
        alias="$xor", extra={"parser": "XOR"}
    )


class ElementValidator(BaseModel):
    """
    Validation model for neo4j element operators defined in expressions.
    """

    element_id_operator: Optional[str] = Field(alias="$elementId", extra={"parser": "elementId({ref}) = ${value}"})
    id_operator: Optional[int] = Field(alias="$id", extra={"parser": "ID({ref}) = ${value}"})


class ExpressionValidator(LogicalValidator, ComparisonValidator):
    """
    Validation model which combines all other validators.
    """

    not_operator: Optional["ExpressionValidator"] = Field(alias="$not")
    all_operator: Optional[List["ExpressionValidator"]] = Field(alias="$all")
    size_operator: Optional[Union["NumericComparisonValidator", "BaseComparisonValidator"]] = Field(alias="$size")
    exists_operator: Optional[bool] = Field(alias="$exists")


class NodeValidator(ElementValidator):
    """
    Validation model for node pattern expressions.
    """

    labels_operator: Optional[List[str]] = Field(alias="$labels")
    pattern_operator: Optional[List["NodePatternValidator"]] = Field(alias="$pattern")

    property_validator = root_validator(allow_reuse=True)(_validate_properties)

    class Config:
        """
        Pydantic configurations.
        """

        extra = Extra.allow


class RelationshipValidator(ElementValidator):
    """
    Validation model for relationship pattern expressions.
    """

    type_operator: Optional[str] = Field(alias="$type")
    hops_operator: Optional[int] = Field(alias="$hops", default=1, gt=0)

    property_validator = root_validator(allow_reuse=True)(_validate_properties)

    class Config:
        """
        Pydantic configurations.
        """

        extra = Extra.allow


class NodePatternValidator(BaseModel):
    """
    Validation model for node patterns used in node queries.
    """

    node_operator: Optional["NodeValidator"] = Field(alias="$node")
    direction_operator: Optional["PatternDirection"] = Field(alias="$direction", default=PatternDirection.BOTH)
    relationship_operator: Optional["RelationshipValidator"] = Field(alias="$relationship")


class RelationshipPatternValidator(BaseModel):
    """
    Validation model for relationship patterns used in node queries.
    """

    start_node_operator: Optional[Dict[str, Any]] = Field(alias="$startNode")
    end_node_operator: Optional[Dict[str, Any]] = Field(alias="$endNode")
    direction_operator: Optional["PatternDirection"] = Field(alias="$direction", default=PatternDirection.BOTH)


# Update forward-refs
NodeValidator.update_forward_refs()
RelationshipValidator.update_forward_refs()
