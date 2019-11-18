import glob
import os

import pytest

import dask.dataframe as dd

import cudf

import dask_cudf

# import pyarrow.orc as orc

cur_dir = os.path.dirname(__file__)
sample_orc = os.path.join(cur_dir, "sample.orc")


def test_read_orc_defaults():
    df1 = cudf.read_orc(sample_orc)
    df2 = dask_cudf.read_orc(sample_orc)
    dd.assert_eq(df1, df2, check_index=False)


def test_filepath_read_orc_defaults():
    path = "file://%s" % sample_orc
    df1 = cudf.read_orc(path)
    df2 = dask_cudf.read_orc(path)
    dd.assert_eq(df1, df2, check_index=False)


def test_filelist_read_orc_defaults():
    path = [sample_orc]
    df1 = cudf.read_orc(path[0])
    df2 = dask_cudf.read_orc(path)
    dd.assert_eq(df1, df2, check_index=False)


@pytest.mark.parametrize("engine", ["cudf", "pyarrow"])
@pytest.mark.parametrize("columns", [["time", "date"], ["time"]])
def test_read_orc_cols(engine, columns):
    df1 = cudf.read_orc(sample_orc, engine=engine, columns=columns)

    df2 = dask_cudf.read_orc(sample_orc, engine=engine, columns=columns)

    dd.assert_eq(df1, df2, check_index=False)


@pytest.mark.parametrize("compression", [None, "snappy"])
@pytest.mark.parametrize(
    "dtypes",
    [
        {"index": int, "c": str, "a": int},
        {"index": int, "c": int, "a": str},
        {"index": int, "c": int, "a": str, "b": float},
        {"index": int, "c": str, "a": object},
    ],
)
def test_to_orc(tmpdir, dtypes, compression):

    # Create cudf and dask_cudf dataframes
    df = cudf.datasets.randomdata(nrows=10, dtypes=dtypes, seed=1)
    df = df.set_index("index").sort_index()
    ddf = dask_cudf.from_cudf(df, npartitions=3)

    # Write cudf dataframe as single file
    # (preserve index by setting to column)
    fname = tmpdir.join("test.orc")
    df.reset_index().to_orc(fname, compression=compression)

    # Write dask_cudf dataframe as multiple files
    # (preserve index by `write_index=True`)
    dask_cudf.to_orc(
        ddf,
        str(tmpdir),
        write_index=True,
        compression=compression,
        compute=True,
    )

    # Read back cudf dataframe
    df_read = cudf.read_orc(fname).set_index("index")

    # Read back dask_cudf dataframe
    paths = glob.glob(str(tmpdir) + "/part.*.orc")
    ddf_read = dask_cudf.read_orc(paths).set_index("index")

    # Make sure the dask_cudf dataframe matches
    # the cudf dataframes (df and df_read)
    dd.assert_eq(df, ddf_read)
    dd.assert_eq(df_read, ddf_read)
