# ---------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# ---------------------------------------------------------

import asyncio
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from promptflow._constants import LINE_NUMBER_KEY, FlowLanguage
from promptflow._core._errors import UnexpectedError
from promptflow._core.operation_context import OperationContext
from promptflow._utils.async_utils import async_run_allowing_running_loop
from promptflow._utils.context_utils import _change_working_dir
from promptflow._utils.execution_utils import (
    apply_default_value_for_input,
    collect_lines,
    get_aggregation_inputs_properties,
    handle_line_failures,
)
from promptflow._utils.logger_utils import bulk_logger
from promptflow._utils.utils import dump_list_to_jsonl, log_progress, resolve_dir_to_absolute, transpose
from promptflow.batch._base_executor_proxy import AbstractExecutorProxy
from promptflow.batch._batch_inputs_processor import BatchInputsProcessor
from promptflow.batch._csharp_executor_proxy import CSharpExecutorProxy
from promptflow.batch._python_executor_proxy import PythonExecutorProxy
from promptflow.batch._result import BatchResult
from promptflow.contracts.flow import Flow
from promptflow.contracts.run_info import Status
from promptflow.exceptions import ErrorTarget, PromptflowException
from promptflow.executor._result import AggregationResult, LineResult
from promptflow.executor.flow_validator import FlowValidator
from promptflow.storage._run_storage import AbstractRunStorage

OUTPUT_FILE_NAME = "output.jsonl"
DEFAULT_CONCURRENCY = 16


