"""
This module contains possible settings for NodeModels and RelationshipModels
"""
from typing import Callable, Dict, List, Optional, Set, Union

from pydantic import BaseModel


class BaseModelSettings(BaseModel):
    """
    Shared settings for NodeModel and RelationshipModel classes or subclasses.
    """

    auto_fetch_nodes: bool = False
    exclude_from_export: Set[str] = set()
    pre_hooks: Dict[str, Union[List[Callable], Callable]] = {}
    post_hooks: Dict[str, Union[List[Callable], Callable]] = {}


class NodeModelSettings(BaseModelSettings):
    """
    Settings for a NodeModel class.
    """

    labels: Optional[Union[Set[str], str]] = None


class RelationshipModelSettings(BaseModelSettings):
    """
    Settings for a RelationshipModel class.
    """

    type: Optional[str] = None
