from functools import wraps
from types import TracebackType
from typing import (
    ClassVar,
    Optional,
    Sequence,
    List,
    Dict,
    Type,
    Iterable,
    Any,
    IO,
    Tuple,
    cast,
)

from dlt.common.destination.exceptions import (
    DestinationUndefinedEntity,
    DestinationTransientException,
    DestinationTerminalException,
)

import weaviate
from weaviate.gql.get import GetBuilder
from weaviate.util import generate_uuid5

from dlt.common import logger
from dlt.common.json import json
from dlt.common.pendulum import pendulum
from dlt.common.typing import StrAny, TFun
from dlt.common.time import ensure_pendulum_datetime
from dlt.common.schema import Schema, TTableSchema, TSchemaTables, TTableSchemaColumns
from dlt.common.schema.typing import TColumnSchema, TColumnType
from dlt.common.schema.utils import get_columns_names_with_prop
from dlt.common.destination import DestinationCapabilitiesContext
from dlt.common.destination.reference import TLoadJobState, LoadJob, JobClientBase, WithStateSync
from dlt.common.data_types import TDataType
from dlt.common.storages import FileStorage

from dlt.destinations.impl.weaviate.weaviate_adapter import VECTORIZE_HINT, TOKENIZATION_HINT

from dlt.destinations.job_impl import EmptyLoadJob
from dlt.destinations.job_client_impl import StorageSchemaInfo, StateInfo
from dlt.destinations.impl.weaviate import capabilities
from dlt.destinations.impl.weaviate.configuration import WeaviateClientConfiguration
from dlt.destinations.impl.weaviate.exceptions import PropertyNameConflict, WeaviateBatchError
from dlt.destinations.type_mapping import TypeMapper


NON_VECTORIZED_CLASS = {
    "vectorizer": "none",
    "vectorIndexConfig": {
        "skip": True,
    },
}


class WeaviateTypeMapper(TypeMapper):
    sct_to_unbound_dbt = {
        "text": "text",
        "double": "number",
        "bool": "boolean",
        "timestamp": "date",
        "date": "date",
        "time": "text",
        "bigint": "int",
        "binary": "blob",
        "decimal": "text",
        "wei": "number",
        "complex": "text",
    }

    sct_to_dbt = {}

    dbt_to_sct = {
        "text": "text",
        "number": "double",
        "boolean": "bool",
        "date": "timestamp",
        "int": "bigint",
        "blob": "binary",
    }


def wrap_weaviate_error(f: TFun) -> TFun:
    @wraps(f)
    def _wrap(self: JobClientBase, *args: Any, **kwargs: Any) -> Any:
        try:
            return f(self, *args, **kwargs)
        # those look like terminal exceptions
        except (
            weaviate.exceptions.ObjectAlreadyExistsException,
            weaviate.exceptions.SchemaValidationException,
            weaviate.exceptions.WeaviateEmbeddedInvalidVersion,
        ) as term_ex:
            raise DestinationTerminalException(term_ex) from term_ex
        except weaviate.exceptions.UnexpectedStatusCodeException as status_ex:
            # special handling for non existing objects/classes
            if status_ex.status_code == 404:
                raise DestinationUndefinedEntity(status_ex)
            if status_ex.status_code == 403:
                raise DestinationTerminalException(status_ex)
            if status_ex.status_code == 422:
                if "conflict for property" in str(status_ex) or "none vectorizer module" in str(
                    status_ex
                ):
                    raise PropertyNameConflict()
                raise DestinationTerminalException(status_ex)
            # looks like there are no more terminal exception
            raise DestinationTransientException(status_ex)
        except weaviate.exceptions.WeaviateBaseError as we_ex:
            # also includes 401 as transient
            raise DestinationTransientException(we_ex)

    return _wrap  # type: ignore


def wrap_batch_error(f: TFun) -> TFun:
    @wraps(f)
    def _wrap(*args: Any, **kwargs: Any) -> Any:
        try:
            return f(*args, **kwargs)
        # those look like terminal exceptions
        except WeaviateBatchError as batch_ex:
            errors = batch_ex.args[0]
            message = errors["error"][0]["message"]
            # TODO: actually put the job in failed/retry state and prepare exception message with full info on failing item
            if "invalid" in message and "property" in message and "on class" in message:
                raise DestinationTerminalException(
                    f"Batch failed {errors} AND WILL **NOT** BE RETRIED"
                )
            if "conflict for property" in message:
                raise PropertyNameConflict()
            raise DestinationTransientException(f"Batch failed {errors} AND WILL BE RETRIED")
        except Exception:
            raise DestinationTransientException("Batch failed AND WILL BE RETRIED")

    return _wrap  # type: ignore