class BatchEngine:
    """This class is used to execute flows in batch mode"""

    executor_proxy_classes: Mapping[str, AbstractExecutorProxy] = {
        FlowLanguage.Python: PythonExecutorProxy,
        FlowLanguage.CSharp: CSharpExecutorProxy,
    }

    @classmethod
    def register_executor(cls, type: str, executor_proxy_cls: AbstractExecutorProxy):
        """Register a executor proxy class for a specific program language.

        This method allows users to register a executor proxy class for a particular
        programming language. The executor proxy class will be used when creating an instance
        of the BatchEngine for flows written in the specified language.

        :param type: The flow program language of the executor proxy,
        :type type: str
        :param executor_proxy_cls: The executor proxy class to be registered.
        :type executor_proxy_cls:  ~promptflow.batch.AbstractExecutorProxy
        """
        cls.executor_proxy_classes[type] = executor_proxy_cls

    def __init__(
        self,
        flow_file: Path,
        working_dir: Optional[Path] = None,
        *,
        connections: Optional[dict] = None,
        storage: Optional[AbstractRunStorage] = None,
        **kwargs,
    ):
        """Create a new batch engine instance

        :param flow_file: The flow file path
        :type flow_file: Path
        :param working_dir: The flow working directory path
        :type working_dir: Optional[Path]
        :param connections: The connections used in the flow
        :type connections: Optional[dict]
        :param storage: The storage to store execution results
        :type storage: Optional[~promptflow.storage._run_storage.AbstractRunStorage]
        :param kwargs: The keyword arguments related to creating the executor proxy class
        :type kwargs: Any
        """
        self._working_dir = Flow._resolve_working_dir(flow_file, working_dir)
        self._flow = Flow.from_yaml(flow_file, working_dir=self._working_dir)
        FlowValidator.ensure_flow_valid_in_batch_mode(self._flow)

        executor_proxy_cls = self.executor_proxy_classes[self._flow.program_language]
        with _change_working_dir(self._working_dir):
            self._executor_proxy: AbstractExecutorProxy = executor_proxy_cls.create(
                flow_file, self._working_dir, connections=connections, storage=storage, **kwargs
            )
        self._storage = storage
        # set it to True when the batch run is canceled
        self._is_canceled = False
        # initialize the line results and aggregation results
        self._line_results: List[LineResult] = []
        self._aggr_results = AggregationResult({}, {}, {})

    def run(
        self,
        input_dirs: Dict[str, str],
        inputs_mapping: Dict[str, str],
        output_dir: Path,
        run_id: Optional[str] = None,
        max_lines_count: Optional[int] = None,
        raise_on_line_failure: Optional[bool] = False,
    ) -> BatchResult:
        """Run flow in batch mode

        :param input_dirs: The directories path of input files
        :type input_dirs: Dict[str, str]
        :param inputs_mapping: The mapping of input names to their corresponding values.
        :type inputs_mapping: Dict[str, str]
        :param output_dir: output dir
        :type output_dir: The directory path of output files
        :param run_id: The run id of this run
        :type run_id: Optional[str]
        :param max_lines_count: The max count of inputs. If it is None, all inputs will be used.
        :type max_lines_count: Optional[int]
        :param raise_on_line_failure: Whether to raise exception when a line fails.
        :type raise_on_line_failure: Optional[bool]
        :return: The result of this batch run
        :rtype: ~promptflow.batch._result.BatchResult
        """

        try:
            self._start_time = datetime.utcnow()
            # set batch input source from input mapping
            OperationContext.get_instance().set_batch_input_source_from_inputs_mapping(inputs_mapping)
            # resolve input data from input dirs and apply inputs mapping
            batch_input_processor = BatchInputsProcessor(self._working_dir, self._flow.inputs, max_lines_count)
            batch_inputs = batch_input_processor.process_batch_inputs(input_dirs, inputs_mapping)
            # resolve output dir
            output_dir = resolve_dir_to_absolute(self._working_dir, output_dir)
            # run flow in batch mode
            with _change_working_dir(self._working_dir):
                return async_run_allowing_running_loop(
                    self._exec_in_task, batch_inputs, run_id, output_dir, raise_on_line_failure
                )
        except Exception as e:
            bulk_logger.error(f"Error occurred while executing batch run. Exception: {str(e)}")
            if isinstance(e, PromptflowException):
                raise e
            else:
                # for unexpected error, we need to wrap it to SystemErrorException to allow us to see the stack trace.
                unexpected_error = UnexpectedError(
                    target=ErrorTarget.BATCH,
                    message_format=(
                        "Unexpected error occurred while executing the batch run. Error: {error_type_and_message}."
                    ),
                    error_type_and_message=f"({e.__class__.__name__}) {e}",
                )
                raise unexpected_error from e
        finally:
            self._executor_proxy.destroy()

    def cancel(self):
        """Cancel the batch run"""
        self._is_canceled = True

    async def _exec_in_task(
        self,
        batch_inputs: List[Dict[str, Any]],
        run_id: str = None,
        output_dir: Path = None,
        raise_on_line_failure: bool = False,
    ) -> BatchResult:
        task = asyncio.create_task(self._exec(batch_inputs, run_id, output_dir, raise_on_line_failure))
        while not task.done():
            # check whether the task is completed or canceled every 1s
            await asyncio.sleep(1)
            if self._is_canceled:
                task.cancel()
                # use current completed line results and aggregation results to create a BatchResult
                return BatchResult.create(
                    self._start_time, datetime.utcnow(), self._line_results, self._aggr_results, status=Status.Canceled
                )
        return task.result()

    async def _exec(
        self,
        batch_inputs: List[Dict[str, Any]],
        run_id: str = None,
        output_dir: Path = None,
        raise_on_line_failure: bool = False,
    ) -> BatchResult:
        try:
            # apply default value in early stage, so we can use it both in line and aggregation nodes execution.
            batch_inputs = [
                apply_default_value_for_input(self._flow.inputs, each_line_input) for each_line_input in batch_inputs
            ]
            run_id = run_id or str(uuid.uuid4())
            # execute lines
            if isinstance(self._executor_proxy, PythonExecutorProxy):
                self._line_results = self._executor_proxy._exec_batch(batch_inputs, output_dir, run_id)
            else:
                self._line_results = await self._exec_batch(batch_inputs, run_id)
            handle_line_failures([r.run_info for r in self._line_results], raise_on_line_failure)
            # execute aggregation nodes
            self._aggr_results = await self._exec_aggregation(batch_inputs, self._line_results, run_id)
            # persist outputs to output dir
            outputs = [
                {LINE_NUMBER_KEY: r.run_info.index, **r.output}
                for r in self._line_results
                if r.run_info.status == Status.Completed
            ]
            self._persist_outputs(outputs, output_dir)
            # summary some infos from line results and aggr results to batch result
            return BatchResult.create(self._start_time, datetime.utcnow(), self._line_results, self._aggr_results)
        except asyncio.CancelledError:
            # If the batch run is canceled, log it and ignore the exception.
            bulk_logger.warning("The batch run is canceled.")

    async def _exec_batch(
        self,
        batch_inputs: List[Mapping[str, Any]],
        run_id: Optional[str] = None,
    ) -> List[LineResult]:
        total_lines = len(batch_inputs)
        completed_line = 0
        pending = [
            asyncio.create_task(self._executor_proxy.exec_line_async(line_inputs, i, run_id))
            for i, line_inputs in enumerate(batch_inputs)
        ]

        while completed_line < total_lines:
            try:
                async with asyncio.Semaphore(DEFAULT_CONCURRENCY):
                    done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    self._line_results.extend([task.result() for task in done])
                    log_progress(
                        self._start_time,
                        bulk_logger,
                        len(self._line_results),
                        total_lines,
                        last_log_count=completed_line,
                    )
                    completed_line = len(self._line_results)
            except asyncio.CancelledError:
                break
        return sorted(self._line_results, key=lambda r: r.run_info.index)

    async def _exec_aggregation(
        self,
        batch_inputs: List[dict],
        line_results: List[LineResult],
        run_id: Optional[str] = None,
    ) -> AggregationResult:
        aggregation_nodes = {node.name for node in self._flow.nodes if node.aggregation}
        if not aggregation_nodes:
            return AggregationResult({}, {}, {})

        bulk_logger.info("Executing aggregation nodes...")

        run_infos = [r.run_info for r in line_results]
        succeeded = [i for i, r in enumerate(run_infos) if r.status == Status.Completed]

        succeeded_batch_inputs = [batch_inputs[i] for i in succeeded]
        resolved_succeeded_batch_inputs = [
            FlowValidator.ensure_flow_inputs_type(flow=self._flow, inputs=input) for input in succeeded_batch_inputs
        ]

        succeeded_inputs = transpose(resolved_succeeded_batch_inputs, keys=list(self._flow.inputs.keys()))

        aggregation_inputs = transpose(
            [result.aggregation_inputs for result in line_results],
            keys=get_aggregation_inputs_properties(self._flow),
        )
        succeeded_aggregation_inputs = collect_lines(succeeded, aggregation_inputs)
        try:
            aggr_results = await self._executor_proxy.exec_aggregation_async(
                succeeded_inputs, succeeded_aggregation_inputs, run_id
            )
            bulk_logger.info("Finish executing aggregation nodes.")
            return aggr_results
        except PromptflowException as e:
            # For PromptflowException, we already do classification, so throw directly.
            raise e
        except Exception as e:
            error_type_and_message = f"({e.__class__.__name__}) {e}"
            raise UnexpectedError(
                message_format=(
                    "Unexpected error occurred while executing the aggregated nodes. "
                    "Please fix or contact support for assistance. The error details: {error_type_and_message}."
                ),
                error_type_and_message=error_type_and_message,
            ) from e

    def _persist_outputs(self, outputs: List[Mapping[str, Any]], output_dir: Path):
        """Persist outputs to json line file in output directory"""
        output_file = output_dir / OUTPUT_FILE_NAME
        dump_list_to_jsonl(output_file, outputs)
