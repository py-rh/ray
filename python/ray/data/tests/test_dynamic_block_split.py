import os
import time

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

import ray
from ray.data import Dataset
from ray.data._internal.lazy_block_list import LazyBlockList
from ray.data.block import BlockMetadata
from ray.data.datasource import Datasource
from ray.data.datasource.csv_datasource import CSVDatasource
from ray.data.datasource.datasource import Reader, ReadTask
from ray.tests.conftest import *  # noqa


# Data source generates random bytes data
class RandomBytesDatasource(Datasource):
    def create_reader(self, **read_args):
        return RandomBytesReader(
            read_args["num_blocks_per_task"],
            read_args["block_size"],
            read_args.get("use_bytes", True),
        )


class RandomBytesReader(Reader):
    def __init__(self, num_blocks_per_task: int, block_size: int, use_bytes=True):
        self.num_blocks_per_task = num_blocks_per_task
        self.block_size = block_size
        self.use_bytes = use_bytes

    def estimate_inmemory_data_size(self):
        return None

    def get_read_tasks(self, parallelism: int):
        def _blocks_generator():
            for _ in range(self.num_blocks_per_task):
                if self.use_bytes:
                    yield pd.DataFrame({"one": [np.random.bytes(self.block_size)]})
                else:
                    yield pd.DataFrame(
                        {"one": [np.array2string(np.ones(self.block_size, dtype=int))]}
                    )

        return parallelism * [
            ReadTask(
                lambda: _blocks_generator(),
                BlockMetadata(
                    num_rows=self.num_blocks_per_task,
                    size_bytes=self.num_blocks_per_task * self.block_size,
                    schema=None,
                    input_files=None,
                    exec_stats=None,
                ),
            )
        ]


class SlowCSVDatasource(CSVDatasource):
    def _read_stream(self, f: "pa.NativeFile", path: str, **reader_args):
        for block in CSVDatasource._read_stream(self, f, path, **reader_args):
            time.sleep(3)
            yield block


# Tests that we don't block on exponential rampup when doing bulk reads.
# https://github.com/ray-project/ray/issues/20625
@pytest.mark.parametrize("block_split", [False, True])
def test_bulk_lazy_eval_split_mode(shutdown_only, block_split, tmp_path):
    # Defensively shutdown Ray for the first test here to make sure there
    # is no existing Ray cluster.
    ray.shutdown()

    ray.init(num_cpus=8)
    ctx = ray.data.context.DataContext.get_current()

    ray.data.range(8, parallelism=8).write_csv(str(tmp_path))
    if not block_split:
        # Setting infinite block size effectively disables block splitting.
        ctx.target_max_block_size = float("inf")
    ds = ray.data.read_datasource(
        SlowCSVDatasource(), parallelism=8, paths=str(tmp_path)
    )

    start = time.time()
    ds.map(lambda x: x)
    delta = time.time() - start

    print("full read time", delta)
    # Should run in ~3 seconds. It takes >9 seconds if bulk read is broken.
    assert delta < 8, delta


@pytest.mark.parametrize(
    "compute",
    [
        "tasks",
        "actors",
    ],
)
def test_dataset(
    shutdown_only,
    target_max_block_size,
    compute,
):
    if compute == "tasks":
        compute = ray.data._internal.compute.TaskPoolStrategy()
    else:
        compute = ray.data.ActorPoolStrategy()
    ray.shutdown()
    # We need at least 2 CPUs to run a actorpool streaming
    ray.init(num_cpus=2)
    # Test 10 tasks, each task returning 10 blocks, each block has 1 row and each
    # row has 1024 bytes.
    num_blocks_per_task = 10
    block_size = target_max_block_size
    num_tasks = 10

    ds = ray.data.read_datasource(
        RandomBytesDatasource(),
        parallelism=num_tasks,
        num_blocks_per_task=num_blocks_per_task,
        block_size=block_size,
    )
    # Note the following calls to ds will not fully execute it.
    assert ds.schema() is not None
    assert ds.count() == num_blocks_per_task * num_tasks
    assert ds.num_blocks() == num_tasks
    assert ds.size_bytes() >= 0.7 * block_size * num_blocks_per_task * num_tasks

    # Too-large blocks will get split to respect target max block size.
    map_ds = ds.map_batches(lambda x: x, compute=compute)
    map_ds = map_ds.materialize()
    assert map_ds.num_blocks() == num_tasks * num_blocks_per_task
    # Blocks smaller than requested batch size will get coalesced.
    map_ds = ds.map_batches(
        lambda x: {}, batch_size=num_blocks_per_task * num_tasks, compute=compute
    )
    map_ds = map_ds.materialize()
    assert map_ds.num_blocks() == 1
    map_ds = ds.map(lambda x: x, compute=compute)
    map_ds = map_ds.materialize()
    assert map_ds.num_blocks() == num_blocks_per_task * num_tasks

    ds_list = ds.split(5)
    assert len(ds_list) == 5
    for new_ds in ds_list:
        assert new_ds.num_blocks() == num_blocks_per_task * num_tasks / 5

    train, test = ds.train_test_split(test_size=0.25)
    assert train.num_blocks() == num_blocks_per_task * num_tasks * 0.75
    assert test.num_blocks() == num_blocks_per_task * num_tasks * 0.25

    new_ds = ds.union(ds, ds)
    assert new_ds.num_blocks() == num_tasks * 3
    new_ds = new_ds.materialize()
    assert new_ds.num_blocks() == num_blocks_per_task * num_tasks * 3

    new_ds = ds.random_shuffle()
    assert new_ds.num_blocks() == num_tasks
    new_ds = ds.randomize_block_order()
    assert new_ds.num_blocks() == num_tasks
    assert ds.groupby("one").count().count() == num_blocks_per_task * num_tasks

    new_ds = ds.zip(ds)
    new_ds = new_ds.materialize()
    assert new_ds.num_blocks() == num_blocks_per_task * num_tasks

    assert len(ds.take(5)) == 5
    assert len(ds.take_all()) == num_blocks_per_task * num_tasks
    for batch in ds.iter_batches(batch_size=10):
        assert len(batch["one"]) == 10


