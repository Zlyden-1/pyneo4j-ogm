"""
Clients module containing abstract base class for all clients and client implementations
for both Neo4j and Memgraph.
"""

import importlib.util
import inspect
import os
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from functools import wraps
from typing import (
    Any,
    AsyncGenerator,
    Callable,
    Dict,
    List,
    LiteralString,
    Optional,
    Self,
    Set,
    Tuple,
    Type,
    Union,
    cast,
)

from neo4j import AsyncDriver, AsyncGraphDatabase, AsyncSession, AsyncTransaction, Query

from pyneo4j_ogm.core.node import NodeModel
from pyneo4j_ogm.core.relationship import RelationshipModel
from pyneo4j_ogm.exceptions import (
    ClientNotInitializedError,
    ModelResolveError,
    NoTransactionInProgress,
    TransactionInProgress,
    UnsupportedDatabaseVersionError,
)
from pyneo4j_ogm.logger import logger
from pyneo4j_ogm.queries.query_builder import QueryBuilder
from pyneo4j_ogm.types.graph import EntityType
from pyneo4j_ogm.types.memgraph import (
    MemgraphConstraintType,
    MemgraphDataType,
    MemgraphDataTypeMapping,
    MemgraphIndexType,
)


def initialize_models_after(func):
    """
    Triggers model initialization for creating indexes/constraints and doing other setup work.

    Args:
        func (Callable): The function to be decorated, which can be either synchronous
            or asynchronous.

    Raises:
        ClientNotInitializedError: The client is not initialized yet.

    Returns:
        Callable: A wrapped function that includes additional functionality for both
            sync and async functions.
    """

    @wraps(func)
    async def wrapper(self, *args, **kwargs) -> None:
        if getattr(self, "_driver", None) is not None:
            initialize = cast(Optional[Callable], getattr(self, "_initialize_models", None))

            if initialize is None:
                raise ValueError("Model initialization function not found")

            await initialize()

        return await func(self, *args, **kwargs)

    return wrapper


def ensure_neo4j_version(major_version: int, minor_version: int, patch_version: int):
    """
    Ensures that the connected Neo4j database has a minimum version. Only usable for
    `Neo4jClient`.

    Args:
        major_version (int): The lowest allowed major version.
        minor_version (int): The lowest allowed minor version.
        patch_version (int): The lowest allowed patch version.
    """

    def decorator(func):

        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            logger.debug("Ensuring client has minimum required version")
            version = cast(Optional[str], getattr(self, "_version", None))
            if version is None:
                raise ClientNotInitializedError()

            major, minor, patch = [int(semver_partial) for semver_partial in version.split(".")]

            if major < major_version or minor < minor_version or patch < patch_version:
                raise UnsupportedDatabaseVersionError()

            result = await func(self, *args, **kwargs)
            return result

        return wrapper

    return decorator


def ensure_initialized(func):
    """
    Ensures the driver of the client is initialized before interacting with the database.

    Args:
        func (Callable): The function to be decorated.

    Raises:
        ClientNotInitializedError: The client is not initialized yet.

    Returns:
        A wrapped function that includes additional functionality.
    """

    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        logger.debug("Ensuring client is initialized")
        if getattr(self, "_driver", None) is None:
            raise ClientNotInitializedError()

        result = await func(self, *args, **kwargs)
        return result

    return wrapper