class LoadWeaviateJob(LoadJob):
    def __init__(
        self,
        schema: Schema,
        table_schema: TTableSchema,
        local_path: str,
        db_client: weaviate.Client,
        client_config: WeaviateClientConfiguration,
        class_name: str,
    ) -> None:
        file_name = FileStorage.get_file_name_from_file_path(local_path)
        super().__init__(file_name)
        self.client_config = client_config
        self.db_client = db_client
        self.table_name = table_schema["name"]
        self.class_name = class_name
        self.unique_identifiers = self.list_unique_identifiers(table_schema)
        self.complex_indices = [
            i
            for i, field in schema.get_table_columns(self.table_name).items()
            if field["data_type"] == "complex"
        ]
        self.date_indices = [
            i
            for i, field in schema.get_table_columns(self.table_name).items()
            if field["data_type"] == "date"
        ]
        with FileStorage.open_zipsafe_ro(local_path) as f:
            self.load_batch(f)

    @wrap_weaviate_error
    def load_batch(self, f: IO[str]) -> None:
        """Load all the lines from stream `f` in automatic Weaviate batches.
        Weaviate batch supports retries so we do not need to do that.
        """

        @wrap_batch_error
        def check_batch_result(results: List[StrAny]) -> None:
            """This kills batch on first error reported"""
            if results is not None:
                for result in results:
                    if "result" in result and "errors" in result["result"]:
                        if "error" in result["result"]["errors"]:
                            raise WeaviateBatchError(result["result"]["errors"])

        with self.db_client.batch(
            batch_size=self.client_config.batch_size,
            timeout_retries=self.client_config.batch_retries,
            connection_error_retries=self.client_config.batch_retries,
            weaviate_error_retries=weaviate.WeaviateErrorRetryConf(
                self.client_config.batch_retries
            ),
            consistency_level=weaviate.ConsistencyLevel[self.client_config.batch_consistency],
            num_workers=self.client_config.batch_workers,
            callback=check_batch_result,
        ) as batch:
            for line in f:
                data = json.loads(line)
                # make complex to strings
                for key in self.complex_indices:
                    if key in data:
                        data[key] = json.dumps(data[key])
                for key in self.date_indices:
                    if key in data:
                        data[key] = ensure_pendulum_datetime(data[key]).isoformat()
                if self.unique_identifiers:
                    uuid = self.generate_uuid(data, self.unique_identifiers, self.class_name)
                else:
                    uuid = None

                batch.add_data_object(data, self.class_name, uuid=uuid)

    def list_unique_identifiers(self, table_schema: TTableSchema) -> Sequence[str]:
        if table_schema.get("write_disposition") == "merge":
            primary_keys = get_columns_names_with_prop(table_schema, "primary_key")
            if primary_keys:
                return primary_keys
        return get_columns_names_with_prop(table_schema, "unique")

    def generate_uuid(
        self, data: Dict[str, Any], unique_identifiers: Sequence[str], class_name: str
    ) -> str:
        data_id = "_".join([str(data[key]) for key in unique_identifiers])
        return generate_uuid5(data_id, class_name)  # type: ignore

    def state(self) -> TLoadJobState:
        return "completed"

    def exception(self) -> str:
        raise NotImplementedError()


