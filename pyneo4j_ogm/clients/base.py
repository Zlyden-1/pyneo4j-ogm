import importlib.util
import inspect
import os
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from functools import wraps
from time import perf_counter
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

from pyneo4j_ogm.exceptions import (
    ClientNotInitializedError,
    ModelResolveError,
    NoTransactionInProgress,
    TransactionInProgress,
)
from pyneo4j_ogm.logger import logger
from pyneo4j_ogm.models.node import NodeModel
from pyneo4j_ogm.models.relationship import RelationshipModel
from pyneo4j_ogm.queries.query_builder import QueryBuilder
from pyneo4j_ogm.registry import Registry


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
        result = await func(self, *args, **kwargs)

        if await self.connected():
            initialize_func = cast(Optional[Callable], getattr(self, "_initialize_models", None))
            if initialize_func is None:
                raise ValueError("Model initialization function not found")

            await initialize_func()

        return result

    return wrapper


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

    _registry: Registry = Registry()
    _driver: Optional[AsyncDriver]
    _session: Optional[AsyncSession]
    _transaction: Optional[AsyncTransaction]
    _using_batches: bool
    _models: Set[Union[Type[NodeModel], Type[RelationshipModel]]]
    _skip_constraint_creation: bool
    _skip_index_creation: bool

    def __init__(self) -> None:
        super().__init__()

        logger.debug("Initializing client")

        self._driver = None
        self._session = None
        self._transaction = None
        self._models = set()
        self._skip_constraint_creation = False
        self._skip_index_creation = False
        self._using_batches = False

        self._registry.register(self)

    @abstractmethod
    async def drop_constraints(self) -> Self:
        """
        Drops all existing constraints.
        """
        pass  # pragma: no cover

    @abstractmethod
    async def drop_indexes(self) -> Self:
        """
        Drops all existing indexes.
        """
        pass  # pragma: no cover

    @abstractmethod
    async def _check_database_version(self) -> None:
        """
        Checks if the connected database is running a supported version.

        Raises:
            UnsupportedDatabaseVersionError: Connected to a database with a unsupported version.
        """
        pass  # pragma: no cover

    @abstractmethod
    async def _initialize_models(self) -> None:
        """
        Initializes all registered models by setting the defined indexes/constraints. This
        method has to be implemented by each client because of differences in index/constraint
        creation.
        """
        pass  # pragma: no cover

    async def connected(self) -> bool:
        """
        Checks if the client is already connected or not. If the client has been connected, but
        no connection can be established, or the authentication details are invalid, `False` is
        returned.

        Returns:
            bool: `True` if the client is connected and ready, otherwise `False`.
        """
        try:
            logger.debug("Checking client connection and authentication")
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

        logger.info("Connected to %s", uri)
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
            return await self.__with_auto_committing_transaction(
                query, query_parameters, resolve_models, raise_on_resolve_exc
            )
        else:
            return await self.__with_implicit_transaction(query, query_parameters, resolve_models, raise_on_resolve_exc)

    @initialize_models_after
    async def register_models(self, *args: Union[Type[NodeModel], Type[RelationshipModel]]) -> Self:
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

        for model in args:
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
            await self.__begin_transaction()

            yield None

            logger.info("Batching transaction finished")
            await self.__commit_transaction()
        except Exception as exc:
            logger.error(exc)
            await self.__rollback_transaction()
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
    async def __begin_transaction(self) -> None:
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
    async def __commit_transaction(self) -> None:
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
    async def __rollback_transaction(self) -> None:
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

    async def __with_implicit_transaction(
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
            await self.__begin_transaction()

        try:
            logger.info("'%s' with parameters %s", query, parameters)
            query_start = perf_counter()
            query_result = await cast(AsyncTransaction, self._transaction).run(cast(LiteralString, query), parameters)
            query_duration = (perf_counter() - query_start) * 1000

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

            summary = await query_result.consume()
            logger.info("Query finished after %dms", summary.result_available_after or query_duration)

            if not self._using_batches:
                # Again, don't commit anything to the database when batching is enabled
                await self.__commit_transaction()

            return results, keys
        except Exception as exc:
            logger.error("Query exception: %s", exc)

            if not self._using_batches:
                # Same as in the beginning, we don't want to roll back anything if we use batching
                await self.__rollback_transaction()

            raise exc

    async def __with_auto_committing_transaction(
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

            logger.info("'%s' with parameters %s", query, parameters)
            query_start = perf_counter()
            query_result = await session.run(cast(LiteralString, query), parameters)
            query_duration = (perf_counter() - query_start) * 1000

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

            summary = await query_result.consume()
            logger.info("Query finished after %dms", summary.result_available_after or query_duration)

            logger.debug("Closing session %s", session)
            await session.close()
            logger.debug("Session closed")

            return results, keys
        except Exception as exc:
            logger.error("Query exception: %s", exc)
            raise exc