class Pyneo4jClient(ABC):
    """
    Base class for all client implementations.

    This client provides all basic functionality which all clients can use. Additionally, it also implements
    a interface for common methods all clients must implement in order to work with models. Methods for
    indexing/constraints are not added since Neo4j/Memgraph have differences in both how they do
    indexing/constraints and the existing types. To reduce complexity, which would be caused by generic methods,
    each client will implement it's own methods, which should follow a common naming scheme.
    """

    _driver: Optional[AsyncDriver]
    _session: Optional[AsyncSession]
    _transaction: Optional[AsyncTransaction]
    _models: Set[Union[Type[NodeModel], Type[RelationshipModel]]]
    _initialized_models: Set[Union[Type[NodeModel], Type[RelationshipModel]]]
    _skip_constraint_creation: bool
    _skip_index_creation: bool
    _using_batches: bool

    def __init__(self) -> None:
        super().__init__()

        logger.debug("Initializing client")

        self._driver = None
        self._session = None
        self._transaction = None
        self._models = set()
        self._initialized_models = set()
        self._skip_constraint_creation = False
        self._skip_index_creation = False
        self._using_batches = False

    @abstractmethod
    async def drop_constraints(self) -> Self:
        """
        Drops all existing constraints.
        """
        pass

    @abstractmethod
    async def drop_indexes(self) -> Self:
        """
        Drops all existing indexes.
        """
        pass

    @abstractmethod
    async def _check_database_version(self) -> None:
        """
        Checks if the connected database is running a supported version.

        Raises:
            UnsupportedDatabaseVersionError: Connected to a database with a unsupported version.
        """
        pass

    @abstractmethod
    async def _initialize_models(self) -> None:
        """
        Initializes all registered models by setting the defined indexes/constraints. This
        method has to be implemented by each client because of differences in index/constraint
        creation. All registered models have to be added to the `_initialized_models` set to
        allow tracking of models which have not been initialized yet.
        """
        pass

    async def connected(self) -> bool:
        """
        Checks if the client is already connected or not. If the client has been connected, but
        no connection can be established, or the authentication details are invalid, `False` is
        returned.

        Returns:
            bool: `True` if the client is connected and ready, otherwise `False`.
        """
        try:
            logger.info("Checking client connection and authentication")
            if self._driver is None:
                logger.debug("Client not initialized yet")
                return False

            logger.debug("Verifying connectivity to database")
            await self._driver.verify_connectivity()
            return True
        except Exception as exc:
            logger.error(exc)
            return False

    @initialize_models_after
    async def connect(
        self,
        uri: str,
        *args,
        skip_constraints: bool = False,
        skip_indexes: bool = False,
        **kwargs,
    ) -> Self:
        """
        Connects to the specified Neo4j/Memgraph database. This method also accepts the same arguments
        as the Neo4j Python driver.

        Args:
            uri (str): The URI to connect to.
            skip_constraints (bool): Whether to skip creating any constraints defined by models. Defaults
                to `False`.
            skip_indexes (bool): Whether to skip creating any indexes defined by models. Defaults to
                `False`.

        Returns:
            Self: The client.
        """
        self._skip_constraint_creation = skip_constraints
        self._skip_index_creation = skip_indexes

        logger.info("Connecting to database %s", uri)
        self._driver = AsyncGraphDatabase.driver(uri=uri, *args, **kwargs)

        logger.debug("Checking connectivity and authentication")
        await self._driver.verify_connectivity()

        logger.debug("Checking for compatible database version")
        await self._check_database_version()

        logger.info("%s connected to database", uri)
        return self

    @ensure_initialized
    async def close(self) -> None:
        """
        Closes the connection to the database.
        """
        logger.info("Closing database connection")
        await cast(AsyncDriver, self._driver).close()
        self._driver = None
        logger.info("Connection to database closed")

    @ensure_initialized
    async def cypher(
        self,
        query: Union[str, LiteralString, Query],
        parameters: Optional[Dict[str, Any]] = None,
        auto_committing: bool = False,
        resolve_models: bool = False,
        raise_on_resolve_exc: bool = False,
    ) -> Tuple[List[List[Any]], List[str]]:
        """
        Runs the defined Cypher query with the given parameters. Returned nodes/relationships
        can be resolved to `registered models` by settings the `resolve_models` parameter to `True`.
        By default, the model parsing will not raise an exception if it fails. This can be changed
        with the `raise_on_resolve_exc` parameter.

        **Note:** When using `Memgraph as a database`, some queries which have info reporting do not allow
        the usage of multicommand transactions. To still be able to run the query, you can set the
        `auto_committing` parameter to `True`. In doing so, the query will be run using a new session
        rather than a current transaction. This also meant that those queries `will not be batched` with others
        when using `with_batching`.

        Args:
            query (Union[str, LiteralString, Query]): Neo4j Query class or query string. Same as queries
                for the Neo4j driver.
            parameters (Optional[Dict[str, Any]]): Optional parameters used by the query. Same as parameters
                for the Neo4j driver. Defaults to `None`.
            auto_committing (bool): Whether to use session or transaction for running the query. Can be used for
                Memgraph queries using info reporting. Defaults to `false`.
            resolve_models (bool): Whether to attempt to resolve the nodes/relationships returned by the query
                to their corresponding models. Models must be registered for this to work. Defaults to `False`.
            raise_on_resolve_exc (bool): Whether to silently fail or raise a `ModelResolveError` error if resolving
                a node/relationship fails. Defaults to `False`.

        Returns:
            Tuple[List[List[Any]], List[str]]: A tuple containing the query result and the names of the returned
                variables.
        """
        query_parameters: Dict[str, Any] = {}

        if parameters is not None and isinstance(parameters, dict):
            query_parameters = parameters

        if auto_committing:
            return await self._with_auto_committing_transaction(
                query, query_parameters, resolve_models, raise_on_resolve_exc
            )
        else:
            return await self._with_implicit_transaction(query, query_parameters, resolve_models, raise_on_resolve_exc)

    @initialize_models_after
    async def register_models(self, models: List[Union[Type[NodeModel], Type[RelationshipModel]]]) -> Self:
        """
        Registers the provided models with the client. Can be omitted if automatic index/constraint creation
        and resolving models in queries is not required.

        Args:
            models (List[Union[Type[NodeModel], Type[RelationshipModel]]]): The models to register. Invalid model
                instances will be skipped during the registration.

        Returns:
            Self: The client.
        """
        logger.debug("Registering models with client")
        original_count = len(self._models)

        for model in models:
            if not issubclass(model, (NodeModel, RelationshipModel)):
                continue

            logger.debug("Registering model %s", model.__class__.__name__)
            self._models.add(model)

        current_count = len(self._models) - original_count
        logger.info("Registered %d models", current_count)

        return self

    @initialize_models_after
    async def register_models_directory(self, path: str) -> Self:
        """
        Recursively imports all discovered models from a given directory path and registers
        them with the client.

        Args:
            path (str): The path to the directory.

        Returns:
            Self: The client.
        """
        logger.debug("Registering models in directory %s", path)
        original_count = len(self._models)

        for root, _, files in os.walk(path):
            logger.debug("Checking %d files for models", len(files))
            for file in files:
                if not file.endswith(".py"):
                    continue

                filepath = os.path.join(root, file)

                logger.debug("Found file %s, importing", filepath)
                module_name = os.path.splitext(os.path.basename(filepath))[0]
                spec = importlib.util.spec_from_file_location(module_name, filepath)

                if spec is None or spec.loader is None:
                    raise ImportError(f"Could not import file {filepath}")

                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                for member in inspect.getmembers(
                    module,
                    lambda x: inspect.isclass(x)
                    and issubclass(x, (NodeModel, RelationshipModel))
                    and x is not NodeModel
                    and x is not RelationshipModel,
                ):
                    self._models.add(member[1])

        current_count = len(self._models) - original_count
        logger.info("Registered %d models", current_count)

        return self

    @asynccontextmanager
    async def batching(self) -> AsyncGenerator[None, Any]:
        """
        Batches all WRITE operations called inside this context manager into a single transaction. Inside
        the context, both client queries and model methods can be called.
        """
        try:
            self._using_batches = True
            logger.info("Starting batch transaction")
            await self._begin_transaction()

            yield None

            logger.info("Batching transaction finished")
            await self._commit_transaction()
        except Exception as exc:
            logger.error(exc)
            await self._rollback_transaction()
            raise exc
        finally:
            self._using_batches = False

    @ensure_initialized
    async def drop_nodes(self) -> Self:
        """
        Deletes all nodes and relationships.
        """
        logger.warning("Dropping all nodes and relationships")
        await self.cypher(f"MATCH {QueryBuilder.node_pattern("n")} DETACH DELETE n")

        logger.info("All nodes and relationships deleted")
        return self

    @ensure_initialized
    async def _begin_transaction(self) -> None:
        """
        Checks for existing sessions/transactions and begins new ones if none exist.

        Raises:
            TransactionInProgress: A session/transaction is already in progress.
        """
        if self._session is not None or self._transaction is not None:
            raise TransactionInProgress()

        logger.debug("Acquiring new session")
        self._session = cast(AsyncDriver, self._driver).session()
        logger.debug("Session %s acquired", self._session)

        logger.debug("Starting new transaction for session %s", self._session)
        self._transaction = await self._session.begin_transaction()
        logger.debug("Transaction %s for session %s acquired", self._transaction, self._session)

    @ensure_initialized
    async def _commit_transaction(self) -> None:
        """
        Commits the current transaction and closes it.

        Raises:
            NoTransactionInProgress: No active session/transaction to commit.
        """
        if self._session is None or self._transaction is None:
            raise NoTransactionInProgress()

        logger.debug("Committing transaction %s and closing session %s", self._transaction, self._session)
        await self._transaction.commit()
        self._transaction = None
        logger.debug("Transaction committed")

        await self._session.close()
        self._session = None
        logger.debug("Session closed")

    @ensure_initialized
    async def _rollback_transaction(self) -> None:
        """
        Rolls the current transaction back and closes it.

        Raises:
            NoTransactionInProgress: No active session/transaction to roll back.
        """
        if self._session is None or self._transaction is None:
            raise NoTransactionInProgress()

        logger.debug("Rolling back transaction %s and closing session %s", self._transaction, self._session)
        await self._transaction.rollback()
        self._transaction = None
        logger.debug("Transaction rolled back")

        await self._session.close()
        self._session = None
        logger.debug("Session closed")

    async def _with_implicit_transaction(
        self,
        query: Union[str, LiteralString, Query],
        parameters: Dict[str, Any],
        resolve_models: bool,
        raise_on_resolve_exc: bool,
    ) -> Tuple[List[List[Any]], List[str]]:
        """
        Runs a query with manually handled transactions, allowing for batching and finer control over
        committing/rollbacks.

        Args:
            query (Union[str, LiteralString, Query]): Neo4j Query class or query string. Same as queries
                for the Neo4j driver.
            parameters (Optional[Dict[str, Any]]): Optional parameters used by the query. Same as parameters
                for the Neo4j driver. Defaults to `None`.
            resolve_models (bool): Whether to attempt to resolve the nodes/relationships returned by the query
                to their corresponding models. Models must be registered for this to work. Defaults to `False`.
            raise_on_resolve_exc (bool): Whether to silently fail or raise a `ModelResolveError` error if resolving
                a node/relationship fails. Defaults to `False`.

        Raises:
            ModelResolveError: `raise_on_resolve_exc` is set to `True` and resolving a result fails.

        Returns:
            Tuple[List[List[Any]], List[str]]: A tuple containing the query result and the names of the returned
                variables.
        """
        if not self._using_batches:
            # If we are currently using batching, we should already be inside a active session/transaction
            await self._begin_transaction()

        try:
            logger.info("%s with parameters %s", query, parameters)
            query_result = await cast(AsyncTransaction, self._transaction).run(cast(LiteralString, query), parameters)

            logger.debug("Parsing query results")
            results = [list(result.values()) async for result in query_result]
            keys = list(query_result.keys())

            if resolve_models:
                try:
                    # TODO: Try to resolve models and raise an exception depending on the parameters provided
                    pass
                except Exception as exc:
                    logger.warning("Resolving models failed with %s", exc)
                    if raise_on_resolve_exc:
                        raise ModelResolveError() from exc

            if not self._using_batches:
                # Again, don't commit anything to the database when batching is enabled
                await self._commit_transaction()

            return results, keys
        except Exception as exc:
            logger.error("Query exception: %s", exc)

            if not self._using_batches:
                # Same as in the beginning, we don't want to roll back anything if we use batching
                await self._rollback_transaction()

            raise exc

    async def _with_auto_committing_transaction(
        self,
        query: Union[str, LiteralString, Query],
        parameters: Dict[str, Any],
        resolve_models: bool,
        raise_on_resolve_exc: bool,
    ) -> Tuple[List[List[Any]], List[str]]:
        """
        Runs a auto-committing query using a session rather than a transaction. This has to be used
        with some Memgraph queries due to some restrictions, though this mainly concerns queries
        with info reporting (`SHOW INDEX INFO` for example).

        Args:
            query (Union[str, LiteralString, Query]): Neo4j Query class or query string. Same as queries
                for the Neo4j driver.
            parameters (Optional[Dict[str, Any]]): Optional parameters used by the query. Same as parameters
                for the Neo4j driver. Defaults to `None`.
            resolve_models (bool): Whether to attempt to resolve the nodes/relationships returned by the query
                to their corresponding models. Models must be registered for this to work. Defaults to `False`.
            raise_on_resolve_exc (bool): Whether to silently fail or raise a `ModelResolveError` error if resolving
                a node/relationship fails. Defaults to `False`.

        Raises:
            ModelResolveError: `raise_on_resolve_exc` is set to `True` and resolving a result fails.

        Returns:
            Tuple[List[List[Any]], List[str]]: A tuple containing the query result and the names of the returned
                variables.
        """
        try:
            logger.debug("Acquiring new session")
            session = cast(AsyncDriver, self._driver).session()
            logger.debug("Session %s acquired", session)

            logger.info("%s with parameters %s", query, parameters)
            query_result = await session.run(cast(LiteralString, query), parameters)

            logger.debug("Parsing query results")
            results = [list(result.values()) async for result in query_result]
            keys = list(query_result.keys())

            if resolve_models:
                try:
                    # TODO: Try to resolve models and raise an exception depending on the parameters provided
                    pass
                except Exception as exc:
                    logger.warning("Resolving models failed with %s", exc)
                    if raise_on_resolve_exc:
                        raise ModelResolveError() from exc

            logger.debug("Closing session %s", session)
            await session.close()
            logger.debug("Session closed")

            return results, keys
        except Exception as exc:
            logger.error("Query exception: %s", exc)
            raise exc