class WeaviateClient(JobClientBase, WithStateSync):
    """Weaviate client implementation."""

    capabilities: ClassVar[DestinationCapabilitiesContext] = capabilities()
    state_properties: ClassVar[List[str]] = [
        "version",
        "engine_version",
        "pipeline_name",
        "state",
        "created_at",
        "_dlt_load_id",
    ]

    def __init__(self, schema: Schema, config: WeaviateClientConfiguration) -> None:
        super().__init__(schema, config)
        self.config: WeaviateClientConfiguration = config
        self.db_client = self.create_db_client(config)

        self._vectorizer_config = {
            "vectorizer": config.vectorizer,
            "moduleConfig": config.module_config,
        }
        self.type_mapper = WeaviateTypeMapper(self.capabilities)

    @property
    def dataset_name(self) -> str:
        return self.config.normalize_dataset_name(self.schema)

    @property
    def sentinel_class(self) -> str:
        # if no dataset name is provided we still want to create sentinel class
        return self.dataset_name or "DltSentinelClass"

    @staticmethod
    def create_db_client(config: WeaviateClientConfiguration) -> weaviate.Client:
        auth_client_secret: weaviate.AuthApiKey = (
            weaviate.AuthApiKey(api_key=config.credentials.api_key)
            if config.credentials.api_key
            else None
        )
        return weaviate.Client(
            url=config.credentials.url,
            timeout_config=(config.conn_timeout, config.read_timeout),
            startup_period=config.startup_period,
            auth_client_secret=auth_client_secret,
            additional_headers=config.credentials.additional_headers,
        )

    def make_qualified_class_name(self, table_name: str) -> str:
        """Make a full Weaviate class name from a table name by prepending
        the dataset name if it exists.
        """
        dataset_separator = self.config.dataset_separator

        return (
            f"{self.dataset_name}{dataset_separator}{table_name}"
            if self.dataset_name
            else table_name
        )

    def get_class_schema(self, table_name: str) -> Dict[str, Any]:
        """Get the Weaviate class schema for a table."""
        return cast(
            Dict[str, Any], self.db_client.schema.get(self.make_qualified_class_name(table_name))
        )

    def create_class(
        self, class_schema: Dict[str, Any], full_class_name: Optional[str] = None
    ) -> None:
        """Create a Weaviate class.

        Args:
            class_schema: The class schema to create.
            full_class_name: The full name of the class to create. If not
                provided, the class name will be prepended with the dataset name
                if it exists.
        """

        updated_schema = class_schema.copy()
        updated_schema["class"] = (
            self.make_qualified_class_name(updated_schema["class"])
            if full_class_name is None
            else full_class_name
        )

        self.db_client.schema.create_class(updated_schema)

    def create_class_property(self, class_name: str, prop_schema: Dict[str, Any]) -> None:
        """Create a Weaviate class property.

        Args:
            class_name: The name of the class to create the property on.
            prop_schema: The property schema to create.
        """
        self.db_client.schema.property.create(
            self.make_qualified_class_name(class_name), prop_schema
        )

    def delete_class(self, class_name: str) -> None:
        """Delete a Weaviate class.

        Args:
            class_name: The name of the class to delete.
        """
        self.db_client.schema.delete_class(self.make_qualified_class_name(class_name))

    def delete_all_classes(self) -> None:
        """Delete all Weaviate classes from Weaviate instance and all data
        associated with it.
        """
        self.db_client.schema.delete_all()

    def query_class(self, class_name: str, properties: List[str]) -> GetBuilder:
        """Query a Weaviate class.

        Args:
            class_name: The name of the class to query.
            properties: The properties to return.

        Returns:
            A Weaviate query builder.
        """
        return self.db_client.query.get(self.make_qualified_class_name(class_name), properties)

    def create_object(self, obj: Dict[str, Any], class_name: str) -> None:
        """Create a Weaviate object.

        Args:
            obj: The object to create.
            class_name: The name of the class to create the object on.
        """
        self.db_client.data_object.create(obj, self.make_qualified_class_name(class_name))

    def drop_storage(self) -> None:
        """Drop the dataset from Weaviate instance.

        Deletes all classes in the dataset and all data associated with them.
        Deletes the sentinel class as well.

        If dataset name was not provided, it deletes all the tables in the current schema
        """
        schema = self.db_client.schema.get()
        class_name_list = [class_["class"] for class_ in schema.get("classes", [])]

        if self.dataset_name:
            prefix = f"{self.dataset_name}{self.config.dataset_separator}"

            for class_name in class_name_list:
                if class_name.startswith(prefix):
                    self.db_client.schema.delete_class(class_name)
        else:
            # in case of no dataset prefix do our best and delete all tables in the schema
            for class_name in self.schema.tables.keys():
                if class_name in class_name_list:
                    self.db_client.schema.delete_class(class_name)

        self._delete_sentinel_class()

    @wrap_weaviate_error
    def initialize_storage(self, truncate_tables: Iterable[str] = None) -> None:
        if not self.is_storage_initialized():
            self._create_sentinel_class()
        elif truncate_tables:
            for table_name in truncate_tables:
                try:
                    class_schema = self.get_class_schema(table_name)
                except weaviate.exceptions.UnexpectedStatusCodeException as e:
                    if e.status_code == 404:
                        continue
                    raise

                self.delete_class(table_name)
                self.create_class(class_schema, full_class_name=class_schema["class"])

    @wrap_weaviate_error
    def is_storage_initialized(self) -> bool:
        try:
            self.db_client.schema.get(self.sentinel_class)
        except weaviate.exceptions.UnexpectedStatusCodeException as e:
            if e.status_code == 404:
                return False
            raise
        return True

    def _create_sentinel_class(self) -> None:
        """Create an empty class to indicate that the storage is initialized."""
        self.create_class(NON_VECTORIZED_CLASS, full_class_name=self.sentinel_class)

    def _delete_sentinel_class(self) -> None:
        """Delete the sentinel class."""
        self.db_client.schema.delete_class(self.sentinel_class)

    @wrap_weaviate_error
    def update_stored_schema(
        self, only_tables: Iterable[str] = None, expected_update: TSchemaTables = None
    ) -> Optional[TSchemaTables]:
        super().update_stored_schema(only_tables, expected_update)
        # Retrieve the schema from Weaviate
        applied_update: TSchemaTables = {}
        try:
            schema_info = self.get_stored_schema_by_hash(self.schema.stored_version_hash)
        except DestinationUndefinedEntity:
            schema_info = None
        if schema_info is None:
            logger.info(
                f"Schema with hash {self.schema.stored_version_hash} "
                "not found in the storage. upgrading"
            )
            self._execute_schema_update(only_tables)
        else:
            logger.info(
                f"Schema with hash {self.schema.stored_version_hash} "
                f"inserted at {schema_info.inserted_at} found "
                "in storage, no upgrade required"
            )

        return applied_update

    def _execute_schema_update(self, only_tables: Iterable[str]) -> None:
        for table_name in only_tables or self.schema.tables:
            exists, existing_columns = self.get_storage_table(table_name)
            # TODO: detect columns where vectorization was added or removed and modify it. currently we ignore change of hints
            new_columns = self.schema.get_new_table_columns(table_name, existing_columns)
            logger.info(f"Found {len(new_columns)} updates for {table_name} in {self.schema.name}")
            if len(new_columns) > 0:
                if exists:
                    for column in new_columns:
                        prop = self._make_property_schema(column["name"], column)
                        self.create_class_property(table_name, prop)
                else:
                    class_schema = self.make_weaviate_class_schema(table_name)
                    self.create_class(class_schema)
        self._update_schema_in_storage(self.schema)

    def get_storage_table(self, table_name: str) -> Tuple[bool, TTableSchemaColumns]:
        table_schema: TTableSchemaColumns = {}

        try:
            class_schema = self.get_class_schema(table_name)
        except weaviate.exceptions.UnexpectedStatusCodeException as e:
            if e.status_code == 404:
                return False, table_schema
            raise

        # Convert Weaviate class schema to dlt table schema
        for prop in class_schema["properties"]:
            schema_c: TColumnSchema = {
                "name": self.schema.naming.normalize_identifier(prop["name"]),
                **self._from_db_type(prop["dataType"][0], None, None),
            }
            table_schema[prop["name"]] = schema_c
        return True, table_schema

    def get_stored_state(self, pipeline_name: str) -> Optional[StateInfo]:
        """Loads compressed state from destination storage"""

        # we need to find a stored state that matches a load id that was completed
        # we retrieve the state in blocks of 10 for this
        stepsize = 10
        offset = 0
        while True:
            state_records = self.get_records(
                self.schema.state_table_name,
                # search by package load id which is guaranteed to increase over time
                sort={"path": ["_dlt_load_id"], "order": "desc"},
                where={
                    "path": ["pipeline_name"],
                    "operator": "Equal",
                    "valueString": pipeline_name,
                },
                limit=stepsize,
                offset=offset,
                properties=self.state_properties,
            )
            offset += stepsize
            if len(state_records) == 0:
                return None
            for state in state_records:
                load_id = state["_dlt_load_id"]
                load_records = self.get_records(
                    self.schema.loads_table_name,
                    where={
                        "path": ["load_id"],
                        "operator": "Equal",
                        "valueString": load_id,
                    },
                    limit=1,
                    properties=["load_id", "status"],
                )
                # if there is a load for this state which was successful, return the state
                if len(load_records):
                    state["dlt_load_id"] = state.pop("_dlt_load_id")
                    return StateInfo(**state)

    # def get_stored_states(self, state_table: str) -> List[StateInfo]:
    #     state_records = self.get_records(state_table,
    #         sort={
    #             "path": ["created_at"],
    #             "order": "desc"
    #         }, properties=self.state_properties)

    #     for state in state_records:
    #         state["dlt_load_id"] = state.pop("_dlt_load_id")
    #     return [StateInfo(**state) for state in state_records]

    def get_stored_schema(self) -> Optional[StorageSchemaInfo]:
        """Retrieves newest schema from destination storage"""
        try:
            record = self.get_records(
                self.schema.version_table_name,
                sort={"path": ["inserted_at"], "order": "desc"},
                where={
                    "path": ["schema_name"],
                    "operator": "Equal",
                    "valueString": self.schema.name,
                },
                limit=1,
            )[0]
            return StorageSchemaInfo(**record)
        except IndexError:
            return None

    def get_stored_schema_by_hash(self, schema_hash: str) -> Optional[StorageSchemaInfo]:
        try:
            record = self.get_records(
                self.schema.version_table_name,
                where={
                    "path": ["version_hash"],
                    "operator": "Equal",
                    "valueString": schema_hash,
                },
                limit=1,
            )[0]
            return StorageSchemaInfo(**record)
        except IndexError:
            return None

    @wrap_weaviate_error
    def get_records(
        self,
        table_name: str,
        where: Dict[str, Any] = None,
        sort: Dict[str, Any] = None,
        limit: int = 0,
        offset: int = 0,
        properties: List[str] = None,
    ) -> List[Dict[str, Any]]:
        # fail if schema does not exist?
        self.get_class_schema(table_name)

        # build query
        if not properties:
            properties = list(self.schema.get_table_columns(table_name).keys())
        query = self.query_class(table_name, properties)
        if where:
            query = query.with_where(where)
        if sort:
            query = query.with_sort(sort)
        if limit:
            query = query.with_limit(limit)
        if offset:
            query = query.with_offset(offset)

        response = query.do()
        full_class_name = self.make_qualified_class_name(table_name)
        records = response["data"]["Get"][full_class_name]
        return cast(List[Dict[str, Any]], records)

    def make_weaviate_class_schema(self, table_name: str) -> Dict[str, Any]:
        """Creates a Weaviate class schema from a table schema."""
        class_schema: Dict[str, Any] = {
            "class": table_name,
            "properties": self._make_properties(table_name),
        }

        # check if any column requires vectorization
        if get_columns_names_with_prop(self.schema.get_table(table_name), VECTORIZE_HINT):
            class_schema.update(self._vectorizer_config)
        else:
            class_schema.update(NON_VECTORIZED_CLASS)

        return class_schema

    def _make_properties(self, table_name: str) -> List[Dict[str, Any]]:
        """Creates a Weaviate properties schema from a table schema.

        Args:
            table: The table name for which columns should be converted to properties
        """

        return [
            self._make_property_schema(column_name, column)
            for column_name, column in self.schema.get_table_columns(table_name).items()
        ]

    def _make_property_schema(self, column_name: str, column: TColumnSchema) -> Dict[str, Any]:
        extra_kv = {}

        vectorizer_name = self._vectorizer_config["vectorizer"]
        # x-weaviate-vectorize: (bool) means that this field should be vectorized
        if not column.get(VECTORIZE_HINT, False):
            # tell weaviate explicitly to not vectorize when column has no vectorize hint
            extra_kv["moduleConfig"] = {
                vectorizer_name: {
                    "skip": True,
                }
            }

        # x-weaviate-tokenization: (str) specifies the method to use
        # for tokenization
        if TOKENIZATION_HINT in column:
            extra_kv["tokenization"] = column[TOKENIZATION_HINT]  # type: ignore

        return {
            "name": column_name,
            "dataType": [self.type_mapper.to_db_type(column)],
            **extra_kv,
        }

    def start_file_load(self, table: TTableSchema, file_path: str, load_id: str) -> LoadJob:
        return LoadWeaviateJob(
            self.schema,
            table,
            file_path,
            db_client=self.db_client,
            client_config=self.config,
            class_name=self.make_qualified_class_name(table["name"]),
        )

    def restore_file_load(self, file_path: str) -> LoadJob:
        return EmptyLoadJob.from_file_path(file_path, "completed")

    @wrap_weaviate_error
    def complete_load(self, load_id: str) -> None:
        properties = {
            "load_id": load_id,
            "schema_name": self.schema.name,
            "status": 0,
            "inserted_at": pendulum.now().isoformat(),
        }
        self.create_object(properties, self.schema.loads_table_name)

    def __enter__(self) -> "WeaviateClient":
        return self

    def __exit__(
        self,
        exc_type: Type[BaseException],
        exc_val: BaseException,
        exc_tb: TracebackType,
    ) -> None:
        pass

    def _update_schema_in_storage(self, schema: Schema) -> None:
        schema_str = json.dumps(schema.to_dict())
        properties = {
            "version_hash": schema.stored_version_hash,
            "schema_name": schema.name,
            "version": schema.version,
            "engine_version": schema.ENGINE_VERSION,
            "inserted_at": pendulum.now().isoformat(),
            "schema": schema_str,
        }
        self.create_object(properties, self.schema.version_table_name)

    def _from_db_type(
        self, wt_t: str, precision: Optional[int], scale: Optional[int]
    ) -> TColumnType:
        return self.type_mapper.from_db_type(wt_t, precision, scale)
