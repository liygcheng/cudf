# Copyright (c) 2018-2020, NVIDIA CORPORATION.

import pickle
import warnings
from numbers import Number

import cupy
import numpy as np
import pandas as pd
import pyarrow as pa
from numba import cuda, njit

import nvstrings

import cudf
import cudf._lib as libcudf
from cudf._lib.column import Column
from cudf._lib.null_mask import (
    MaskState,
    bitmask_allocation_size_bytes,
    create_null_mask,
)
from cudf._lib.quantiles import quantile as cpp_quantile
from cudf._lib.scalar import Scalar
from cudf._lib.stream_compaction import unique_count as cpp_unique_count
from cudf._lib.transform import bools_to_mask
from cudf.core.buffer import Buffer
from cudf.core.dtypes import CategoricalDtype
from cudf.utils import cudautils, ioutils, utils
from cudf.utils.dtypes import (
    is_categorical_dtype,
    is_numerical_dtype,
    is_scalar,
    is_string_dtype,
    np_to_pa_dtype,
)
from cudf.utils.utils import buffers_from_pyarrow, mask_dtype


class ColumnBase(Column):
    def __init__(self, data, size, dtype, mask=None, offset=0, children=()):
        """
        Parameters
        ----------
        data : Buffer
        dtype
            The type associated with the data Buffer
        mask : Buffer, optional
        children : tuple, optional
        """
        super().__init__(
            data,
            size=size,
            dtype=dtype,
            mask=mask,
            offset=offset,
            children=children,
        )

    def __reduce__(self):
        return (
            build_column,
            (
                self.base_data,
                self.dtype,
                self.base_mask,
                self.size,
                self.offset,
                self.base_children,
            ),
        )

    def as_frame(self):
        from cudf.core.frame import Frame

        """
        Converts a Column to Frame
        """
        return Frame({None: self.copy(deep=False)})

    @property
    def data_array_view(self):
        """
        View the data as a device array object
        """
        if self.dtype == "object":
            raise ValueError("Cannot get an array view of a StringColumn")

        if is_categorical_dtype(self.dtype):
            return self.codes.data_array_view
        else:
            dtype = self.dtype

        result = cuda.as_cuda_array(self.data)
        # Workaround until `.view(...)` can change itemsize
        # xref: https://github.com/numba/numba/issues/4829
        result = cuda.devicearray.DeviceNDArray(
            shape=(result.nbytes // dtype.itemsize,),
            strides=(dtype.itemsize,),
            dtype=dtype,
            gpu_data=result.gpu_data,
        )
        return result

    @property
    def mask_array_view(self):
        """
        View the mask as a device array
        """
        result = cuda.as_cuda_array(self.mask)
        dtype = mask_dtype

        # Workaround until `.view(...)` can change itemsize
        # xref: https://github.com/numba/numba/issues/4829
        result = cuda.devicearray.DeviceNDArray(
            shape=(result.nbytes // dtype.itemsize,),
            strides=(dtype.itemsize,),
            dtype=dtype,
            gpu_data=result.gpu_data,
        )
        return result

    def __len__(self):
        return self.size

    def to_pandas(self):
        arr = self.data_array_view
        sr = pd.Series(arr.copy_to_host())

        if self.nullable:
            mask_bytes = (
                cudautils.expand_mask_bits(len(self), self.mask_array_view)
                .copy_to_host()
                .astype(bool)
            )
            sr[~mask_bytes] = None
        return sr

    def equals(self, other):
        if self is other:
            return True
        if other is None or len(self) != len(other):
            return False
        if len(self) == 1:
            val = self[0] == other[0]
            # when self is multiindex we need to checkall
            if isinstance(val, np.ndarray):
                return val.all()
            return bool(val)
        return self.binary_operator("eq", other).min()

    def all(self):
        return bool(libcudf.reduce.reduce("all", self, dtype=np.bool_))

    def any(self):
        return bool(libcudf.reduce.reduce("any", self, dtype=np.bool_))

    def __sizeof__(self):
        n = self.data.size
        if self.nullable:
            n += self.mask.size
        return n

    @classmethod
    def _concat(cls, objs, dtype=None):

        if len(objs) == 0:
            dtype = pd.api.types.pandas_dtype(dtype)
            if is_categorical_dtype(dtype):
                dtype = CategoricalDtype()
            return column_empty(0, dtype=dtype, masked=True)

        # If all columns are `NumericalColumn` with different dtypes,
        # we cast them to a common dtype.
        # Notice, we can always cast pure null columns
        not_null_cols = list(filter(lambda o: o.valid_count > 0, objs))
        if len(not_null_cols) > 0 and (
            len(
                [
                    o
                    for o in not_null_cols
                    if not is_numerical_dtype(o.dtype)
                    or np.issubdtype(o.dtype, np.datetime64)
                ]
            )
            == 0
        ):
            col_dtypes = [o.dtype for o in not_null_cols]
            # Use NumPy to find a common dtype
            common_dtype = np.find_common_type(col_dtypes, [])
            # Cast all columns to the common dtype
            for i in range(len(objs)):
                objs[i] = objs[i].astype(common_dtype)

        # Find the first non-null column:
        head = objs[0]
        for i, obj in enumerate(objs):
            if obj.valid_count > 0:
                head = obj
                break

        for i, obj in enumerate(objs):
            # Check that all columns are the same type:
            if not pd.api.types.is_dtype_equal(obj.dtype, head.dtype):
                # if all null, cast to appropriate dtype
                if obj.valid_count == 0:
                    objs[i] = column_empty_like(
                        head, dtype=head.dtype, masked=True, newsize=len(obj)
                    )
                else:
                    raise ValueError("All columns must be the same type")

        cats = None
        is_categorical = all(is_categorical_dtype(o.dtype) for o in objs)

        # Combine CategoricalColumn categories
        if is_categorical:
            # Combine and de-dupe the categories
            cats = (
                cudf.concat([o.cat().categories for o in objs])
                .to_series()
                .drop_duplicates()
                ._column
            )
            objs = [
                o.cat()._set_categories(cats, is_unique=True) for o in objs
            ]
            # Map `objs` into a list of the codes until we port Categorical to
            # use the libcudf++ Category data type.
            objs = [o.cat().codes._column for o in objs]
            head = head.cat().codes._column

        newsize = sum(map(len, objs))
        if newsize > libcudf.MAX_COLUMN_SIZE:
            raise MemoryError(
                "Result of concat cannot have "
                "size > {}".format(libcudf.MAX_COLUMN_SIZE_STR)
            )

        # Filter out inputs that have 0 length
        objs = [o for o in objs if len(o) > 0]

        # Perform the actual concatenation
        if newsize > 0:
            col = libcudf.concat.concat_columns(objs)
        else:
            col = column_empty(0, head.dtype, masked=True)

        if is_categorical:
            col = build_categorical_column(
                categories=cats,
                codes=as_column(col.base_data, dtype=col.dtype),
                mask=col.base_mask,
                size=col.size,
                offset=col.offset,
            )

        return col

    def dropna(self):
        dropped_col = self.as_frame().dropna()._as_column()
        return dropped_col

    def _get_mask_as_column(self):
        data = Buffer(cupy.ones(len(self), dtype=np.bool_))
        mask = as_column(data=data)
        if self.nullable:
            mask = mask.set_mask(self._mask).fillna(False)
        return mask

    def _memory_usage(self, **kwargs):
        return self.__sizeof__()

    def to_gpu_array(self, fillna=None):
        """Get a dense numba device array for the data.

        Parameters
        ----------
        fillna : scalar, 'pandas', or None
            See *fillna* in ``.to_array``.

        Notes
        -----

        if ``fillna`` is ``None``, null values are skipped.  Therefore, the
        output size could be smaller.
        """
        if fillna:
            return self.fillna(self.default_na_value()).data_array_view
        else:
            return self.dropna().data_array_view

    def to_array(self, fillna=None):
        """Get a dense numpy array for the data.

        Parameters
        ----------
        fillna : scalar, 'pandas', or None
            Defaults to None, which will skip null values.
            If it equals "pandas", null values are filled with NaNs.
            Non integral dtype is promoted to np.float64.

        Notes
        -----

        if ``fillna`` is ``None``, null values are skipped.  Therefore, the
        output size could be smaller.
        """
        return self.to_gpu_array(fillna=fillna).copy_to_host()

    def _fill(self, fill_value, begin=0, end=-1, inplace=False):
        if end <= begin or begin >= self.size:
            return self if inplace else self.copy()

        if is_categorical_dtype(self.dtype):
            return self._fill_categorical(fill_value, begin, end, inplace)

        fill_scalar = Scalar(fill_value, self.dtype)

        if not inplace:
            return libcudf.filling.fill(self, begin, end, fill_scalar)

        if is_string_dtype(self.dtype):
            return self._mimic_inplace(
                libcudf.filling.fill(self, begin, end, fill_scalar),
                inplace=True,
            )

        if fill_value is None and not self.nullable:
            mask = create_null_mask(self.size, state=MaskState.ALL_VALID)
            self.set_base_mask(mask)

        libcudf.filling.fill_in_place(self, begin, end, fill_scalar)

        return self

    def _fill_categorical(self, fill_value, begin, end, inplace):
        fill_code = self._encode(fill_value)
        fill_scalar = Scalar(fill_code, self.codes.dtype)

        result = self if inplace else self.copy()

        libcudf.filling.fill_in_place(result.codes, begin, end, fill_scalar)
        return result

    def shift(self, offset, fill_value):
        return libcudf.copying.shift(self, offset, fill_value)

    @property
    def valid_count(self):
        """Number of non-null values"""
        return len(self) - self.null_count

    @property
    def nullmask(self):
        """The gpu buffer for the null-mask
        """
        if self.nullable:
            return self.mask_array_view
        else:
            raise ValueError("Column has no null mask")

    def copy(self, deep=True):
        """Columns are immutable, so a deep copy produces a copy of the
        underlying data and mask and a shallow copy creates a new column and
        copies the references of the data and mask.
        """
        if deep:
            return libcudf.copying.copy_column(self)
        else:
            return build_column(
                self.base_data,
                self.dtype,
                mask=self.base_mask,
                size=self.size,
                offset=self.offset,
                children=self.base_children,
            )

    def view(self, newcls, **kwargs):
        """View the underlying column data differently using a subclass of
        ColumnBase

        Parameters
        ----------
        newcls : ColumnBase
            The logical view to be used
        **kwargs :
            Additional paramters for instantiating instance of *newcls*.
            Valid keywords are valid parameters for ``newcls.__init__``.
            Any omitted keywords will be defaulted to the corresponding
            attributes in ``self``.
        """
        params = Column._replace_defaults(self)
        params.update(kwargs)
        if "mask" in kwargs and "null_count" not in kwargs:
            del params["null_count"]
        return newcls(**params)

    def element_indexing(self, index):
        """Default implementation for indexing to an element

        Raises
        ------
        ``IndexError`` if out-of-bound
        """
        index = np.int32(index)
        if index < 0:
            index = len(self) + index
        if index > len(self) - 1:
            raise IndexError

        val = self[index : (index + 1)]
        if val.null_count == 1:
            val = None
        else:
            val = val.to_array()[0]

        return val

    def __getitem__(self, arg):
        from cudf.core.column import column

        if isinstance(arg, Number):
            arg = int(arg)
            return self.element_indexing(arg)
        elif isinstance(arg, slice):

            if is_categorical_dtype(self):
                codes = self.codes[arg]
                return build_categorical_column(
                    categories=self.categories,
                    codes=as_column(codes.base_data, dtype=codes.dtype),
                    mask=codes.base_mask,
                    ordered=self.ordered,
                    size=codes.size,
                    offset=codes.offset,
                )

            start, stop, stride = arg.indices(len(self))

            if start < 0:
                start = start + len(self)
            if stop < 0:
                stop = stop + len(self)

            if start >= stop:
                return column_empty(0, self.dtype, masked=True)
            # compute mask slice
            if stride == 1 or stride is None:

                return libcudf.copying.column_slice(self, [start, stop])[0]
            else:
                # Need to create a gather map for given slice with stride
                gather_map = as_column(
                    cupy.arange(
                        start=start,
                        stop=stop,
                        step=stride,
                        dtype=np.dtype(np.int32),
                    )
                )
                return self.take(gather_map)
        else:
            arg = column.as_column(arg)
            if len(arg) == 0:
                arg = column.as_column([], dtype="int32")
            if pd.api.types.is_integer_dtype(arg.dtype):
                return self.take(arg)
            if pd.api.types.is_bool_dtype(arg.dtype):
                return self.apply_boolean_mask(arg)
            raise NotImplementedError(type(arg))

    def __setitem__(self, key, value):
        """
        Set the value of self[key] to value.

        If value and self are of different types,
        value is coerced to self.dtype
        """
        from cudf.core import column

        if isinstance(key, slice):
            key_start, key_stop, key_stride = key.indices(len(self))
            if key_start < 0:
                key_start = key_start + len(self)
            if key_stop < 0:
                key_stop = key_stop + len(self)
            if key_start >= key_stop:
                return self.copy()
            if (key_stride is None or key_stride == 1) and is_scalar(value):
                return self._fill(value, key_start, key_stop, inplace=True)
            if key_stride != 1 or key_stride is not None or is_scalar(value):
                key = as_column(
                    cupy.arange(
                        start=key_start,
                        stop=key_stop,
                        step=key_stride,
                        dtype=np.dtype(np.int32),
                    )
                )
                nelem = len(key)
            else:
                nelem = abs(key_stop - key_start)
        else:
            key = column.as_column(key)
            if pd.api.types.is_bool_dtype(key.dtype):
                if not len(key) == len(self):
                    raise ValueError(
                        "Boolean mask must be of same length as column"
                    )
                key = column.as_column(cupy.arange(len(self)))[key]
            nelem = len(key)

        if is_scalar(value):
            if is_categorical_dtype(self.dtype):
                value = self._encode(value)
            else:
                value = self.dtype.type(value) if value is not None else value
        else:
            if len(value) != nelem:
                msg = (
                    f"Size mismatch: cannot set value "
                    f"of size {len(value)} to indexing result of size "
                    f"{nelem}"
                )
                raise ValueError(msg)
            value = column.as_column(value).astype(self.dtype)
            if is_categorical_dtype(value.dtype):
                value = value.cat().set_categories(self.categories)
                assert self.dtype == value.dtype

        if (
            isinstance(key, slice)
            and (key_stride == 1 or key_stride is None)
            and not is_scalar(value)
        ):

            out = libcudf.copying.copy_range(
                value, self, 0, nelem, key_start, key_stop, False
            )
            if is_categorical_dtype(value.dtype):
                out = build_categorical_column(
                    categories=value.categories,
                    codes=as_column(out.base_data, dtype=out.dtype),
                    mask=out.base_mask,
                    size=out.size,
                    offset=out.offset,
                    ordered=value.ordered,
                )
        else:
            try:
                if is_scalar(value):
                    input = self
                    if is_categorical_dtype(self.dtype):
                        input = self.codes

                    out = input.as_frame()._scatter(key, [value])._as_column()

                    if is_categorical_dtype(self.dtype):
                        out = build_categorical_column(
                            categories=self.categories,
                            codes=as_column(out.base_data, dtype=out.dtype),
                            mask=out.base_mask,
                            size=out.size,
                            offset=out.offset,
                            ordered=self.ordered,
                        )

                else:
                    if not isinstance(value, Column):
                        value = as_column(value)
                    out = (
                        self.as_frame()
                        ._scatter(key, value.as_frame())
                        ._as_column()
                    )
            except RuntimeError as e:
                if "out of bounds" in str(e):
                    raise IndexError(
                        f"index out of bounds for column of size {len(self)}"
                    ) from e
                raise

        self._mimic_inplace(out, inplace=True)

    def fillna(self, value):
        """Fill null values with ``value``.

        Returns a copy with null filled.
        """
        raise NotImplementedError

    def isnull(self):
        """Identify missing values in a Column.
        """
        return libcudf.unary.is_null(self)

    def isna(self):
        """Identify missing values in a Column. Alias for isnull.
        """
        return self.isnull()

    def notnull(self):
        """Identify non-missing values in a Column.
        """
        return libcudf.unary.is_valid(self)

    def notna(self):
        """Identify non-missing values in a Column. Alias for notnull.
        """
        return self.notnull()

    def find_first_value(self, value):
        """
        Returns offset of first value that matches
        """
        # FIXME: Inefficient find in CPU code
        arr = self.to_array()
        indices = np.argwhere(arr == value)
        if not len(indices):
            raise ValueError("value not found")
        return indices[-1, 0]

    def find_last_value(self, value):
        """
        Returns offset of last value that matches
        """
        arr = self.to_array()
        indices = np.argwhere(arr == value)
        if not len(indices):
            raise ValueError("value not found")
        return indices[-1, 0]

    def append(self, other):
        from cudf.core.column import as_column

        return ColumnBase._concat([self, as_column(other)])

    def quantile(self, q, interpolation, exact):

        is_number = isinstance(q, Number)

        if is_number:
            quant = [float(q)]
        elif isinstance(q, list) or isinstance(q, np.ndarray):
            quant = q
        else:
            msg = "`q` must be either a single element, list or numpy array"
            raise TypeError(msg)

        # get sorted indicies and exclude nulls
        sorted_indices = self.as_frame()._get_sorted_inds(True, "after")
        sorted_indices = sorted_indices[self.null_count :]

        return cpp_quantile(self, quant, interpolation, sorted_indices, exact)

    def take(self, indices, keep_index=True):
        """Return Column by taking values from the corresponding *indices*.
        """
        # Handle zero size
        if indices.size == 0:
            return column_empty_like(self, newsize=0)
        try:
            return (
                self.as_frame()
                ._gather(indices, keep_index=keep_index)
                ._as_column()
            )
        except RuntimeError as e:
            if "out of bounds" in str(e):
                raise IndexError(
                    f"index out of bounds for column of size {len(self)}"
                ) from e
            raise

    def isin(self, values):
        """Check whether values are contained in the Column.

        Parameters
        ----------
        values : set or list-like
            The sequence of values to test. Passing in a single string will
            raise a TypeError. Instead, turn a single string into a list
            of one element.
        use_name : bool
            If ``True`` then combine hashed column values
            with hashed column name. This is useful for when the same
            values in different columns should be encoded
            with different hashed values.
        Returns
        -------
        result: Column
            Column of booleans indicating if each element is in values.
        Raises
        -------
        TypeError
            If values is a string
        """
        if is_scalar(values):
            raise TypeError(
                "only list-like objects are allowed to be passed "
                f"to isin(), you passed a [{type(values).__name__}]"
            )

        from cudf import DataFrame, Series

        lhs = self
        rhs = None

        try:
            # We need to convert values to same type as self,
            # hence passing dtype=self.dtype
            rhs = as_column(values, dtype=self.dtype)
        except ValueError:
            # pandas functionally returns all False when cleansing via
            # typecasting fails
            return as_column(cupy.zeros(len(self), dtype="bool"))

        # If categorical, combine categories first
        if is_categorical_dtype(lhs):
            lhs_cats = lhs.cat().categories._values
            rhs_cats = rhs.cat().categories._values
            if np.issubdtype(rhs_cats.dtype, lhs_cats.dtype):
                # if the categories are the same dtype, we can combine them
                cats = Series(lhs_cats.append(rhs_cats)).drop_duplicates()
                lhs = lhs.cat().set_categories(cats, is_unique=True)
                rhs = rhs.cat().set_categories(cats, is_unique=True)
            else:
                # If they're not the same dtype, short-circuit if the values
                # list doesn't have any nulls. If it does have nulls, make
                # the values list a Categorical with a single null
                if not rhs.has_nulls:
                    return cupy.zeros(len(self), dtype="bool")
                rhs = as_column(pd.Categorical.from_codes([-1], categories=[]))
                rhs = rhs.cat().set_categories(lhs_cats).astype(self.dtype)

        lhs = DataFrame({"x": lhs, "orig_order": cupy.arange(len(lhs))})
        rhs = DataFrame({"x": rhs, "bool": cupy.ones(len(rhs), "bool")})
        res = lhs.merge(rhs, on="x", how="left").sort_values(by="orig_order")
        res = res.drop_duplicates(subset="orig_order").reset_index(drop=True)
        res = res["bool"].fillna(False)

        return res._column

    def as_mask(self):
        """Convert booleans to bitmask

        Returns
        -------
        Buffer
        """

        if self.has_nulls:
            raise ValueError("Column must have no nulls.")

        return bools_to_mask(self)

    @ioutils.doc_to_dlpack()
    def to_dlpack(self):
        """{docstring}"""
        import cudf.io.dlpack as dlpack

        return dlpack.to_dlpack(self)

    @property
    def is_unique(self):
        return self.unique_count() == len(self)

    @property
    def is_monotonic(self):
        return self.is_monotonic_increasing

    @property
    def is_monotonic_increasing(self):
        if not hasattr(self, "_is_monotonic_increasing"):
            if self.has_nulls:
                self._is_monotonic_increasing = False
            else:
                self._is_monotonic_increasing = self.as_frame()._is_sorted(
                    ascending=None, null_position=None
                )
        return self._is_monotonic_increasing

    @property
    def is_monotonic_decreasing(self):
        if not hasattr(self, "_is_monotonic_decreasing"):
            if self.has_nulls:
                self._is_monotonic_decreasing = False
            else:
                self._is_monotonic_decreasing = self.as_frame()._is_sorted(
                    ascending=[False], null_position=None
                )
        return self._is_monotonic_decreasing

    def get_slice_bound(self, label, side, kind):
        """
        Calculate slice bound that corresponds to given label.
        Returns leftmost (one-past-the-rightmost if ``side=='right'``) position
        of given label.
        Parameters
        ----------
        label : object
        side : {'left', 'right'}
        kind : {'ix', 'loc', 'getitem'}
        """
        assert kind in ["ix", "loc", "getitem", None]
        if side not in ("left", "right"):
            raise ValueError(
                "Invalid value for side kwarg,"
                " must be either 'left' or 'right': %s" % (side,)
            )

        # TODO: Handle errors/missing keys correctly
        #       Not currently using `kind` argument.
        if side == "left":
            return self.find_first_value(label, closest=True)
        if side == "right":
            return self.find_last_value(label, closest=True) + 1

    def sort_by_values(self, ascending=True, na_position="last"):
        col_inds = self.as_frame()._get_sorted_inds(ascending, na_position)
        col_keys = self[col_inds]
        return col_keys, col_inds

    def unique_count(self, method="sort", dropna=True):
        if method != "sort":
            msg = "non sort based unique_count() not implemented yet"
            raise NotImplementedError(msg)
        return cpp_unique_count(self, ignore_nulls=dropna)

    def astype(self, dtype, **kwargs):
        if is_categorical_dtype(dtype):
            return self.as_categorical_column(dtype, **kwargs)
        elif pd.api.types.pandas_dtype(dtype).type in (np.str_, np.object_):
            return self.as_string_column(dtype, **kwargs)

        elif np.issubdtype(dtype, np.datetime64):
            return self.as_datetime_column(dtype, **kwargs)

        else:
            return self.as_numerical_column(dtype, **kwargs)

    def as_categorical_column(self, dtype, **kwargs):
        if "ordered" in kwargs:
            ordered = kwargs["ordered"]
        else:
            ordered = False

        sr = cudf.Series(self)
        labels, cats = sr.factorize()

        # columns include null index in factorization; remove:
        if self.has_nulls:
            cats = cats.dropna()
            labels = labels - 1

        return build_categorical_column(
            categories=cats._column,
            codes=labels._column,
            mask=self.mask,
            ordered=ordered,
        )

    def as_numerical_column(self, dtype, **kwargs):
        raise NotImplementedError

    def as_datetime_column(self, dtype, **kwargs):
        raise NotImplementedError

    def as_string_column(self, dtype, **kwargs):
        raise NotImplementedError

    def apply_boolean_mask(self, mask):
        mask = as_column(mask, dtype="bool")
        result = (
            self.as_frame()._apply_boolean_mask(boolean_mask=mask)._as_column()
        )
        return result

    def argsort(self, ascending):
        _, inds = self.sort_by_values(ascending=ascending)
        return inds

    @property
    def __cuda_array_interface__(self):
        output = {
            "shape": (len(self),),
            "strides": (self.dtype.itemsize,),
            "typestr": self.dtype.str,
            "data": (self.data_ptr, False),
            "version": 1,
        }

        if self.nullable and self.has_nulls:
            from types import SimpleNamespace

            # Create a simple Python object that exposes the
            # `__cuda_array_interface__` attribute here since we need to modify
            # some of the attributes from the numba device array
            mask = SimpleNamespace(
                __cuda_array_interface__={
                    "shape": (len(self),),
                    "typestr": "<t1",
                    "data": (self.mask_ptr, True),
                    "version": 1,
                }
            )
            output["mask"] = mask

        return output

    def searchsorted(
        self, value, side="left", ascending=True, na_position="last"
    ):
        values = as_column(value).as_frame()
        return self.as_frame().searchsorted(
            values, side, ascending=ascending, na_position=na_position
        )

    def unique(self):
        """
        Get unique values in the data
        """
        return self.as_frame().drop_duplicates(keep="first")._as_column()

    def serialize(self):
        header = {}
        frames = []
        header["type-serialized"] = pickle.dumps(type(self))
        header["dtype"] = self.dtype.str

        data_header, data_frames = self.data.serialize()
        header["data"] = data_header
        frames.extend(data_frames)

        if self.nullable:
            mask_header, mask_frames = self.mask.serialize()
            header["mask"] = mask_header
            frames.extend(mask_frames)

        header["frame_count"] = len(frames)
        return header, frames

    @classmethod
    def deserialize(cls, header, frames):
        dtype = header["dtype"]
        data = Buffer.deserialize(header["data"], [frames[0]])
        mask = None
        if "mask" in header:
            mask = Buffer.deserialize(header["mask"], [frames[1]])
        return build_column(data=data, dtype=dtype, mask=mask)


def column_empty_like(column, dtype=None, masked=False, newsize=None):
    """Allocate a new column like the given *column*
    """
    if dtype is None:
        dtype = column.dtype
    row_count = len(column) if newsize is None else newsize

    if (
        hasattr(column, "dtype")
        and is_categorical_dtype(column.dtype)
        and dtype == column.dtype
    ):
        codes = column_empty_like(column.codes, masked=masked, newsize=newsize)
        return build_column(
            data=None,
            dtype=dtype,
            mask=codes.base_mask,
            children=(as_column(codes.base_data, dtype=codes.dtype),),
            size=codes.size,
        )

    return column_empty(row_count, dtype, masked)


def column_empty_like_same_mask(column, dtype):
    """Create a new empty Column with the same length and the same mask.

    Parameters
    ----------
    dtype : np.dtype like
        The dtype of the data buffer.
    """
    result = column_empty_like(column, dtype)
    if column.nullable:
        result = result.set_mask(column.mask)
    return result


def column_empty(row_count, dtype="object", masked=False):
    """Allocate a new column like the given row_count and dtype.
    """
    dtype = pd.api.types.pandas_dtype(dtype)
    children = ()

    if is_categorical_dtype(dtype):
        data = None
        children = (
            build_column(
                data=Buffer.empty(row_count * np.dtype("int32").itemsize),
                dtype="int32",
            ),
        )
    elif dtype.kind in "OU":
        data = None
        children = (
            build_column(
                data=Buffer(cupy.zeros(row_count + 1, dtype="int32")),
                dtype="int32",
            ),
            build_column(
                data=Buffer.empty(row_count * np.dtype("int8").itemsize),
                dtype="int8",
            ),
        )
    else:
        data = Buffer.empty(row_count * dtype.itemsize)

    if masked:
        mask = create_null_mask(row_count, state=MaskState.ALL_NULL)
    else:
        mask = None

    return build_column(data, dtype, mask=mask, children=children)


def build_column(data, dtype, mask=None, size=None, offset=0, children=()):
    """
    Build a Column of the appropriate type from the given parameters

    Parameters
    ----------
    data : Buffer
        The data buffer (can be None if constructin certain Column
        types like StringColumn or CategoricalColumn)
    dtype
        The dtype associated with the Column to construct
    mask : Buffer, optionapl
        The mask buffer
    size : int, optional
    offset : int, optional
    children : tuple, optional
    """
    from cudf.core.column.numerical import NumericalColumn
    from cudf.core.column.datetime import DatetimeColumn
    from cudf.core.column.categorical import CategoricalColumn
    from cudf.core.column.string import StringColumn

    dtype = pd.api.types.pandas_dtype(dtype)

    if is_categorical_dtype(dtype):
        if not len(children) == 1:
            raise ValueError(
                "Must specify exactly one child column for CategoricalColumn"
            )
        if not isinstance(children[0], ColumnBase):
            raise TypeError("children must be a tuple of Columns")
        return CategoricalColumn(
            dtype=dtype, mask=mask, size=size, offset=offset, children=children
        )
    elif dtype.type is np.datetime64:
        return DatetimeColumn(
            data=data, dtype=dtype, mask=mask, size=size, offset=offset
        )
    elif dtype.type in (np.object_, np.str_):
        return StringColumn(
            mask=mask, size=size, offset=offset, children=children
        )
    else:
        return NumericalColumn(
            data=data, dtype=dtype, mask=mask, size=size, offset=offset
        )


def build_categorical_column(
    categories, codes, mask=None, size=None, offset=0, ordered=None
):
    """
    Build a CategoricalColumn

    Parameters
    ----------
    categories : Column
        Column of categories
    codes : Column
        Column of codes, the size of the resulting Column will be
        the size of `codes`
    mask : Buffer
        Null mask
    size : int, optional
    offset : int, optional
    ordered : bool
        Indicates whether the categories are ordered
    """
    if len(categories) == 0 and len(codes):
        raise ValueError("Cannot have nonempty codes for empty categories")

    dtype = CategoricalDtype(categories=as_column(categories), ordered=ordered)

    return build_column(
        data=None,
        dtype=dtype,
        mask=mask,
        size=size,
        offset=offset,
        children=(as_column(codes),),
    )


def as_column(arbitrary, nan_as_null=None, dtype=None, length=None):
    """Create a Column from an arbitrary object

    Parameters
    ----------
    arbitrary : object
        Object to construct the Column from. See *Notes*.
    nan_as_null : bool, optional, default None
        If None (default), treats NaN values in arbitrary as null if there is
        no mask passed along with it. If True, combines the mask and NaNs to
        form a new validity mask. If False, leaves NaN values as is.
    dtype : optional
        Optionally typecast the construted Column to the given
        dtype.
    length : int, optional
        If `arbitrary` is a scalar, broadcast into a Column of
        the given length.

    Returns
    -------
    A Column of the appropriate type and size.

    Notes
    -----
    Currently support inputs are:

    * ``Column``
    * ``Series``
    * ``Index``
    * Scalars (can be broadcasted to a specified `length`)
    * Objects exposing ``__cuda_array_interface__`` (e.g., numba device arrays)
    * Objects exposing ``__array_interface__``(e.g., numpy arrays)
    * pyarrow array
    * pandas.Categorical objects
    """

    from cudf.core.column import numerical, categorical, datetime, string
    from cudf.core.series import Series
    from cudf.core.index import Index

    if isinstance(arbitrary, ColumnBase):
        if dtype is not None:
            return arbitrary.astype(dtype)
        else:
            return arbitrary

    elif isinstance(arbitrary, Series):
        data = arbitrary._column
        if dtype is not None:
            data = data.astype(dtype)
    elif isinstance(arbitrary, Index):
        data = arbitrary._values
        if dtype is not None:
            data = data.astype(dtype)
    # TODO: Remove nvstrings here when nvstrings is fully removed
    elif isinstance(arbitrary, nvstrings.nvstrings):
        byte_count = arbitrary.byte_count()
        if byte_count > libcudf.MAX_STRING_COLUMN_BYTES:
            raise MemoryError(
                "Cannot construct string columns "
                "containing > {} bytes. "
                "Consider using dask_cudf to partition "
                "your data.".format(libcudf.MAX_STRING_COLUMN_BYTES_STR)
            )
        sbuf = Buffer.empty(arbitrary.byte_count())
        obuf = Buffer.empty(
            (arbitrary.size() + 1) * np.dtype("int32").itemsize
        )

        nbuf = None
        if arbitrary.null_count() > 0:
            nbuf = create_null_mask(
                arbitrary.size(), state=MaskState.UNINITIALIZED
            )
            arbitrary.set_null_bitmask(nbuf.ptr, bdevmem=True)
        arbitrary.to_offsets(sbuf.ptr, obuf.ptr, None, bdevmem=True)
        children = (
            build_column(obuf, dtype="int32"),
            build_column(sbuf, dtype="int8"),
        )
        data = build_column(
            data=None, dtype="object", mask=nbuf, children=children
        )
        data._nvstrings = arbitrary

    elif isinstance(arbitrary, Buffer):
        if dtype is None:
            raise TypeError(f"dtype cannot be None if 'arbitrary' is a Buffer")
        data = build_column(arbitrary, dtype=dtype)

    elif hasattr(arbitrary, "__cuda_array_interface__"):
        desc = arbitrary.__cuda_array_interface__
        current_dtype = np.dtype(desc["typestr"])
        data = _data_from_cuda_array_interface_desc(arbitrary)
        mask = _mask_from_cuda_array_interface_desc(arbitrary)
        col = build_column(data, dtype=current_dtype, mask=mask)

        if dtype is not None:
            col = col.astype(dtype)

        if np.issubdtype(col.dtype, np.floating):
            if nan_as_null or (mask is None and nan_as_null is None):
                mask = libcudf.transform.nans_to_nulls(col.fillna(np.nan))
                col = col.set_mask(mask)
        elif np.issubdtype(col.dtype, np.datetime64):
            if nan_as_null or (mask is None and nan_as_null is None):
                col = utils.time_col_replace_nulls(col)
        return col

    elif isinstance(arbitrary, pa.Array):
        if isinstance(arbitrary, pa.StringArray):
            pa_size, pa_offset, nbuf, obuf, sbuf = buffers_from_pyarrow(
                arbitrary
            )
            children = (
                build_column(data=obuf, dtype="int32"),
                build_column(data=sbuf, dtype="int8"),
            )

            data = string.StringColumn(
                mask=nbuf, children=children, size=pa_size, offset=pa_offset
            )

        elif isinstance(arbitrary, pa.NullArray):
            new_dtype = pd.api.types.pandas_dtype(dtype)
            if (type(dtype) == str and dtype == "empty") or dtype is None:
                new_dtype = pd.api.types.pandas_dtype(
                    arbitrary.type.to_pandas_dtype()
                )

            if is_categorical_dtype(new_dtype):
                arbitrary = arbitrary.dictionary_encode()
            else:
                if nan_as_null:
                    arbitrary = arbitrary.cast(np_to_pa_dtype(new_dtype))
                else:
                    # casting a null array doesn't make nans valid
                    # so we create one with valid nans from scratch:
                    if new_dtype == np.dtype("object"):
                        arbitrary = utils.scalar_broadcast_to(
                            None, (len(arbitrary),), dtype=new_dtype
                        )
                    else:
                        arbitrary = utils.scalar_broadcast_to(
                            np.nan, (len(arbitrary),), dtype=new_dtype
                        )
            data = as_column(arbitrary, nan_as_null=nan_as_null)
        elif isinstance(arbitrary, pa.DictionaryArray):
            codes = as_column(arbitrary.indices)
            if isinstance(arbitrary.dictionary, pa.NullArray):
                categories = as_column([], dtype="object")
            else:
                categories = as_column(arbitrary.dictionary)
            dtype = CategoricalDtype(
                categories=categories, ordered=arbitrary.type.ordered
            )
            data = categorical.CategoricalColumn(
                dtype=dtype,
                mask=codes.base_mask,
                children=(codes,),
                size=codes.size,
                offset=codes.offset,
            )
        elif isinstance(arbitrary, pa.TimestampArray):
            dtype = np.dtype("M8[{}]".format(arbitrary.type.unit))
            pa_size, pa_offset, pamask, padata, _ = buffers_from_pyarrow(
                arbitrary, dtype=dtype
            )

            data = datetime.DatetimeColumn(
                data=padata,
                mask=pamask,
                dtype=dtype,
                size=pa_size,
                offset=pa_offset,
            )
        elif isinstance(arbitrary, pa.Date64Array):
            raise NotImplementedError
            pa_size, pa_offset, pamask, padata, _ = buffers_from_pyarrow(
                arbitrary, dtype="M8[ms]"
            )
            data = datetime.DatetimeColumn(
                data=padata,
                mask=pamask,
                dtype=np.dtype("M8[ms]"),
                size=pa_size,
                offset=pa_offset,
            )
        elif isinstance(arbitrary, pa.Date32Array):
            # No equivalent np dtype and not yet supported
            warnings.warn(
                "Date32 values are not yet supported so this will "
                "be typecast to a Date64 value",
                UserWarning,
            )
            data = as_column(arbitrary.cast(pa.int32())).astype("M8[ms]")
        elif isinstance(arbitrary, pa.BooleanArray):
            # Arrow uses 1 bit per value while we use int8
            dtype = np.dtype(np.bool)
            # Needed because of bug in PyArrow
            # https://issues.apache.org/jira/browse/ARROW-4766
            if len(arbitrary) > 0:
                arbitrary = arbitrary.cast(pa.int8())
            else:
                arbitrary = pa.array([], type=pa.int8())

            pa_size, pa_offset, pamask, padata, _ = buffers_from_pyarrow(
                arbitrary, dtype=dtype
            )
            data = numerical.NumericalColumn(
                data=padata,
                mask=pamask,
                dtype=dtype,
                size=pa_size,
                offset=pa_offset,
            )
        elif isinstance(arbitrary, pa.ListArray):
            raise NotImplementedError(
                "cudf doesn't support list like data types"
            )

        else:
            pa_size, pa_offset, pamask, padata, _ = buffers_from_pyarrow(
                arbitrary
            )
            data = numerical.NumericalColumn(
                data=padata,
                dtype=np.dtype(arbitrary.type.to_pandas_dtype()),
                mask=pamask,
                size=pa_size,
                offset=pa_offset,
            )

    elif isinstance(arbitrary, pa.ChunkedArray):
        gpu_cols = [
            as_column(chunk, dtype=dtype) for chunk in arbitrary.chunks
        ]

        if dtype and dtype != "empty":
            new_dtype = dtype
        else:
            pa_type = arbitrary.type
            if pa.types.is_dictionary(pa_type):
                new_dtype = "category"
            else:
                new_dtype = np.dtype(pa_type.to_pandas_dtype())

        data = ColumnBase._concat(gpu_cols, dtype=new_dtype)

    elif isinstance(arbitrary, (pd.Series, pd.Categorical)):
        if is_categorical_dtype(arbitrary):
            data = as_column(pa.array(arbitrary, from_pandas=True))
        elif arbitrary.dtype == np.bool:
            # Bug in PyArrow or HDF that requires us to do this
            data = as_column(
                pa.array(np.asarray(arbitrary), from_pandas=True),
                dtype=arbitrary.dtype,
            )
        else:
            data = as_column(
                pa.array(arbitrary, from_pandas=nan_as_null),
                dtype=arbitrary.dtype,
            )
        if dtype is not None:
            data = data.astype(dtype)

    elif isinstance(arbitrary, pd.Timestamp):
        # This will always treat NaTs as nulls since it's not technically a
        # discrete value like NaN
        data = as_column(pa.array(pd.Series([arbitrary]), from_pandas=True))
        if dtype is not None:
            data = data.astype(dtype)

    elif np.isscalar(arbitrary) and not isinstance(arbitrary, memoryview):
        length = length or 1
        if (
            (nan_as_null is True)
            and isinstance(arbitrary, (np.floating, float))
            and np.isnan(arbitrary)
        ):
            arbitrary = None
            if dtype is None:
                dtype = np.dtype("float64")

        data = as_column(
            utils.scalar_broadcast_to(arbitrary, length, dtype=dtype)
        )
        if not nan_as_null:
            if np.issubdtype(data.dtype, np.floating):
                data = data.fillna(np.nan)
            elif np.issubdtype(data.dtype, np.datetime64):
                data = data.fillna(np.datetime64("NaT"))

    elif hasattr(arbitrary, "__array_interface__"):
        # CUDF assumes values are always contiguous
        desc = arbitrary.__array_interface__
        shape = desc["shape"]
        arb_dtype = np.dtype(desc["typestr"])
        # CUDF assumes values are always contiguous
        if len(shape) > 1:
            raise ValueError("Data must be 1-dimensional")

        arbitrary = np.asarray(arbitrary)
        if not arbitrary.flags["C_CONTIGUOUS"]:
            arbitrary = np.ascontiguousarray(arbitrary)

        if dtype is not None:
            arbitrary = arbitrary.astype(dtype)

        if arb_dtype.kind == "M":

            time_unit, _ = np.datetime_data(arbitrary.dtype)
            cast_dtype = time_unit in ("D", "W", "M", "Y")

            if cast_dtype:
                arbitrary = arbitrary.astype(np.dtype("datetime64[s]"))

            buffer = Buffer(arbitrary)
            mask = None
            if nan_as_null:
                data = as_column(
                    buffer, dtype=arbitrary.dtype, nan_as_null=nan_as_null
                )
                data = utils.time_col_replace_nulls(data)
                mask = data.mask

            data = datetime.DatetimeColumn(
                data=buffer, mask=mask, dtype=arbitrary.dtype
            )
        elif arb_dtype.kind in ("O", "U"):
            data = as_column(
                pa.Array.from_pandas(arbitrary), dtype=arbitrary.dtype
            )
            # There is no cast operation available for pa.Array from int to
            # str, Hence instead of handling in pa.Array block, we
            # will have to type-cast here.
            if dtype is not None:
                data = data.astype(dtype)
        else:
            data = as_column(cupy.asarray(arbitrary), nan_as_null=nan_as_null)

    elif isinstance(arbitrary, memoryview):
        data = as_column(
            np.asarray(arbitrary), dtype=dtype, nan_as_null=nan_as_null
        )

    else:
        try:
            data = as_column(
                memoryview(arbitrary), dtype=dtype, nan_as_null=nan_as_null
            )
        except TypeError:
            pa_type = None
            np_type = None
            try:
                if dtype is not None:
                    dtype = pd.api.types.pandas_dtype(dtype)
                    if is_categorical_dtype(dtype):
                        raise TypeError
                    else:
                        np_type = np.dtype(dtype).type
                        if np_type == np.bool_:
                            pa_type = pa.bool_()
                        else:
                            pa_type = np_to_pa_dtype(np.dtype(dtype))
                data = as_column(
                    pa.array(
                        arbitrary,
                        type=pa_type,
                        from_pandas=True
                        if nan_as_null is None
                        else nan_as_null,
                    ),
                    dtype=dtype,
                    nan_as_null=nan_as_null,
                )
            except (pa.ArrowInvalid, pa.ArrowTypeError, TypeError):
                if is_categorical_dtype(dtype):
                    sr = pd.Series(arbitrary, dtype="category")
                    data = as_column(sr, nan_as_null=nan_as_null)
                elif np_type == np.str_:
                    sr = pd.Series(arbitrary, dtype="str")
                    data = as_column(sr, nan_as_null=nan_as_null)
                else:
                    data = as_column(
                        np.asarray(
                            arbitrary,
                            dtype=dtype if dtype is None else np.dtype(dtype),
                        ),
                        dtype=dtype,
                        nan_as_null=nan_as_null,
                    )
    return data


def column_applymap(udf, column, out_dtype):
    """Apply a elemenwise function to transform the values in the Column.

    Parameters
    ----------
    udf : function
        Wrapped by numba jit for call on the GPU as a device function.
    column : Column
        The source column.
    out_dtype  : numpy.dtype
        The dtype for use in the output.

    Returns
    -------
    result : Column
    """
    core = njit(udf)
    results = column_empty(len(column), dtype=out_dtype)
    values = column.data_array_view
    if column.nullable:
        # For masked columns
        @cuda.jit
        def kernel_masked(values, masks, results):
            i = cuda.grid(1)
            # in range?
            if i < values.size:
                # valid?
                if utils.mask_get(masks, i):
                    # call udf
                    results[i] = core(values[i])

        masks = column.mask_array_view
        kernel_masked.forall(len(column))(values, masks, results)
    else:
        # For non-masked columns
        @cuda.jit
        def kernel_non_masked(values, results):
            i = cuda.grid(1)
            # in range?
            if i < values.size:
                # call udf
                results[i] = core(values[i])

        kernel_non_masked.forall(len(column))(values, results)

    return as_column(results)


def _data_from_cuda_array_interface_desc(obj):
    desc = obj.__cuda_array_interface__
    ptr = desc["data"][0]
    nelem = desc["shape"][0] if len(desc["shape"]) > 0 else 1
    dtype = np.dtype(desc["typestr"])

    data = Buffer(data=ptr, size=nelem * dtype.itemsize, owner=obj)
    return data


def _mask_from_cuda_array_interface_desc(obj):
    desc = obj.__cuda_array_interface__
    mask = desc.get("mask", None)

    if mask is not None:
        desc = mask.__cuda_array_interface__
        ptr = desc["data"][0]
        nelem = desc["shape"][0]
        typestr = desc["typestr"]
        typecode = typestr[1]
        if typecode == "t":
            mask_size = bitmask_allocation_size_bytes(nelem)
            mask = Buffer(data=ptr, size=mask_size, owner=obj)
        elif typecode == "b":
            col = as_column(mask)
            mask = bools_to_mask(col)
        else:
            raise NotImplementedError(
                f"Cannot infer mask from typestr {typestr}"
            )
    return mask


def serialize_columns(columns):
    """
    Return the headers and frames resulting
    from serializing a list of Column
    Parameters
    ----------
    columns : list
        list of Columns to serialize
    Returns
    -------
    headers : list
        list of header metadata for each Column
    frames : list
        list of frames
    """
    headers = []
    frames = []

    if len(columns) > 0:
        header_columns = [c.serialize() for c in columns]
        headers, column_frames = zip(*header_columns)
        for f in column_frames:
            frames.extend(f)

    return headers, frames


def deserialize_columns(headers, frames):
    """
    Construct a list of Columns from a list of headers
    and frames.
    """
    columns = []

    for meta in headers:
        col_frame_count = meta["frame_count"]
        col_typ = pickle.loads(meta["type-serialized"])
        colobj = col_typ.deserialize(meta, frames[:col_frame_count])
        columns.append(colobj)
        # Advance frames
        frames = frames[col_frame_count:]

    return columns