class Neo4jClient(Pyneo4jClient):
    """
    Neo4j client used for interacting with a Neo4j database. Provides basic functionality for querying, indexing,
    constraints and other utilities.
    """

    _version: Optional[str]

    @ensure_initialized
    async def drop_constraints(self) -> Self:
        logger.debug("Discovering constraints")
        constraints, _ = await self.cypher("SHOW CONSTRAINTS")

        if len(constraints) == 0:
            return self

        logger.warning("Dropping %d constraints", len(constraints))
        for constraint in constraints:
            logger.debug("Dropping constraint %s", constraint[1])
            await self.cypher(f"DROP CONSTRAINT {constraint[1]}")

        logger.info("%d constraints dropped", len(constraints))
        return self

    @ensure_initialized
    async def drop_indexes(self) -> Self:
        logger.debug("Discovering indexes")
        indexes, _ = await self.cypher("SHOW INDEXES")

        if len(indexes) == 0:
            return self

        logger.warning("Dropping %d indexes", len(indexes))
        for index in indexes:
            logger.debug("Dropping index %s", index[1])
            await self.cypher(f"DROP INDEX {index[1]}")

        logger.info("%d indexes dropped", len(indexes))
        return self

    @ensure_initialized
    async def uniqueness_constraint(
        self,
        name: str,
        entity_type: EntityType,
        label_or_type: str,
        properties: Union[List[str], str],
        raise_on_existing: bool = False,
    ) -> Self:
        """
        Creates a uniqueness constraint for a given node or relationship. By default, this will use `IF NOT EXISTS`
        when creating constraints to prevent errors if the constraint already exists. This behavior can be changed by
        passing `raise_on_existing` as `True`.

        Args:
            name (str): The name of the constraint.
            entity_type (EntityType): The type of graph entity for which the constraint will be created.
            label_or_type (str): When creating a constraint for a node, the label on which the constraint will be created.
                In case of a relationship, the relationship type.
            properties (Union[List[str], str]): The properties which should be affected by the constraint.
            raise_on_existing (bool): Whether to use `IF NOT EXISTS` to prevent errors when creating duplicate constraints.
                Defaults to `False`.

        Returns:
            Self: The client.
        """
        logger.info("Creating uniqueness constraint %s on %s", name, label_or_type)
        normalized_properties = [properties] if isinstance(properties, str) else properties

        existence_pattern = "" if raise_on_existing else " IF NOT EXISTS"

        if entity_type == EntityType.NODE:
            entity_pattern = QueryBuilder.node_pattern("e", label_or_type)
        else:
            entity_pattern = QueryBuilder.relationship_pattern("e", label_or_type)

        if len(normalized_properties) == 1:
            properties_pattern = f"e.{normalized_properties[0]}"
        else:
            properties_pattern = f"({', '.join([f'e.{property_}' for property_ in normalized_properties])})"

        logger.debug("Creating uniqueness constraint for %s on properties %s", label_or_type, properties_pattern)
        await self.cypher(
            f"CREATE CONSTRAINT {name}{existence_pattern} FOR {entity_pattern} REQUIRE {properties_pattern} IS UNIQUE"
        )

        return self

    @ensure_initialized
    async def range_index(
        self,
        name: str,
        label_or_type: str,
        entity_type: EntityType,
        properties: Union[List[str], str],
        raise_on_existing: bool = False,
    ) -> Self:
        """
        Creates a range index for the given node or relationship. By default, this will se `IF NOT EXISTS`
        when creating indexes to prevent errors if the index already exists. This behavior can be
        changed by passing `raise_on_existing` as `True`.

        Args:
            name (str): The name of the index.
            entity_type (EntityType): The type of graph entity for which the index will be created.
            label_or_type (str): When creating a index for a node, the label on which the index will be created.
                In case of a relationship, the relationship type.
            properties (Union[List[str], str]): The properties which should be affected by the index.
            raise_on_existing (bool): Whether to use `IF NOT EXISTS` to prevent errors when creating duplicate indexes.
                Defaults to `False`.

        Returns:
            Self: The client.
        """
        logger.info("Creating range index %s for %s", name, label_or_type)
        normalized_properties = [properties] if isinstance(properties, str) else properties

        existence_pattern = "" if raise_on_existing else " IF NOT EXISTS"
        properties_pattern = ", ".join(f"e.{property_}" for property_ in normalized_properties)

        if entity_type == EntityType.NODE:
            entity_pattern = QueryBuilder.node_pattern("e", label_or_type)
        else:
            entity_pattern = QueryBuilder.relationship_pattern("e", label_or_type)

        logger.debug("Creating range index for %s on properties %s", label_or_type, properties_pattern)
        await self.cypher(f"CREATE INDEX {name}{existence_pattern} FOR {entity_pattern} ON ({properties_pattern})")

        return self

    @ensure_initialized
    async def text_index(
        self,
        name: str,
        label_or_type: str,
        entity_type: EntityType,
        property_: str,
        raise_on_existing: bool = False,
    ) -> Self:
        """
        Creates a text index for the given node or relationship. By default, this will se `IF NOT EXISTS`
        when creating indexes to prevent errors if the index already exists. This behavior can be
        changed by passing `raise_on_existing` as `True`.

        Args:
            name (str): The name of the index.
            entity_type (EntityType): The type of graph entity for which the index will be created.
            label_or_type (str): When creating a index for a node, the label on which the index will be created.
                In case of a relationship, the relationship type.
            property_ (str): The property which should be affected by the index.
            raise_on_existing (bool): Whether to use `IF NOT EXISTS` to prevent errors when creating duplicate indexes.
                Defaults to `False`.

        Returns:
            Self: The client.
        """
        logger.info("Creating text index %s for %s", name, label_or_type)
        existence_pattern = "" if raise_on_existing else " IF NOT EXISTS"

        if entity_type == EntityType.NODE:
            entity_pattern = QueryBuilder.node_pattern("e", label_or_type)
        else:
            entity_pattern = QueryBuilder.relationship_pattern("e", label_or_type)

        logger.debug("Creating text index for %s on property %s", label_or_type, property_)
        await self.cypher(f"CREATE TEXT INDEX {name}{existence_pattern} FOR {entity_pattern} ON (e.{property_})")

        return self

    @ensure_initialized
    async def point_index(
        self,
        name: str,
        label_or_type: str,
        entity_type: EntityType,
        property_: str,
        raise_on_existing: bool = False,
    ) -> Self:
        """
        Creates a point index for the given node or relationship. By default, this will se `IF NOT EXISTS`
        when creating indexes to prevent errors if the index already exists. This behavior can be
        changed by passing `raise_on_existing` as `True`.

        Args:
            name (str): The name of the index.
            entity_type (EntityType): The type of graph entity for which the index will be created.
            label_or_type (str): When creating a index for a node, the label on which the index will be created.
                In case of a relationship, the relationship type.
            property_ (str): The property which should be affected by the index.
            raise_on_existing (bool): Whether to use `IF NOT EXISTS` to prevent errors when creating duplicate indexes.
                Defaults to `False`.

        Returns:
            Self: The client.
        """
        logger.info("Creating point index %s for %s", name, label_or_type)
        existence_pattern = "" if raise_on_existing else " IF NOT EXISTS"

        if entity_type == EntityType.NODE:
            entity_pattern = QueryBuilder.node_pattern("e", label_or_type)
        else:
            entity_pattern = QueryBuilder.relationship_pattern("e", label_or_type)

        logger.debug("Creating point index for %s on property %s", label_or_type, property_)
        await self.cypher(f"CREATE POINT INDEX {name}{existence_pattern} FOR {entity_pattern} ON (e.{property_})")

        return self

    @ensure_initialized
    async def fulltext_index(
        self,
        name: str,
        labels_or_types: Union[List[str], str],
        entity_type: EntityType,
        properties: Union[List[str], str],
        raise_on_existing: bool = False,
    ) -> Self:
        """
        Creates a fulltext index for the given node or relationship. By default, this will se `IF NOT EXISTS`
        when creating indexes to prevent errors if the index already exists. This behavior can be
        changed by passing `raise_on_existing` as `True`.

        Args:
            name (str): The name of the index.
            entity_type (EntityType): The type of graph entity for which the index will be created.
            labels_or_types (Union[List[str], str]): When creating a index for a node, the labels on which the index will be created.
                In case of a relationship, the relationship types.
            properties (Union[List[str], str]): The properties which should be affected by the index.
            raise_on_existing (bool): Whether to use `IF NOT EXISTS` to prevent errors when creating duplicate indexes.
                Defaults to `False`.

        Returns:
            Self: The client.
        """
        logger.info("Creating fulltext index %s for %s", name, labels_or_types)
        normalized_properties = [properties] if isinstance(properties, str) else properties

        existence_pattern = "" if raise_on_existing else " IF NOT EXISTS"
        properties_pattern = ", ".join(f"e.{property_}" for property_ in normalized_properties)

        if entity_type == EntityType.NODE:
            entity_pattern = QueryBuilder.node_pattern("e", labels_or_types, True)
        else:
            entity_pattern = QueryBuilder.relationship_pattern("e", labels_or_types)

        logger.debug("Creating fulltext index for %s on properties %s", labels_or_types, properties_pattern)
        await self.cypher(
            f"CREATE FULLTEXT INDEX {name}{existence_pattern} FOR {entity_pattern} ON EACH [{properties_pattern}]"
        )

        return self

    @ensure_initialized
    @ensure_neo4j_version(5, 18, 0)
    async def vector_index(
        self,
        name: str,
        label_or_type: str,
        entity_type: EntityType,
        property_: str,
        raise_on_existing: bool = False,
    ) -> Self:
        """
        Creates a vector index for the given node or relationship. By default, this will se `IF NOT EXISTS`
        when creating indexes to prevent errors if the index already exists. This behavior can be
        changed by passing `raise_on_existing` as `True`.

        Args:
            name (str): The name of the index.
            entity_type (EntityType): The type of graph entity for which the index will be created.
            label_or_type (str): When creating a index for a node, the label on which the index will be created.
                In case of a relationship, the relationship type.
            property_ (str): The property which should be affected by the index.
            raise_on_existing (bool): Whether to use `IF NOT EXISTS` to prevent errors when creating duplicate indexes.
                Defaults to `False`.

        Returns:
            Self: The client.
        """
        logger.info("Creating vector index %s for %s", name, label_or_type)
        existence_pattern = "" if raise_on_existing else " IF NOT EXISTS"

        if entity_type == EntityType.NODE:
            entity_pattern = QueryBuilder.node_pattern("e", label_or_type)
        else:
            entity_pattern = QueryBuilder.relationship_pattern("e", label_or_type)

        logger.debug("Creating vector index for %s on property %s", label_or_type, property_)
        await self.cypher(f"CREATE VECTOR INDEX {name}{existence_pattern} FOR {entity_pattern} ON (e.{property_})")

        return self

    @ensure_initialized
    async def _check_database_version(self) -> None:
        logger.debug("Checking if Neo4j version is supported")
        server_info = await cast(AsyncDriver, self._driver).get_server_info()

        version = server_info.agent.split("/")[1]
        self._version = version

        if int(version.split(".")[0]) < 5:
            raise UnsupportedDatabaseVersionError()

    @ensure_initialized
    async def _initialize_models(self) -> None:
        pass


