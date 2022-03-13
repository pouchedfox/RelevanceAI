# -*- coding: utf-8 -*-
"""Batch Insert"""
import asyncio
import json
import math
import os
import sys
import time
import traceback
import uuid

import pandas as pd

from ast import literal_eval
from datetime import datetime
from functools import partial
from pathlib import Path
from threading import Thread
from typing import Any, Callable, Dict, List, Optional, Union

from doc_utils import DocUtils

from relevanceai.analytics_funcs import track
from relevanceai.api.endpoints.client import APIClient
from relevanceai.api.batch.batch_retrieve import BatchRetrieveClient
from relevanceai.api.batch.chunk import Chunker
from relevanceai.api.batch.local_logger import PullUpdatePushLocalLogger
from relevanceai.concurrency import multiprocess, multithread
from relevanceai.errors import MissingFieldError
from relevanceai.logger import FileLogger
from relevanceai.progress_bar import progress_bar
from relevanceai.utils import Utils

BYTE_TO_MB = 1024 * 1024
LIST_SIZE_MULTIPLIER = 3

SUCCESS_CODES = [200]
RETRY_CODES = [400, 404]
HALF_CHUNK_CODES = [413, 524]


class BatchInsertClient(Utils, BatchRetrieveClient, APIClient, Chunker):
    def _insert_documents(
        self,
        dataset_id: str,
        documents: list,
        bulk_fn: Callable = None,
        max_workers: int = 8,
        retry_chunk_mult: float = 0.5,
        show_progress_bar: bool = False,
        chunksize: int = 0,
        use_json_encoder: bool = True,
        verbose: bool = True,
        create_id: bool = False,
        *args,
        **kwargs,
    ):

        """
        Insert a list of documents with multi-threading automatically enabled.

        - When inserting the document you can optionally specify your own id for a document by using the field name "_id", if not specified a random id is assigned.
        - When inserting or specifying vectors in a document use the suffix (ends with) "_vector_" for the field name. e.g. "product_description_vector_".
        - When inserting or specifying chunks in a document the suffix (ends with) "_chunk_" for the field name. e.g. "products_chunk_".
        - When inserting or specifying chunk vectors in a document's chunks use the suffix (ends with) "_chunkvector_" for the field name. e.g. "products_chunk_.product_description_chunkvector_".

        Documentation can be found here: https://ingest-api-dev-aueast.relevance.ai/latest/documentation#operation/InsertEncode

        Parameters
        ----------
        dataset_id : string
            Unique name of dataset
        documents : list
            A list of documents. Document is a JSON-like data that we store our metadata and vectors with. For specifying id of the document use the field '_id', for specifying vector field use the suffix of '_vector_'
        bulk_fn : callable
            Function to apply to documents before uploading
        max_workers : int
            Number of workers active for multi-threading
        retry_chunk_mult: int
            Multiplier to apply to chunksize if upload fails
        chunksize : int
            Number of documents to upload per worker. If None, it will default to the size specified in config.upload.target_chunk_mb
        use_json_encoder : bool
            Whether to automatically convert documents to json encodable format

        Example
        --------

        >>> from relevanceai import Client
        >>> client = Client()
        >>> df = client.Dataset("sample_dataset_id")
        >>> documents = [{"_id": "10", "value": 5}, {"_id": "332", "value": 10}]
        >>> df.insert_documents(documents)

        """

        self.logger.info(f"You are currently inserting into {dataset_id}")

        self.logger.info(
            f"You can track your stats and progress via our dashboard at https://cloud.relevance.ai/collections/dashboard/stats/?collection={dataset_id}"
        )
        # Check if the collection exists
        self.datasets.create(dataset_id)

        if use_json_encoder:
            documents = self.json_encoder(documents)

        # TODO: rename this function to convert_id_to_string
        self._convert_id_to_string(documents, create_id=create_id)

        def bulk_insert_func(documents):
            return self.datasets.bulk_insert(
                dataset_id,
                documents,
                return_documents=True,
                *args,
                **kwargs,
            )

        if verbose:
            print(
                f"while inserting, you can visit your dashboard at https://cloud.relevance.ai/dataset/{dataset_id}/dashboard/monitor/"
            )

        return self._write_documents(
            bulk_insert_func,
            documents,
            bulk_fn,
            max_workers,
            retry_chunk_mult,
            show_progress_bar=show_progress_bar,
            chunksize=chunksize,
        )

    def _insert_csv(
        self,
        dataset_id: str,
        filepath_or_buffer,
        chunksize: int = 10000,
        max_workers: int = 8,
        retry_chunk_mult: float = 0.5,
        show_progress_bar: bool = False,
        index_col: int = None,
        csv_args: Optional[dict] = None,
        col_for_id: str = None,
        auto_generate_id: bool = True,
    ):

        """
        Insert data from csv file

        Parameters
        ----------
        dataset_id : string
            Unique name of dataset
        filepath_or_buffer :
            Any valid string path is acceptable. The string could be a URL. Valid URL schemes include http, ftp, s3, gs, and file.
        chunksize : int
            Number of lines to read from csv per iteration
        max_workers : int
            Number of workers active for multi-threading
        retry_chunk_mult: int
            Multiplier to apply to chunksize if upload fails
        csv_args : dict
            Optional arguments to use when reading in csv. For more info, see https://pandas.pydata.org/documents/reference/api/pandas.read_csv.html
        index_col : None
            Optional argument to specify if there is an index column to be skipped (e.g. index_col = 0)
        col_for_id : str
            Optional argument to use when a specific field is supposed to be used as the unique identifier ('_id')
        auto_generate_id: bool = True
            Automatically generateds UUID if auto_generate_id is True and if the '_id' field does not exist

        Example
        ---------
        >>> from relevanceai import Client
        >>> client = Client()
        >>> df = client.Dataset("sample_dataset_id")
        >>> csv_filename = "temp.csv"
        >>> df.insert_csv(csv_filename)

        """
        csv_args = {} if csv_args is None else csv_args

        csv_args.pop("index_col", None)
        csv_args.pop("chunksize", None)
        df = pd.read_csv(
            filepath_or_buffer, index_col=index_col, chunksize=chunksize, **csv_args
        )

        # Initialise output
        inserted = 0
        failed_documents = []
        failed_documents_detailed = []

        # Chunk inserts
        for chunk in df:
            response = self._insert_csv_chunk(
                chunk=chunk,
                dataset_id=dataset_id,
                max_workers=max_workers,
                retry_chunk_mult=retry_chunk_mult,
                show_progress_bar=show_progress_bar,
                col_for_id=col_for_id,
                auto_generate_id=auto_generate_id,
            )
            inserted += response["inserted"]
            failed_documents += response["failed_documents"]
            failed_documents_detailed += response["failed_documents_detailed"]

        return {
            "inserted": inserted,
            "failed_documents": failed_documents,
            "failed_documents_detailed": failed_documents_detailed,
        }

    def _insert_csv_chunk(
        self,
        chunk,
        dataset_id,
        max_workers,
        retry_chunk_mult,
        show_progress_bar,
        col_for_id,
        auto_generate_id,
    ):
        # generate '_id' if possible
        # col_for_id
        if "_id" not in chunk.columns and col_for_id:
            if col_for_id in chunk:
                chunk.insert(0, "_id", chunk[col_for_id], False)
            else:
                self.logger.warning(
                    f"The specified column {col_for_id} does not exist in the CSV file"
                )
        # auto_generate_id
        if "_id" not in chunk.columns and auto_generate_id:
            index = chunk.index
            uuids = [uuid.uuid4() for _ in range(len(index))]
            chunk.insert(0, "_id", uuids, False)
            self.logger.warning(
                "We will be auto-generating IDs since no id field is detected"
            )

        # Check for _id
        if "_id" not in chunk.columns:
            raise MissingFieldError("Need _id as a column")

        # add fix for when lists are read in as strings
        EXCEPTION_COLUMNS = ("_vector_", "_chunk_")
        vector_columns = [i for i in chunk.columns if i.endswith(EXCEPTION_COLUMNS)]
        for i in vector_columns:
            chunk[i] = chunk[i].apply(literal_eval)

        chunk_json = chunk.to_dict(orient="records")

        print(
            f"while inserting, you can visit your dashboard at https://cloud.relevance.ai/dataset/{dataset_id}/dashboard/monitor/"
        )
        response = self._insert_documents(
            dataset_id=dataset_id,
            documents=chunk_json,
            max_workers=max_workers,
            retry_chunk_mult=retry_chunk_mult,
            show_progress_bar=show_progress_bar,
            verbose=False,
        )
        return response

    def _update_documents(
        self,
        dataset_id: str,
        documents: list,
        bulk_fn: Callable = None,
        max_workers: int = 8,
        retry_chunk_mult: float = 0.5,
        chunksize: int = 0,
        show_progress_bar=False,
        use_json_encoder: bool = True,
        create_id: bool = False,
        *args,
        **kwargs,
    ):
        """
        Update a list of documents with multi-threading automatically enabled.
        Edits documents by providing a key value pair of fields you are adding or changing, make sure to include the "_id" in the documents.

        Example
        ----------

        >>> from relevanceai import Client
        >>> url = "https://api-aueast.relevance.ai/v1/"
        >>> collection = ""
        >>> project = ""
        >>> api_key = ""
        >>> client = Client(project=project, api_key=api_key, firebase_uid=firebase_uid)
        >>> documents = client.datasets.documents.get_where(collection, select_fields=['title'])
        >>> while len(documents['documents']) > 0:
        >>>     documents['documents'] = model.encode_documents_in_bulk(['product_name'], documents['documents'])
        >>>     client.update_documents(collection, documents['documents'])
        >>>     documents = client.datasets.documents.get_where(collection, select_fields=['product_name'], cursor=documents['cursor'])

        Parameters
        ----------
        dataset_id : string
            Unique name of dataset
        documents : list
            A list of documents. Document is a JSON-like data that we store our metadata and vectors with. For specifying id of the document use the field '_id', for specifying vector field use the suffix of '_vector_'
        bulk_fn : callable
            Function to apply to documents before uploading
        max_workers : int
            Number of workers active for multi-threading
        retry_chunk_mult: int
            Multiplier to apply to chunksize if upload fails
        chunksize : int
            Number of documents to upload per worker. If None, it will default to the size specified in config.upload.target_chunk_mb
        use_json_encoder : bool
            Whether to automatically convert documents to json encodable format
        """

        self.logger.info(f"You are currently updating {dataset_id}")

        self.logger.info(
            f"You can track your stats and progress via our dashboard at https://cloud.relevance.ai/collections/dashboard/stats/?collection={dataset_id}"
        )

        # Turn _id into string
        self._convert_id_to_string(documents, create_id=create_id)

        if use_json_encoder:
            documents = self.json_encoder(documents)

        def bulk_update_func(documents):
            return self.datasets.documents.bulk_update(
                dataset_id,
                documents,
                return_documents=True,
                *args,
                **kwargs,
            )

        return self._write_documents(
            bulk_update_func,
            documents,
            bulk_fn,
            max_workers,
            retry_chunk_mult,
            show_progress_bar=show_progress_bar,
            chunksize=chunksize,
        )

    update_documents = _update_documents

    def pull_update_push(
        self,
        dataset_id: str,
        update_function,
        updated_dataset_id: str = None,
        log_file: str = None,
        updated_documents_file: str = None,
        updating_args: Optional[dict] = None,
        retrieve_chunk_size: int = 100,
        max_workers: int = 8,
        filters: Optional[list] = None,
        select_fields: Optional[list] = None,
        show_progress_bar: bool = True,
        use_json_encoder: bool = True,
    ):
        """
        Loops through every document in your collection and applies a function (that is specified by you) to the documents.
        These documents are then uploaded into either an updated collection, or back into the original collection.

        Parameters
        ----------
        dataset_id: string
            The dataset_id of the collection where your original documents are

        update_function: function
            A function created by you that converts documents in your original collection into the updated documents. The function must contain a field which takes in a list of documents from the original collection. The output of the function must be a list of updated documents.

        updated_dataset_id: string
            The dataset_id of the collection where your updated documents are uploaded into. If 'None', then your original collection will be updated.

        log_file: str
            The log file to direct any information or issues that may crop up.
            If no log file is specified, one will automatically be created.

        updated_documents_file: str
            A file to keep track of documents that have already been update.
            If a file is not specified, one will automatically be created.

        updating_args: dict
            Additional arguments to your update_function, if they exist. They must be in the format of {'Argument': Value}

        retrieve_chunk_size: int
            The number of documents that are received from the original collection with each loop iteration.

        max_workers: int
            The number of processors you want to parallelize with

        filters: list
            A list of filters to apply on the retrieval query

        select_fields: list
            A list of fields to query over

        use_json_encoder : bool
            Whether to automatically convert documents to json encodable format
        """
        updating_args = {} if updating_args is None else updating_args
        filters = [] if filters is None else filters
        select_fields = [] if select_fields is None else select_fields

        if not callable(update_function):
            raise TypeError(
                "Your update function needs to be a function! Please read the documentation if it is not."
            )

        # Check if a logging_collection has been supplied
        if log_file is None:
            log_file = (
                dataset_id
                + "_"
                + str(datetime.now().strftime("%d-%m-%Y-%H-%M-%S"))
                + "_pull_update_push"
                + ".log"
            )
            self.logger.info(f"Created {log_file}")

        if updated_documents_file is None:
            updated_documents_file = "_".join(
                [
                    dataset_id,
                    str(datetime.now().strftime("%d-%m-%Y-%H-%M-%S")),
                    "pull_update_push-updated_documents.temp",
                ]
            )
            self.logger.info(f"Created {updated_documents_file}")

        with FileLogger(fn=log_file, verbose=True):
            # Instantiate the logger to document the successful IDs
            PULL_UPDATE_PUSH_LOGGER = PullUpdatePushLocalLogger(updated_documents_file)

            # Track failed documents
            failed_documents: List[Dict] = []
            failed_documents_detailed: List[Dict] = []

            # Get document lengths to calculate iterations
            original_length = self.get_number_of_documents(dataset_id, filters)

            # get the remaining number in case things break
            remaining_length = (
                original_length - PULL_UPDATE_PUSH_LOGGER.count_ids_in_fn()
            )

            # iterations_required = math.ceil(remaining_length / retrieve_chunk_size)
            iterations_required = math.ceil(remaining_length / retrieve_chunk_size)

            # Get incomplete documents from raw collection
            retrieve_filters = filters + [
                # {
                #     "field": "ids",
                #     "filter_type": "ids",
                #     "condition": "!=",
                #     "condition_value": completed_documents_list,
                # }
            ]
            for _ in progress_bar(
                range(iterations_required), show_progress_bar=show_progress_bar
            ):

                orig_json = self.datasets.documents.get_where(
                    dataset_id,
                    filters=retrieve_filters,
                    page_size=retrieve_chunk_size,
                    select_fields=select_fields,
                )

                documents = orig_json["documents"]

                try:
                    updated_data = update_function(documents)
                except Exception as e:
                    self.logger.error("Your updating function does not work: " + str(e))
                    traceback.print_exc()
                    return

                updated_documents = [i["_id"] for i in documents]

                # Upload documents
                if updated_dataset_id is None:
                    insert_json = self._update_documents(
                        dataset_id=dataset_id,
                        documents=updated_data,
                        max_workers=max_workers,
                        show_progress_bar=False,
                        use_json_encoder=use_json_encoder,
                    )
                else:
                    insert_json = self._insert_documents(
                        dataset_id=updated_dataset_id,
                        documents=updated_data,
                        max_workers=max_workers,
                        show_progress_bar=False,
                        use_json_encoder=use_json_encoder,
                    )

                chunk_failed = insert_json["failed_documents"]
                chunk_documents_detailed = insert_json["failed_documents_detailed"]
                failed_documents.extend(chunk_failed)
                failed_documents_detailed.extend(chunk_documents_detailed)
                success_documents = list(set(updated_documents) - set(failed_documents))
                PULL_UPDATE_PUSH_LOGGER.log_ids(success_documents)
                self.logger.success(
                    f"Chunk of {retrieve_chunk_size} original documents updated and uploaded with {len(chunk_failed)} failed documents!"
                )

            if failed_documents:
                # This will be picked up by FileLogger
                print("The following documents failed to be updated/inserted:")
                for failed_document in failed_documents:
                    print(f"  * {failed_document}")

        self.logger.info(f"Deleting {updated_documents_file}")
        os.remove(updated_documents_file)

        self.logger.success(f"Pull, Update, Push is complete!")

        return {
            "failed_documents": failed_documents,
            "failed_documents_detailed": failed_documents_detailed,
        }

    def pull_update_push_async(
        self,
        dataset_id: str,
        update_function,
        updating_args: Optional[dict] = None,
        updated_dataset_id: Optional[str] = None,
        log_file: str = None,
        retrieve_chunk_size: int = 100,
        filters: Optional[list] = None,
        select_fields: Optional[list] = None,
        use_json_encoder: bool = True,
        include_vector: bool = True,
        show_progress_bar: bool = True,
        insert: bool = False,
    ):
        """
        Loops through every document in your collection and applies a function (that is specified by you) to the documents.
        These documents are then uploaded into either an updated collection, or back into the original collection.

        Parameters
        ----------
        dataset_id: str
            The dataset_id of the collection where your original documents are

        update_function
            A function created by you that converts documents in your original collection into the updated documents. The function must contain a field which takes in a list of documents from the original collection. The output of the function must be a list of updated documents.

        updating_args: dict
            Additional arguments to your update_function, if they exist. They must be in the format of {'Argument': Value}

        updated_dataset_id: str
            The dataset_id of the collection where your updated documents are uploaded into. If 'None', then your original collection will be updated.

        log_file: str
            The log file to direct any information or issues that may crop up.
            If no log file is specified, one will automatically be created.

        retrieve_chunk_size: int
            The number of documents that are received from the original collection with each loop iteration.

        filters: list
            A list of filters to apply on the retrieval query

        select_fields: list
            A list of fields to query over

        use_json_encoder : bool
            Whether to automatically convert documents to json encodable format

        include_vector: bool
            If True, includes vectors in the updating query

        show_progress_bar: bool
            If True, shows a progress bar

        insert: bool
            If True, inserts rather than updates an already-existing dataset
        """
        updating_args = {} if updating_args is None else updating_args
        filters = [] if filters is None else filters
        select_fields = [] if select_fields is None else select_fields

        if not callable(update_function):
            raise TypeError(
                "Your update function needs to be a function! Please read the documentation if it is not."
            )

        if log_file is None:
            log_file = (
                dataset_id
                + "_"
                + str(datetime.now().strftime("%d-%m-%Y-%H-%M-%S"))
                + "_pull_update_push"
                + ".log"
            )
            self.logger.info(f"Created {log_file}")

        with FileLogger(fn=log_file, verbose=True):
            num_documents = self.get_number_of_documents(dataset_id, filters)

            # This number will determine how many requests are sent.
            # Example: Suppose the number of documents is 1254 and
            # retrieve_chunk_size is 100. Then the number of requests would
            # be 1254 // 100, which would be 12, and one, which amounts to
            # 13 requests. This must be defined ahead of time because the
            # cursor predetermines the number of documents to retrieve on
            # the first call. So, The first 12 calls would each get 100
            # documents and call 13 would retrieve the remaining 54.
            num_requests = (num_documents // retrieve_chunk_size) + 1

            async def pull_update_push_subset(page_size: int, cursor: str = None):
                response = await self.datasets.documents.get_where_async(
                    dataset_id,
                    filters=filters,
                    cursor=cursor,
                    page_size=page_size,
                    select_fields=select_fields,
                    include_vector=include_vector,
                )

                documents = response["documents"]

                try:
                    updated_documents = update_function(documents, **updating_args)
                except Exception as e:
                    self.logger.error("Your updating function does not work: " + str(e))
                    traceback.print_exc()
                    return

                updated_ids = [document["_id"] for document in documents]

                inserted = await self._process_documents(
                    dataset_id=updated_dataset_id
                    if updated_dataset_id is not None
                    else dataset_id,
                    documents=updated_documents,
                    insert=insert,
                    use_json_encoder=use_json_encoder,
                )

                return inserted, updated_ids

            tasks = []
            if retrieve_chunk_size >= num_documents:
                tasks.append(pull_update_push_subset(num_documents, None))
            else:
                # Retrieve the cursor. While executing a get_where call just
                # to retrieve the cursor is somewhat inefficient, this is
                # the cleanest solution at the moment.
                cursor = self.datasets.documents.get_where(
                    dataset_id,
                    filters=filters,
                    page_size=retrieve_chunk_size,
                    select_fields=select_fields,
                    include_vector=False,  # Set to false to for speed
                )["cursor"]

                # The cursor is constrained by its first query. Specifically,
                # if the query from which we get the cursor has a certain page
                # size, further accesses through the cursor will be the same
                # page size. Therefore, in the tasks below, the first task
                # does a proper query of the first {retrieve_chunk_size}
                # documents. Subsequent calls use the cursor to access the
                # next {retrieve_chunk_size} documents {num_requests-1} times.
                # The 0 is there to make the aforementioned statement
                # explicit.
                tasks.extend(
                    [
                        pull_update_push_subset(retrieve_chunk_size, None)
                        if not task_num
                        else pull_update_push_subset(0, cursor)
                        for task_num in range(num_requests)
                    ]
                )

            # The event loop is required to call asynchronous functions from
            # a regular (synchronous) function. This class spins off a
            # background thread that runs an event loop. All asynchronous
            # tasks are run there.
            class EventLoop(Thread):
                def __init__(self, tasks, num_requests, show_progress_bar):
                    super().__init__()
                    self._loop = asyncio.new_event_loop()
                    self.daemon = True

                    self.futures = []
                    self.tasks = tasks

                    from relevanceai.progress_bar import progress_bar

                    self.show_progress_bar = show_progress_bar
                    self.progress_tracker = progress_bar(
                        range(num_requests), show_progress_bar=show_progress_bar
                    )
                    self.progress_iterator = iter(self.progress_tracker)

                def run(self):
                    self._loop.run_forever()

                async def _create_future(self, task):
                    return await asyncio.create_task(task)

                def execute_tasks(self):
                    self.futures.extend(
                        [
                            asyncio.run_coroutine_threadsafe(
                                self._create_future(task), self._loop
                            )
                            for task in self.tasks
                        ]
                    )

                    from concurrent.futures import as_completed

                    for _ in as_completed(self.futures):
                        if self.progress_tracker is not None:
                            next(self.progress_iterator)
                        if self.show_progress_bar:
                            self.progress_tracker.update(1)

                def terminate(self):
                    self._loop.call_soon_threadsafe(self._loop.stop)
                    self.join()

            threaded_loop = EventLoop(tasks, num_requests, show_progress_bar)
            threaded_loop.start()
            threaded_loop.execute_tasks()
            threaded_loop.terminate()

            failed_documents = []
            for future in threaded_loop.futures:
                inserted, updated_ids = future.result()
                failed_documents.extend(inserted["failed_documents"])

            if failed_documents:
                # This will be picked up by FileLogger
                print("The following documents failed to be updated/inserted:")
                for failed_document in failed_documents:
                    print(f"  * {failed_document}")

            self.logger.success("Pull, update, and push is complete!")

            return {"failed_documents": failed_documents}

    def pull_update_push_to_cloud(
        self,
        dataset_id: str,
        update_function,
        updated_dataset_id: str = None,
        logging_dataset_id: str = None,
        updating_args: Optional[dict] = None,
        retrieve_chunk_size: int = 100,
        retrieve_chunk_size_failure_retry_multiplier: float = 0.5,
        number_of_retrieve_retries: int = 3,
        max_workers: int = 8,
        max_error: int = 1000,
        filters: Optional[list] = None,
        select_fields: Optional[list] = None,
        show_progress_bar: bool = True,
        use_json_encoder: bool = True,
    ):
        """
        Loops through every document in your collection and applies a function (that is specified by you) to the documents.
        These documents are then uploaded into either an updated collection, or back into the original collection.

        Parameters
        ----------
        original_dataset_id: string
            The dataset_id of the collection where your original documents are
        logging_dataset_id: string
            The dataset_id of the collection which logs which documents have been updated. If 'None', then one will be created for you.
        updated_dataset_id: string
            The dataset_id of the collection where your updated documents are uploaded into. If 'None', then your original collection will be updated.
        update_function: function
            A function created by you that converts documents in your original collection into the updated documents. The function must contain a field which takes in a list of documents from the original collection. The output of the function must be a list of updated documents.
        updating_args: dict
            Additional arguments to your update_function, if they exist. They must be in the format of {'Argument': Value}
        retrieve_chunk_size: int
            The number of documents that are received from the original collection with each loop iteration.
        retrieve_chunk_size_failure_retry_multiplier: int
            If fails, retry on each chunk
        max_workers: int
            The number of processors you want to parallelize with
        max_error: int
            How many failed uploads before the function breaks
        json_encoder : bool
            Whether to automatically convert documents to json encodable format
        """
        updating_args = {} if updating_args is None else updating_args
        filters = [] if filters is None else filters
        select_fields = [] if select_fields is None else select_fields

        # Check if a logging_collection has been supplied
        if logging_dataset_id is None:
            logging_dataset_id = (
                dataset_id
                + "_"
                + str(datetime.now().strftime("%d-%m-%Y-%H-%M-%S"))
                + "_pull_update_push"
            )

        with FileLogger(fn=f"{logging_dataset_id}.log", verbose=True):
            # Check collections and create completed list if needed
            collection_list = self.datasets.list()
            if logging_dataset_id not in collection_list["datasets"]:
                self.logger.info("Creating a logging collection for you.")
                self.logger.info(self.datasets.create(logging_dataset_id))

            # Track failed documents
            failed_documents: List[Dict] = []

            # Trust the process
            for _ in range(number_of_retrieve_retries):

                # Get document lengths to calculate iterations
                original_length = self.get_number_of_documents(dataset_id, filters)
                completed_length = self.get_number_of_documents(logging_dataset_id)
                remaining_length = original_length - completed_length
                iterations_required = math.ceil(remaining_length / retrieve_chunk_size)

                self.logger.debug(f"{original_length}")
                self.logger.debug(f"{completed_length}")
                self.logger.debug(f"{iterations_required}")

                # Return if no documents to update
                if remaining_length == 0:
                    self.logger.success(f"Pull, Update, Push is complete!")
                    return {
                        "failed_documents": failed_documents,
                        "logging_collection": logging_dataset_id,
                    }

                for _ in progress_bar(
                    range(iterations_required), show_progress_bar=show_progress_bar
                ):

                    # Get completed documents
                    log_json = self._get_all_documents(
                        logging_dataset_id, show_progress_bar=False
                    )
                    completed_documents_list = [i["_id"] for i in log_json]

                    # Get incomplete documents from raw collection
                    retrieve_filters = filters + [
                        {
                            "field": "ids",
                            "filter_type": "ids",
                            "condition": "!=",
                            "condition_value": completed_documents_list,
                        }
                    ]

                    orig_json = self.datasets.documents.get_where(
                        dataset_id,
                        filters=retrieve_filters,
                        page_size=retrieve_chunk_size,
                        select_fields=select_fields,
                    )

                    documents = orig_json["documents"]
                    self.logger.debug(f"{len(documents)}")

                    # Update documents
                    try:
                        updated_data = update_function(documents, **updating_args)
                    except Exception as e:
                        self.logger.error(
                            "Your updating function does not work: " + str(e)
                        )
                        traceback.print_exc()
                        return
                    updated_documents = [i["_id"] for i in documents]
                    self.logger.debug(f"{len(updated_data)}")

                    # Upload documents
                    if updated_dataset_id is None:
                        insert_json = self._update_documents(
                            dataset_id=dataset_id,
                            documents=updated_data,
                            max_workers=max_workers,
                            show_progress_bar=False,
                            use_json_encoder=use_json_encoder,
                        )
                    else:
                        insert_json = self._insert_documents(
                            dataset_id=updated_dataset_id,
                            documents=updated_data,
                            max_workers=max_workers,
                            show_progress_bar=False,
                            use_json_encoder=use_json_encoder,
                        )

                    # Check success
                    chunk_failed = insert_json["failed_documents"]
                    self.logger.success(
                        f"Chunk of {retrieve_chunk_size} original documents updated and uploaded with {len(chunk_failed)} failed documents!"
                    )
                    failed_documents.extend(chunk_failed)
                    success_documents = list(
                        set(updated_documents) - set(failed_documents)
                    )
                    upload_documents = [{"_id": i} for i in success_documents]

                    self._insert_documents(
                        logging_dataset_id,
                        upload_documents,
                        max_workers=max_workers,
                        show_progress_bar=False,
                    )

                    # If fail, try to reduce retrieve chunk
                    if len(chunk_failed) > 0:
                        self.logger.warning(
                            "Failed to upload. Retrieving half of previous number."
                        )
                        retrieve_chunk_size = int(
                            retrieve_chunk_size
                            * retrieve_chunk_size_failure_retry_multiplier
                        )
                        time.sleep(self.config.seconds_between_retries)
                        break

                    if len(failed_documents) > max_error:
                        self.logger.error(
                            f"You have over {max_error} failed documents which failed to upload!"
                        )
                        return {
                            "failed_documents": failed_documents,
                            "logging_collection": logging_dataset_id,
                        }

                self.logger.success(f"Pull, Update, Push is complete!")

            return {
                "failed_documents": failed_documents,
                "logging_collection": logging_dataset_id,
            }

    @track
    def insert_df(self, dataset_id, dataframe, *args, **kwargs):
        """Insert a dataframe for eachd doc"""

        def _is_valid(v):
            try:
                if pd.isna(v):
                    return False
                else:
                    return True
            except:
                return True

        documents = [
            {k: v for k, v in doc.items() if _is_valid(v)}
            for doc in dataframe.to_dict(orient="records")
        ]
        results = self._insert_documents(dataset_id, documents, *args, **kwargs)
        self.print_search_dashboard_url(dataset_id)
        return results

    def print_search_dashboard_url(self, dataset_id):
        search_url = (
            f"https://cloud.relevance.ai/dataset/{dataset_id}/deploy/recent/search"
        )
        self._dataset_id = dataset_id
        print(f"🍡 You can now explore your search app at {search_url}")

    def delete_pull_update_push_logs(self, dataset_id=False):

        collection_list = self.datasets.list()["datasets"]

        if dataset_id:
            log_collections = [
                i
                for i in collection_list
                if ("pull_update_push" in i) and (dataset_id in i)
            ]

        else:
            log_collections = [i for i in collection_list if ("pull_update_push" in i)]

        [self.datasets.delete(i, confirm=False) for i in log_collections]
        return

    def _write_documents(
        self,
        insert_function,
        documents: list,
        bulk_fn: Callable = None,
        max_workers: int = 8,
        retry_chunk_mult: float = 0.5,
        show_progress_bar: bool = False,
        chunksize: int = 0,
    ):

        # Get one document to test the size
        if len(documents) == 0:
            self.logger.warning("No document is detected")
            return {
                "inserted": 0,
                "failed_documents": [],
                "failed_documents_detailed": [],
            }

        # Insert documents
        test_doc = json.dumps(documents[0], indent=4)
        doc_mb = sys.getsizeof(test_doc) * LIST_SIZE_MULTIPLIER / BYTE_TO_MB
        if chunksize == 0:
            target_chunk_mb = int(self.config.get_option("upload.target_chunk_mb"))
            max_chunk_size = int(self.config.get_option("upload.max_chunk_size"))
            chunksize = (
                int(target_chunk_mb / doc_mb) + 1
                if int(target_chunk_mb / doc_mb) + 1 < len(documents)
                else len(documents)
            )
            chunksize = max(chunksize, max_chunk_size)

        # Initialise number of inserted documents
        inserted: List[str] = []

        # Initialise failed documents
        failed_ids = [i["_id"] for i in documents]

        # Initialise failed documents detailed
        failed_ids_detailed: List[str] = []

        # Initialise cancelled documents
        cancelled_ids = []

        for i in range(int(self.config.get_option("retries.number_of_retries"))):
            if len(failed_ids) > 0:
                self.logger.info(f"Inserting with chunksize {chunksize}")
                if bulk_fn is not None:
                    insert_json = multiprocess(
                        func=bulk_fn,
                        iterables=documents,
                        post_func_hook=insert_function,
                        max_workers=max_workers,
                        chunksize=chunksize,
                        show_progress_bar=show_progress_bar,
                    )
                else:
                    insert_json = multithread(
                        insert_function,
                        documents,
                        max_workers=max_workers,
                        chunksize=chunksize,
                        show_progress_bar=show_progress_bar,
                    )

                failed_ids = []
                failed_ids_detailed = []

                # Update inserted amount
                def is_successfully_inserted(chunk: Union[Dict, Any]) -> bool:
                    return chunk["status_code"] == 200

                inserted += list(
                    map(
                        lambda x: x["response_json"]["inserted"],
                        filter(is_successfully_inserted, insert_json),
                    )
                )

                for chunk in insert_json:

                    # Track failed in 200
                    if chunk["status_code"] in SUCCESS_CODES:
                        failed_ids += [
                            i["_id"] for i in chunk["response_json"]["failed_documents"]
                        ]

                        failed_ids_detailed += [
                            i for i in chunk["response_json"]["failed_documents"]
                        ]

                    # Cancel documents with 400 or 404
                    elif chunk["status_code"] in RETRY_CODES:
                        cancelled_ids += [i["_id"] for i in chunk["documents"]]

                    # Half chunksize with 413 or 524
                    elif chunk["status_code"] in HALF_CHUNK_CODES:
                        failed_ids += [i["_id"] for i in chunk["documents"]]
                        chunksize = int(chunksize * retry_chunk_mult)

                    # Retry all other errors
                    else:
                        failed_ids += [i["_id"] for i in chunk["documents"]]

                # Update documents to retry which have failed
                self.logger.warning(
                    f"Failed to upload {failed_ids}. Automatically retrying for you with chunksize {chunksize}"
                )
                documents = [i for i in documents if i["_id"] in failed_ids]

            else:
                break
            time.sleep(int(self.config["retries.seconds_between_retries"]))

        # When returning, add in the cancelled id
        failed_ids.extend(cancelled_ids)

        output = {
            "inserted": sum(inserted),
            "failed_documents": failed_ids,
            "failed_documents_detailed": failed_ids_detailed,
        }
        return output

    def rename_fields(
        self,
        dataset_id: str,
        field_mappings: dict,
        retrieve_chunk_size: int = 100,
        max_workers: int = 8,
        show_progress_bar: bool = True,
    ):
        """
        Loops through every document in your collection and renames specified fields by deleting the old one and
        creating a new field using the provided mapping
        These documents are then uploaded into either an updated collection, or back into the original collection.

        Example:
        rename_fields(dataset_id,field_mappings = {'a.b.d':'a.b.c'})  => doc['a']['b']['d'] => doc['a']['b']['c']
        rename_fields(dataset_id,field_mappings = {'a.b':'a.c'})  => doc['a']['b'] => doc['a']['c']

        Parameters
        ----------
        dataset_id : string
            The dataset_id of the collection where your original documents are
        field_mappings : dict
            A dictionary in the form f {old_field_name1 : new_field_name1, ...}
        retrieve_chunk_size: int
            The number of documents that are received from the original collection with each loop iteration.
        retrieve_chunk_size_failure_retry_multiplier: int
            If fails, retry on each chunk
        max_workers: int
            The number of processors you want to parallelize with
        show_progress_bar: bool
            Shows a progress bar if True
        """
        self.logger.warning(
            "Currently this function is in beta and may change in the future."
        )

        skip = set()
        for old_f, new_f in field_mappings.items():
            if len(old_f.split(".")) != len(new_f.split(".")):
                skip.add(old_f)
                self.logger.warning(f"{old_f} does not match {new_f}.")

        for k in skip:
            del field_mappings[k]

        def rename_dict_fields(d, field_mappings={}, track=""):
            modified_dict = {}
            for k, v in sorted(d.items(), key=lambda x: x[0]):
                if track != "" and track[-1] != ".":
                    track += "."
                if track + k not in field_mappings.keys():
                    kk = k
                else:
                    kk = field_mappings[track + k].split(".")[-1]

                if isinstance(v, dict):
                    if k not in modified_dict:
                        modified_dict[kk] = {}
                    modified_dict[kk] = rename_dict_fields(
                        v, field_mappings, track + kk
                    )
                else:
                    modified_dict[kk] = v
            return modified_dict

        sample_documents = self.datasets.documents.list(dataset_id)

        def update_function(sample_documents):
            for i, d in enumerate(sample_documents):
                sample_documents[i] = rename_dict_fields(
                    d, field_mappings=field_mappings, track=""
                )
            return sample_documents

        self.pull_update_push(dataset_id, update_function, retrieve_chunk_size=200)

    def _process_insert_results(self, results: dict, return_json: bool = False):
        # in case API is backwards incompatible

        if "failed_documents" in results:
            if len(results["failed_documents"]) == 0:
                print("✅ All documents inserted/edited successfully.")
            else:
                print(
                    "❗Few errors with inserting/editing documents. Please check logs."
                )

        elif "failed_document_ids" in results:
            if len(results["failed_document_ids"]) == 0:
                print("✅ All documents inserted/edited successfully.")
            else:
                print(
                    "❗Few errors with inserting/editing documents. Please check logs."
                )

        # Make backwards compatible on errors
        if (
            len(results.get("failed_documents", []))
            + len(results.get("failed_document_ids", []))
            > 0
        ) or return_json:
            return results

    async def _apply_bulk_fn(self, bulk_fn, documents: list):
        """
        Called from _process_documents. Calls bulk_fn on documents.

        Parameters
        ----------
        bulk_fn
            An asynchronous function that takes in the documents

        documents: list
            A list of documents
        """
        if len(documents) == 0:
            self.logger.warning("No document is detected")
            return {
                "inserted": 0,
                "failed_documents": [],
                "failed_documents_detailed": [],
            }

        num_documents_inserted: int = 0
        # Maintain a reference to documents to keep track during looping
        documents_remaining = documents.copy()
        for _ in range(int(self.config.get_option("retries.number_of_retries"))):
            if len(documents_remaining) > 0:
                # bulk_update_async
                response = await bulk_fn(documents=documents)
                num_documents_inserted += response["inserted"]
                documents_remaining = [
                    document
                    for document in documents_remaining
                    if document["_id"] in response["failed_documents"]
                ]
            else:
                # Once documents_remaining is empty, leave the for-loop...
                break
            # ...else, wait some amount of time before retrying
            time.sleep(int(self.config["retries.seconds_between_retries"]))

        return {
            "inserted": num_documents_inserted,
            "failed_documents": documents_remaining,
        }

    async def _process_documents(
        self,
        dataset_id: str,
        documents: list,
        insert: bool,
        use_json_encoder: bool = True,
        create_id: bool = False,
        verbose: bool = True,
        **kwargs,
    ):
        """
        Called from pull_update_push_async. This method determines via user
        input whether to insert or to update documents in a Dataset. (A
        Dataset that does not exist is automatically created.) Then, the
        operation (either bulk_insert_async or bulk_update_async) is wrapped
        by _apply_bulk_fn and then applied to the given documents.

        Parameters
        ----------
        dataset_id: str
            The dataset_id of the collection to change

        documents: list
            A list of documents

        insert: bool
            If True, inserts rather than updates an already-existing dataset

        use_json_encoder: bool
            Whether to automatically convert documents to json encodable format

        create_id: bool
            If True, creates a indices for the documents

        verbose: bool
            If True, print user-informing statements.

        **kwargs
            Additional arguments for bulk_insert_async or bulk_update_async
        """
        if use_json_encoder:
            documents = self.json_encoder(documents)

        in_dataset = dataset_id in self.datasets.list()["datasets"]
        if not in_dataset or insert:
            operation = f"inserting into {dataset_id}"
            if not in_dataset:
                self.datasets.create(dataset_id)
            if insert:
                create_id = True
            self._convert_id_to_string(documents, create_id=create_id)

            async def bulk_fn(documents):
                return await self.datasets.bulk_insert_async(
                    dataset_id=dataset_id,
                    documents=documents,
                    **{
                        key: value
                        for key, value in kwargs.items()
                        if key not in {"dataset_id", "updates"}
                    },
                )

        else:
            operation = f"updating {dataset_id}"
            self._convert_id_to_string(documents, create_id=create_id)

            async def bulk_fn(documents):
                return await self.datasets.documents.bulk_update_async(
                    dataset_id=dataset_id,
                    updates=documents,
                    **{
                        key: value
                        for key, value in kwargs.items()
                        if key not in {"dataset_id", "documents"}
                    },
                )

        self.logger.info(f"You are currently {operation}")

        self.logger.info(
            "You can track your stats and progress via our dashboard at "
            f"https://cloud.relevance.ai/collections/dashboard/stats/?collection={dataset_id}"
        )

        if verbose:
            print(
                f"While {operation}, you can visit your dashboard at "
                f"https://cloud.relevance.ai/dataset/{dataset_id}/dashboard/monitor/"
            )

        return await self._apply_bulk_fn(bulk_fn=bulk_fn, documents=documents)