def test_dataset_pipeline(ray_start_regular_shared, target_max_block_size):
    # Test 10 tasks, each task returning 10 blocks, each block has 1 row and each
    # row has 1024 bytes.
    num_blocks_per_task = 10
    block_size = 1024
    num_tasks = 10

    ds = ray.data.read_datasource(
        RandomBytesDatasource(),
        parallelism=num_tasks,
        num_blocks_per_task=num_blocks_per_task,
        block_size=block_size,
    )
    dsp = ds.window(blocks_per_window=2)
    assert dsp._length == num_tasks / 2

    dsp = dsp.map_batches(lambda x: x)
    result_batches = list(ds.iter_batches(batch_size=5))
    for batch in result_batches:
        assert len(batch["one"]) == 5
    assert len(result_batches) == num_blocks_per_task * num_tasks / 5

    dsp = ds.window(blocks_per_window=2)
    assert dsp._length == num_tasks / 2

    dsp = ds.repeat().map_batches(lambda x: x)
    assert len(dsp.take(5)) == 5


def test_filter(ray_start_regular_shared, target_max_block_size):
    # Test 10 tasks, each task returning 10 blocks, each block has 1 row and each
    # row has 1024 bytes.
    num_blocks_per_task = 10
    block_size = 1024

    ds = ray.data.read_datasource(
        RandomBytesDatasource(),
        parallelism=1,
        num_blocks_per_task=num_blocks_per_task,
        block_size=block_size,
    )

    ds = ds.filter(lambda _: True)
    ds = ds.materialize()
    assert ds.count() == num_blocks_per_task
    assert ds.num_blocks() == num_blocks_per_task

    ds = ds.filter(lambda _: False)
    ds = ds.materialize()
    assert ds.count() == 0
    assert ds.num_blocks() == num_blocks_per_task


def test_lazy_block_list(shutdown_only, target_max_block_size):
    # Test 10 tasks, each task returning 10 blocks, each block has 1 row and each
    # row has 1024 bytes.
    num_blocks_per_task = 10
    block_size = 1024
    num_tasks = 10

    ds = ray.data.read_datasource(
        RandomBytesDatasource(),
        parallelism=num_tasks,
        num_blocks_per_task=num_blocks_per_task,
        block_size=block_size,
    )
    ds.schema()

    # Check internal states of LazyBlockList before execution
    block_list = ds._plan._in_blocks
    block_refs = block_list._block_partition_refs
    cached_metadata = block_list._cached_metadata
    metadata = block_list.get_metadata()

    assert isinstance(block_list, LazyBlockList)
    assert len(block_refs) == num_tasks
    assert block_refs[0] is not None and all(
        map(lambda ref: ref is None, block_refs[1:])
    )
    assert all(map(lambda ref: ref is None, block_list._block_partition_meta_refs))
    assert len(cached_metadata) == num_tasks
    for i, block_metadata in enumerate(cached_metadata):
        if i == 0:
            assert len(block_metadata) == num_blocks_per_task
            for m in block_metadata:
                assert m.num_rows == 1
        else:
            assert block_metadata is None
    assert len(metadata) == num_tasks - 1 + num_blocks_per_task
    for i, block_metadata in enumerate(metadata):
        if i < num_blocks_per_task:
            assert block_metadata.num_rows == 1
            assert block_metadata.schema is not None
        else:
            assert block_metadata.num_rows == num_blocks_per_task
            assert block_metadata.schema is None

    # Check APIs of LazyBlockList
    new_block_list = block_list.copy()
    new_block_list.clear()
    assert len(block_list._block_partition_refs) == num_tasks

    block_lists = block_list.split(2)
    assert len(block_lists) == num_tasks / 2
    assert len(block_lists[0]._block_partition_refs) == 2
    assert len(block_lists[0]._cached_metadata) == 2

    block_lists = block_list.split_by_bytes(block_size * num_blocks_per_task * 2)
    assert len(block_lists) == num_tasks / 2
    assert len(block_lists[0]._block_partition_refs) == 2
    assert len(block_lists[0]._cached_metadata) == 2

    new_block_list = block_list.truncate_by_rows(num_blocks_per_task * 3)
    assert len(new_block_list._block_partition_refs) == 3
    assert len(new_block_list._cached_metadata) == 3

    left_block_list, right_block_list = block_list.divide(3)
    assert len(left_block_list._block_partition_refs) == 3
    assert len(left_block_list._cached_metadata) == 3
    assert len(right_block_list._block_partition_refs) == num_tasks - 3
    assert len(right_block_list._cached_metadata) == num_tasks - 3

    new_block_list = block_list.randomize_block_order()
    assert len(new_block_list._block_partition_refs) == num_tasks
    assert len(new_block_list._cached_metadata) == num_tasks

    output_blocks = block_list.get_blocks_with_metadata()
    assert len(output_blocks) == num_tasks * num_blocks_per_task
    for _, metadata in output_blocks:
        assert metadata.num_rows == 1
    for _, metadata in block_list.iter_blocks_with_metadata():
        assert metadata.num_rows == 1

    # Check internal states of LazyBlockList after execution
    ds = ds.materialize()
    metadata = block_list.get_metadata()

    assert block_list._num_computed() == num_tasks
    assert len(block_refs) == num_tasks
    assert all(map(lambda ref: ref is not None, block_refs))
    assert all(map(lambda ref: ref is None, block_list._block_partition_meta_refs))
    assert len(cached_metadata) == num_tasks
    for block_metadata in cached_metadata:
        assert len(block_metadata) == num_blocks_per_task
        for m in block_metadata:
            assert m.num_rows == 1
    assert len(metadata) == num_tasks * num_blocks_per_task
    for block_metadata in metadata:
        assert block_metadata.num_rows == 1
        assert block_metadata.schema is not None