class MemgraphClient(Pyneo4jClient):
    """
    Memgraph client used for interacting with a Memgraph database. Provides basic functionality for querying, indexing,
    constraints and other utilities.
    """

    async def drop_constraints(self) -> Self:
        logger.debug("Discovering constraints")
        constraints, _ = await self.cypher("SHOW CONSTRAINT INFO", auto_committing=True)

        if len(constraints) == 0:
            return self

        logger.warning("Dropping %d constraints", len(constraints))
        for constraint in constraints:
            match constraint[0]:
                case MemgraphConstraintType.EXISTS.value:
                    await self.cypher(
                        f"DROP CONSTRAINT ON (n:{constraint[1]}) ASSERT EXISTS (n.{constraint[2]})",
                        auto_committing=True,
                    )
                case MemgraphConstraintType.UNIQUE.value:
                    await self.cypher(
                        f"DROP CONSTRAINT ON (n:{constraint[1]}) ASSERT {', '.join([f'n.{constraint_property}' for constraint_property in constraint[2]])} IS UNIQUE",
                        auto_committing=True,
                    )
                case MemgraphConstraintType.DATA_TYPE.value:
                    # Some data types in Memgraph are returned differently that what is used when creating them
                    # Because of that we have to do some additional mapping when dropping them
                    await self.cypher(
                        f"DROP CONSTRAINT ON (n:{constraint[1]}) ASSERT n.{constraint[2]} IS TYPED {MemgraphDataTypeMapping[constraint[3]]}",
                        auto_committing=True,
                    )

        logger.info("%d constraints dropped", len(constraints))
        return self

    async def drop_indexes(self) -> Self:
        logger.debug("Discovering indexes")
        indexes, _ = await self.cypher("SHOW INDEX INFO", auto_committing=True)

        if len(indexes) == 0:
            return self

        logger.warning("Dropping %d indexes", len(indexes))
        for index in indexes:
            match index[0]:
                case MemgraphIndexType.EDGE_TYPE.value:
                    await self.cypher(f"DROP EDGE INDEX ON :{index[1]}", auto_committing=True)
                case MemgraphIndexType.EDGE_TYPE_AND_PROPERTY.value:
                    await self.cypher(f"DROP EDGE INDEX ON :{index[1]}({index[2]})", auto_committing=True)
                case MemgraphIndexType.LABEL.value:
                    await self.cypher(f"DROP INDEX ON :{index[1]}", auto_committing=True)
                case MemgraphIndexType.LABEL_AND_PROPERTY.value:
                    await self.cypher(f"DROP INDEX ON :{index[1]}({index[2]})", auto_committing=True)
                case MemgraphIndexType.POINT.value:
                    await self.cypher(f"DROP POINT INDEX ON :{index[1]}({index[2]})", auto_committing=True)

        logger.info("%d indexes dropped", len(indexes))
        return self

    @ensure_initialized
    async def existence_constraint(self, label: str, properties: Union[List[str], str]) -> Self:
        """
        Creates a new existence constraint for a node with a given label. Can only be used to create existence constraints
        on nodes.

        Args:
            label (str): The label on which the constraint will be created.
            properties (Union[List[str], str]): The properties which should be affected by the constraint.

        Returns:
            Self: The client.
        """
        logger.info("Creating existence constraint on %s", label)
        normalized_properties = [properties] if isinstance(properties, str) else properties
        node_pattern = QueryBuilder.node_pattern("n", label)

        for property_ in normalized_properties:
            logger.debug("Creating existence constraint for %s on property %s", label, property_)
            await self.cypher(
                f"CREATE CONSTRAINT ON {node_pattern} ASSERT EXISTS (n.{property_})", auto_committing=True
            )

        return self

    @ensure_initialized
    async def uniqueness_constraint(self, label: str, properties: Union[List[str], str]) -> Self:
        """
        Creates a new uniqueness constraint for a node with a given label. Can only be used to create uniqueness constraints
        on nodes.

        Args:
            label (str): The label on which the constraint will be created.
            properties (Union[List[str], str]): The properties which should be affected by the constraint.

        Returns:
            Self: The client.
        """
        logger.info("Creating uniqueness constraint on %s", label)
        normalized_properties = [properties] if isinstance(properties, str) else properties

        node_pattern = QueryBuilder.node_pattern("n", label)
        property_pattern = ", ".join(f"n.{property_}" for property_ in normalized_properties)

        logger.debug("Creating uniqueness constraint for %s on properties %s", label, property_pattern)
        await self.cypher(
            f"CREATE CONSTRAINT ON {node_pattern} ASSERT {property_pattern} IS UNIQUE", auto_committing=True
        )

        return self

    @ensure_initialized
    async def data_type_constraint(
        self, label: str, properties: Union[List[str], str], data_type: MemgraphDataType
    ) -> Self:
        """
        Creates a new data type constraint for a node with a given label. Can only be used to create data type constraints
        on nodes.

        Args:
            label (str): The label on which the constraint will be created.
            properties (Union[List[str], str]): The properties which should be affected by the constraint.
            data_type (MemgraphDataType): The data type to enforce.

        Raises:
            ClientError: If a data type constraint already exists on the label-property pair.

        Returns:
            Self: The client.
        """
        logger.info("Creating data type constraint on %s for type %s", label, data_type.value)
        normalized_properties = [properties] if isinstance(properties, str) else properties

        node_pattern = QueryBuilder.node_pattern("n", label)

        for property_ in normalized_properties:
            logger.debug("Creating data type constraint for %s on property %s", label, property_)
            await self.cypher(
                f"CREATE CONSTRAINT ON {node_pattern} ASSERT n.{property_} IS TYPED {data_type.value}",
                auto_committing=True,
            )

        return self

    @ensure_initialized
    async def index(self, label_or_edge: str, entity_type: EntityType) -> Self:
        """
        Creates a label/edge index.

        Args:
            label_or_edge (str): Label/edge in which the index is created.
            entity_type (EntityType): The type of graph entity for which the index will be created.

        Returns:
            Self: The client.
        """
        logger.info("Creating %s index for %s", "label" if entity_type == EntityType.NODE else "edge", label_or_edge)
        await self.cypher(
            f"CREATE {'EDGE ' if entity_type == EntityType.RELATIONSHIP else ''} INDEX ON :{label_or_edge}",
            auto_committing=True,
        )

        return self

    @ensure_initialized
    async def property_index(self, label_or_edge: str, entity_type: EntityType, property_: str) -> Self:
        """
        Creates a label/property or edge/property pair index.

        Args:
            label_or_edge (str): Label/edge in which the index is created.
            entity_type (EntityType): The type of graph entity for which the index will be created.
            property_ (str): The property which should be affected by the index.

        Returns:
            Self: The client.
        """
        logger.info(
            "Creating %s pair index for %s on %s",
            "label" if entity_type == EntityType.NODE else "edge",
            label_or_edge,
            property_,
        )
        await self.cypher(
            f"CREATE {'EDGE ' if entity_type == EntityType.RELATIONSHIP else ''} INDEX ON :{label_or_edge}({property_})",
            auto_committing=True,
        )

        return self

    @ensure_initialized
    async def point_index(self, label: str, property_: str) -> Self:
        """
        Creates a point index.

        Args:
            label (str): Label/edge in which the index is created.
            property_ (str): The property which should be affected by the index.

        Returns:
            Self: The client.
        """
        logger.info(
            "Creating point index for %s on %s",
            label,
            property_,
        )
        await self.cypher(f"CREATE POINT INDEX ON :{label}({property_})", auto_committing=True)

        return self

    @ensure_initialized
    async def _check_database_version(self) -> None:
        # I'm not sure if we actually need/can to check anything here since the server info
        # only states 'Neo4j/v5.11.0 compatible graph database server - Memgraph'
        pass

    @ensure_initialized
    async def _initialize_models(self) -> None:
        pass