@pytest.mark.skip("Needs zero-copy optimization for read->map_batches.")
def test_read_large_data(ray_start_cluster):
    # Test 20G input with single task
    num_blocks_per_task = 20
    block_size = 1024 * 1024 * 1024

    cluster = ray_start_cluster
    cluster.add_node(num_cpus=1)

    ray.init(cluster.address)

    def foo(batch):
        return pd.DataFrame({"one": [1]})

    ds = ray.data.read_datasource(
        RandomBytesDatasource(),
        parallelism=1,
        num_blocks_per_task=num_blocks_per_task,
        block_size=block_size,
    )

    ds = ds.map_batches(foo, batch_size=None)
    assert ds.count() == num_blocks_per_task


def _test_write_large_data(
    tmp_path, ext, write_fn, read_fn, use_bytes, write_kwargs=None
):
    # Test 2G input with single task
    num_blocks_per_task = 200
    block_size = 10 * 1024 * 1024

    ds = ray.data.read_datasource(
        RandomBytesDatasource(),
        parallelism=1,
        num_blocks_per_task=num_blocks_per_task,
        block_size=block_size,
        use_bytes=use_bytes,
    )

    # This should succeed without OOM.
    # https://github.com/ray-project/ray/pull/37966.
    out_dir = os.path.join(tmp_path, ext)
    write_kwargs = {} if write_kwargs is None else write_kwargs
    write_fn(ds, out_dir, **write_kwargs)

    max_heap_memory = ds._write_ds._get_stats_summary().get_max_heap_memory()
    assert max_heap_memory < (num_blocks_per_task * block_size / 2), (
        max_heap_memory,
        ext,
    )

    # Make sure we can read out a record.
    if read_fn is not None:
        assert read_fn(out_dir).count() == num_blocks_per_task


def test_write_large_data_parquet(shutdown_only, tmp_path):
    _test_write_large_data(
        tmp_path,
        "parquet",
        Dataset.write_parquet,
        ray.data.read_parquet,
        use_bytes=True,
    )


def test_write_large_data_json(shutdown_only, tmp_path):
    _test_write_large_data(
        tmp_path, "json", Dataset.write_json, ray.data.read_json, use_bytes=False
    )


def test_write_large_data_numpy(shutdown_only, tmp_path):
    _test_write_large_data(
        tmp_path,
        "numpy",
        Dataset.write_numpy,
        ray.data.read_numpy,
        use_bytes=False,
        write_kwargs={"column": "one"},
    )


def test_write_large_data_csv(shutdown_only, tmp_path):
    _test_write_large_data(
        tmp_path, "csv", Dataset.write_csv, ray.data.read_csv, use_bytes=False
    )


def test_write_large_data_tfrecords(shutdown_only, tmp_path):
    _test_write_large_data(
        tmp_path,
        "tfrecords",
        Dataset.write_tfrecords,
        ray.data.read_tfrecords,
        use_bytes=True,
    )


def test_write_large_data_webdataset(shutdown_only, tmp_path):
    _test_write_large_data(
        tmp_path,
        "webdataset",
        Dataset.write_webdataset,
        ray.data.read_webdataset,
        use_bytes=True,
    )


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main(["-v", __file__]))
