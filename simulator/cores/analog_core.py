#
# Copyright 2017-2023 Sandia Corporation. Under the terms of Contract DE-AC04-94AL85000 with
# Sandia Corporation, the U.S. Government retains certain rights in this software.
#
# See LICENSE for full license details
#

"""A Numpy-like interface for interacting with analog MVM operations.

AnalogCore is the primary interface for CrossSim analog MVM simulations. AnalogCores
behave as if they are Numpy arrays for 1 and 2 dimensional matrix multiplication (left
and right), transpostion and slice operations. This is intended to provide drop-in
compatibility with existing numpy matrix manipulation code. AnalogCore only implements
functions which make physical sense for analog MVM array, for instance element-wise
operations are intentionally not supported as they don't have an obvious physical
implementation.

Internally AnalogCore may partition a single matrix across multiple physical arrays
based on the input data types and specified parameters.
"""

import numpy as np
from warnings import warn
from simulator.parameters import CrossSimParameters

from . import BalancedCore, OffsetCore, BitslicedCore, NumericCore

from simulator.backend import ComputeBackend
from simulator.parameters.core_parameters import PartitionStrategy, CoreStyle

xp = ComputeBackend()

import numpy.typing as npt


class AnalogCore:
    """Primary simulation object for Analog MVM.

    AnalogCore provides a numpy-like interface for matrix multiplication operations
    using analog MVM arrays. AnalogCore should be the primary interface for algorithms.
    AnalogCore internally contains multiple physical cores (which may corrospond to
    multiple discrete arrays in the case of balanced and bit-sliced systems). AnalogCore
    may also expand the provided matrix as needed for analog computation, for instance
    when using complex numbers.

    Attributes:
        matrix:
            Numpy-like array to be represented by this core. This sets the size of the
            array (which may be larger than a single physical array).
        params:
            A CrossSimParameters object or list of CrossSimParameters objects
            specifying the properties of the constructed AnalogCores. For simulations
            where a single matrix will be split across multiple physical cores this
            must be a list of length equal to the number of underlying cores.
        empty_matrix:
            Bool indicating whether to initialize the array from the input matrix.
            For creating arrays where the data isn't known yet.
    """

    # Forces numpy to use rmatmat for right multiplies
    # Alternative is subclassing ndarray which has some annoying potential side effects
    # cupy uses __array_priority__ = 100 so need to be 1 more than that
    __array_priority__ = 101

    def __init__(
        self,
        matrix: npt.ArrayLike,
        params: CrossSimParameters | list[CrossSimParameters],
        empty_matrix=False,
    ) -> None:
        """Initializes an AnalogCore object with the provided dimension and parameters.

        Args:
            matrix: Matrix to initialize the array size and (optionally) data
            params: Parameters object or objects specifying the behavior of the object.
            empty_array: Bool to skip initializing array data from input matrix
        Raises:
            ValueError: Parameter object is invalid for the configuration.
        """
        # Set master params object
        if type(params) is not list:
            self.params = params.copy()
        else:
            self.params = params[0]

        # Initialize the compute backend
        gpu_id = (
            self.params.simulation.gpu_id if self.params.simulation.useGPU else None
        )
        xp.__init__(self.params.simulation.useGPU, gpu_id)

        # Floating point epsilons come up occassionally so just store it here
        self._eps = xp.finfo(float).eps

        # This protects from the case where AnalogCore is a 1D vector which breaks
        # complex equivalent expansion. This could probably be fixed but it is a
        # sufficiently unusual case that just throw an error for now.
        if matrix.ndim == 1 or any(i == 1 for i in matrix.shape):
            raise ValueError("AnalogCore must 2 dimensional")

        # params used in AnalogCore
        self.complex_valued = (
            self.params.core.complex_matrix or self.params.core.complex_input
        )
        self.fast_matmul = self.params.simulation.fast_matmul
        self.shape = matrix.shape
        self.weight_clipping = self.params.core.mapping.weights.clipping

        self.mvm_input_percentile_scaling = (
            self.params.core.mapping.inputs.mvm.percentile is not None
        )
        self.vmm_input_percentile_scaling = (
            self.params.core.mapping.inputs.vmm.percentile is not None
        )

        # AnalogCore has slice objects to simplfy core operation stacking
        self.rslice = None
        self.cslice = None

        # AnalogCore has slice objects to simplfy core operation stacking
        self.rslice = None
        self.cslice = None

        # TODO: temporary setting nrow and ncol for compatibility
        # Change when fixing row/col confusion
        nrow, ncol = matrix.shape

        # Double # rows and columns for complex matrix
        if self.complex_valued:
            nrow *= 2
            ncol *= 2

        # Determine number of cores
        NrowsMax = self.params.core.rows_max
        NcolsMax = self.params.core.cols_max
        if NrowsMax > 0:
            self.Ncores = (ncol - 1) // NrowsMax + 1
        else:
            self.Ncores = 1

        if NcolsMax > 0:
            self.Ncores *= (nrow - 1) // NcolsMax + 1
        else:
            self.Ncores *= 1

        # Check that Ncores is compatible with the number of params objects
        params_ = params
        # Just duplicate the params object if the user only passed in a single params.
        # If the list lengths don't match however, e.g. user intentionally passed in a
        # list but it is the wrong size, that might indicate a config problem so error.
        if self.Ncores > 1 and type(params_) is not list:
            params_ = [params] * self.Ncores
        if self.Ncores == 1 and type(params_) is list:
            raise ValueError("Too many params objects provided for single-core layer")
        if self.Ncores > 1 and type(params_) is list and len(params_) != self.Ncores:
            raise ValueError(
                "Number of params objects provided does not match number of cores",
            )

        self.col_partition_priority = (
            self.params.core.mapping.weights.col_partition_priority
        )
        self.row_partition_priority = (
            self.params.core.mapping.weights.row_partition_priority
        )
        self.col_partition_strategy = (
            self.params.core.mapping.weights.col_partition_strategy
        )
        self.row_partition_strategy = (
            self.params.core.mapping.weights.row_partition_strategy
        )

        # Create single cores
        if self.Ncores == 1:
            self.cores = [[None]]
            self.cores[0][0] = self._make_core(params=self.params)
            self.num_cores_row = 1
            self.num_cores_col = 1
            self.NrowsVec = [nrow]
            self.NcolsVec = [ncol]

        else:
            self.num_cores_row = self.Ncores // ((ncol - 1) // NrowsMax + 1)
            self.num_cores_col = self.Ncores // self.num_cores_row
            self.cores = [None] * self.num_cores_row
            for r in range(self.num_cores_row):
                self.cores[r] = [
                    self._make_core(params=params_[r * c])
                    for c in range(self.num_cores_col)
                ]

            # Partition the matrix across the sub cores with the following priority:
            # If rows/cols can be partition evenly partition evenly
            # If row/col_partition priority is defined check if nrow/ncol is a multiple of N*max/partition so all but one core has Nmax*
            #   This is useful (with col_partition_priority = [2, 4]) so that convolution blocks in depthwise convolutions are not partition.
            #   Not totally robust
            # Otherwise partition based on row/col_partition_strategy (max or even)

            if nrow % self.num_cores_row == 0:
                self.NrowsVec = (nrow // self.num_cores_row) * np.ones(
                    self.num_cores_row,
                    dtype=np.int32,
                )
            else:
                # prio_partition = True in (((nrow % (self.NrowsMax / div)) == 0) for div in self.row_partition_priority)
                prio_partition = True in (
                    ((nrow % (self.NcolsMax / div)) == 0)
                    for div in self.row_partition_priority
                )

                if (
                    prio_partition
                    or self.row_partition_strategy == PartitionStrategy.MAX
                ):
                    # rows_per_core = NrowsMax
                    rows_per_core = NcolsMax
                else:
                    rows_per_core = np.round(nrow / self.num_cores_row).astype(np.int32)
                self.NrowsVec = rows_per_core * np.ones(
                    self.num_cores_row,
                    dtype=np.int32,
                )
                self.NrowsVec[-1] = nrow - (self.num_cores_row - 1) * rows_per_core

            if ncol % self.num_cores_col == 0:
                self.NcolsVec = (ncol // self.num_cores_col) * np.ones(
                    self.num_cores_col,
                    dtype=np.int32,
                )
            else:
                # prio_partition = True in (((ncol % (self.NcolsMax / div)) == 0) for div in self.col_partition_priority)
                prio_partition = True in (
                    ((ncol % (self.NrowsMax / div)) == 0)
                    for div in self.col_partition_priority
                )

                if (
                    prio_partition
                    or self.col_partition_strategy == PartitionStrategy.MAX
                ):
                    # cols_per_core = NcolsMax
                    cols_per_core = NrowsMax
                else:
                    cols_per_core = np.round(ncol / self.num_cores_col).astype(np.int32)

                self.NcolsVec = cols_per_core * np.ones(
                    self.num_cores_col,
                    dtype=np.int32,
                )
                self.NcolsVec[-1] = ncol - (self.num_cores_col - 1) * cols_per_core

            # Precompute a list of row/col partition information (id, start, end)
            # This is used to aggreagate partitions in every other function
            self.row_partition_bounds = [
                (r, np.sum(self.NrowsVec[:r]), np.sum(self.NrowsVec[: r + 1]))
                for r in range(self.num_cores_row)
            ]
            self.col_partition_bounds = [
                (c, np.sum(self.NcolsVec[:c]), np.sum(self.NcolsVec[: c + 1]))
                for c in range(self.num_cores_col)
            ]

        self.nrow = nrow
        self.ncol = ncol
        self.dtype = None
        self.ndim = 2

        if not empty_matrix:
            self.set_matrix(matrix)

    def check_dimensions(self, x, reverse=False):
        # This function does not account for SW packing, which is not intended to be used for non-convolution
        # workloads. If MVM is called from convolution, this function is bypassed.

        M, N = self.shape

        """
            'reverse' indicates instances where left multiplying
            self by the multiplier occurs

            x - multiplier
        """

        if reverse:
            len_two = len(x.shape) == 2
            if (len_two and x.shape[1] != M) or (not len_two and x.shape[0] != M):
                raise ValueError(
                    f"Dimension Mismatch, Multiplier: {x.shape}, Matrix: {self.shape}",
                )
        else:
            if x.shape[0] != N:
                raise ValueError(
                    f"Dimension Mismatch, Matrix: {self.shape}, Multiplier: {x.shape}",
                )

    def set_matrix(
        self,
        matrix: npt.ArrayLike,
        verbose: bool = False,
        error_mask=None,
    ) -> None:
        """Programs a matrix into the AnalogCore.

        Transform the input matrix as needed for programming to analog arrays including
        complex expansion, clipping, and matrix partitioning. Calls the set_matrix()
        methoods of the underlying core objects.

        Args:
            matrix: Numpy ndarray to be programmed into the array.
            verbose: Boolean flag to enable verbose print statements.
            error_mask: Tuple of slices for setting parts of the matrix

        Raises:
            ValueError: Matrix is not valid for the input parameters.
        """
        matrix = np.asarray(matrix)
        if self.shape != matrix.shape:
            raise ValueError("Matrix shape must match AnalogCore shape")

        if verbose:
            print("Min/Max matrix values", np.min(matrix), np.max(matrix))

        if (
            matrix.dtype == np.complex64 or matrix.dtype == np.complex128
        ) and not self.complex_valued:
            raise ValueError(
                "If setting complex-valued matrices, please set core.complex_matrix = True",
            )

        self.dtype = matrix.dtype

        # Break up complex matrix into real and imaginary quadrants
        if self.complex_valued:
            Nx, Ny = matrix.shape
            matrix_real = np.real(matrix)
            matrix_imag = np.imag(matrix)
            mcopy = np.zeros((2 * Nx, 2 * Ny), dtype=matrix_real.dtype)
            mcopy[0:Nx, 0:Ny] = matrix_real
            mcopy[Nx:, 0:Ny] = matrix_imag
            mcopy[0:Nx, Ny:] = -matrix_imag
            mcopy[Nx:, Ny:] = matrix_real
        else:
            mcopy = matrix.copy()

        # For partial matrix updates new values must be inside the previous range.
        # If the values would exceed this range then you would have to reprogram all
        # matrix values based on the new range, so instead we will clip and warn
        if error_mask:
            mat_max = np.max(matrix)
            mat_min = np.min(matrix)

            # Adding an epsilon here to avoid erroreous errors
            if mat_max > (self.max + self._eps) or mat_min < (self.min - self._eps):
                print(mat_max, self.params.core.mapping.weights.max)
                print(mat_min, self.params.core.mapping.weights.min)
                warn(
                    "Partial matrix update contains values outside of weight range. These values will be clipped. To remove this wanring, set the weight range to contain the full range of expected parital matrix updates.",
                    category=RuntimeWarning,
                )

        # Clip the matrix values
        # This is done at this level so that matrix partitions are not separately clipped using
        # different limits
        # Need to update the params on the individual cores
        # Only set percentile limits if we're writng the full matrix
        weight_limits = None
        if not error_mask:
            if self.params.core.mapping.weights.percentile:
                self.min, self.max = self._set_limits_percentile(
                    self.params.core.mapping.weights,
                    mcopy,
                    reset=True,
                )
            else:
                self.min = self.params.core.mapping.weights.min
                self.max = self.params.core.mapping.weights.max
            weight_limits = (self.min, self.max)

        if self.weight_clipping:
            mcopy = mcopy.clip(self.min, self.max)

        for i in range(self.num_cores_row):
            for j in range(self.num_cores_col):
                self.cores[i][
                    j
                ].params.core.mapping.weights.min = self.params.core.mapping.weights.min
                self.cores[i][
                    j
                ].params.core.mapping.weights.max = self.params.core.mapping.weights.max

        if self.Ncores == 1:
            self.cores[0][0].set_matrix(
                mcopy,
                weight_limits=weight_limits,
                error_mask=error_mask,
            )
        else:
            for row, row_start, row_end in self.row_partition_bounds:
                for col, col_start, col_end in self.col_partition_bounds:
                    error_mask_ = error_mask
                    if error_mask:
                        if error_mask[0].step and error_mask[0].step < 0:
                            row_mask = slice(
                                error_mask[0].start - row_start,
                                None,
                                error_mask[0].step,
                            )
                        else:
                            row_mask = slice(
                                error_mask[0].start - row_start,
                                error_mask[0].stop - row_start,
                                error_mask[0].step,
                            )

                        if error_mask[1].step and error_mask[1].step < 0:
                            col_mask = slice(
                                error_mask[1].start - col_start,
                                None,
                                error_mask[1].step,
                            )
                        else:
                            col_mask = slice(
                                error_mask[1].start - col_start,
                                error_mask[1].stop - col_start,
                                error_mask[1].step,
                            )

                        error_mask_ = (
                            slice(*row_mask.indices(row_end - row_start)),
                            slice(*col_mask.indices(col_end - col_start)),
                        )
                    mat_prog = mcopy[row_start:row_end, col_start:col_end]
                    self.cores[row][col].set_matrix(
                        mat_prog,
                        weight_limits=weight_limits,
                        error_mask=error_mask_,
                    )

    def get_matrix(self) -> npt.ArrayLike:
        """Returns the programmed matrix with weight errors and clipping applied.

        The programmed matrix is converted into original input matrix format, e.g.,
        a single matrix with real or complex inputs, but with analog-specific
        non-idealities applied. Currently this is clipping and programming-time weight
        errors (programming and drift).

        Returns:
            A Numpy array of the programmed matrix.

        """
        if self.Ncores == 1:
            matrix = self.cores[0][0]._read_matrix()
        else:
            matrix = np.zeros((self.nrow, self.ncol))
            for row, row_start, row_end in self.row_partition_bounds:
                for col, col_start, col_end in self.col_partition_bounds:
                    matrix[row_start:row_end, col_start:col_end] = self.cores[row][
                        col
                    ]._read_matrix()

        if not self.complex_valued:
            return matrix
        else:
            Nx, Ny = matrix.shape[0] // 2, matrix.shape[1] // 2
            m_real = matrix[0:Nx, 0:Ny]
            m_imag = matrix[Nx:, 0:Ny]
            return m_real + 1j * m_imag

    def matvec(
        self,
        vec: npt.ArrayLike,
        bypass_dimcheck: bool = False,
    ) -> npt.ArrayLike:
        """Perform matrix-vector (Ax = b) multiply on programmed vector (1D).

        Primary simulation function for 1D inputs. Transforms the vector for analog
        simulation and calls the underlying core simulation functions for each
        sub-core. Without errors this should be identical to ``A.matmul(vec)`` or
        ``A @ vec`` where A is the numpy array programmed with set_matrix().

        Args:
            vec: 1D Numpy-like array to be multiplied.
            bypass: If True, bypasses call to check_dimensions()

        Returns:
            1D Numpy-like array result of matrix-vector multiplication.
        """
        # If complex, concatenate real and imaginary part of input
        if not bypass_dimcheck:
            self.check_dimensions(vec)

        if self.complex_valued:
            vec_real = xp.real(vec)
            vec_imag = xp.imag(vec)
            vcopy = xp.concatenate((vec_real, vec_imag))
        else:
            vcopy = vec.copy()

        input_range = None
        if self.mvm_input_percentile_scaling:
            input_range = self._set_limits_percentile(
                self.params.core.mapping.inputs.mvm,
                vcopy,
                reset=True,
            )

        if self.Ncores == 1:
            output = self.cores[0][0].run_xbar_mvm(vcopy, input_range)

        else:
            output = xp.zeros(self.nrow)
            for row, row_start, row_end in self.row_partition_bounds:
                for col, col_start, col_end in self.col_partition_bounds:
                    vec_in = vcopy[col_start:col_end]
                    output[row_start:row_end] += self.cores[row][col].run_xbar_mvm(
                        vec_in,
                        input_range,
                    )

        # If complex, compose real and imaginary
        if self.complex_valued:
            N = int(len(output) / 2)
            output_real = output[:N]
            output_imag = output[N:]
            output = output_real + 1j * output_imag

        return output

    def matmat(self, mat: npt.ArrayLike) -> npt.ArrayLike:
        """Perform right matrix-matrix (AX = B) multiply on programmed matrix (2D).

        Primary simulation function for 2D inputs. Transforms the matrix for analog
        simulation and calls the underlying core simulation functions for each
        sub-core.  Without errors this should be identical to ``A.matmul(mat)`` or
        ``A @ mat`` where A is the numpy array programmed with set_matrix().

        Args:
            vec: 2D Numpy-like array to be multiplied.

        Returns:
            2D Numpy-like array result of matrix-matrix multiplication.
        """
        if self.complex_valued:
            mat_real = xp.real(mat)
            mat_imag = xp.imag(mat)
            mcopy = xp.vstack((mat_real, mat_imag))
        else:
            mcopy = mat.copy()

        input_range = None
        if self.mvm_input_percentile_scaling:
            input_range = self._set_limits_percentile(
                self.params.core.mapping.inputs.mvm,
                mcopy,
                reset=True,
            )

        if self.Ncores == 1:
            output = self.cores[0][0].run_xbar_mvm(mcopy, input_range)

        else:
            output = xp.zeros((self.nrow, mat.shape[1]))
            for row, row_start, row_end in self.row_partition_bounds:
                for col, col_start, col_end in self.col_partition_bounds:
                    mat_in = mcopy[col_start:col_end]
                    output[row_start:row_end] += self.cores[row][col].run_xbar_mvm(
                        mat_in,
                        input_range,
                    )

        if self.complex_valued:
            output_real = output[: int(self.nrow // 2)]
            output_imag = output[int(self.nrow // 2) :]
            output = output_real + 1j * output_imag

        return output

    def vecmat(self, vec: npt.ArrayLike) -> npt.ArrayLike:
        """Perform vector-matrix (xA = b) multiply on programmed vector (1D).

        Primary simulation function for 1D inputs. Transforms the vector for analog
        simulation and calls the underlying core simulation functions for each
        sub-core. Without errors this should be identical to ``vec.matmul(A)`` or
        ``vec @ A`` where A is the numpy array programmed with set_matrix().

        Args:
            vec: 1D Numpy-like array to be multiplied.

        Returns:
            1D Numpy-like array result of vector-matrix multiplication.
        """
        if self.complex_valued:
            vec_real = xp.real(vec)
            vec_imag = xp.imag(vec)
            vcopy = xp.concatenate((vec_imag, vec_real))
        else:
            vcopy = vec.copy()

        input_range = None
        if self.vmm_input_percentile_scaling:
            input_range = self._set_limits_percentile(
                self.params.core.mapping.inputs.vmm,
                vcopy,
                reset=True,
            )

        if self.Ncores == 1:
            output = self.cores[0][0].run_xbar_vmm(vcopy, input_range)

        else:
            output = xp.zeros(self.ncol)
            for row, row_start, row_end in self.row_partition_bounds:
                for col, col_start, col_end in self.col_partition_bounds:
                    vec_in = vcopy[row_start:row_end]
                    output[col_start:col_end] += self.cores[row][col].run_xbar_vmm(
                        vec_in,
                        input_range,
                    )

        if self.complex_valued:
            N = int(len(output) / 2)
            output_real = output[N:]
            output_imag = output[:N]
            output = output_real + 1j * output_imag

        return output

    def rmatmat(self, mat: npt.ArrayLike) -> npt.ArrayLike:
        """Perform left matrix-matrix (XA = B) multiply on programmed matrix (2D).

        Primary simulation function for 2D inputs. Transforms the matrix for analog
        simulation and calls the underlying core simulation functions for each
        sub-core.  Without errors this should be identical to ``mat.matmul(A)`` or
        ``mat @ A`` where A is the numpy array programmed with set_matrix().

        Args:
            vec: 2D Numpy-like array to be multiplied.

        Returns:
            2D Numpy-like array result of matrix-matrix multiplication.
        """
        if self.complex_valued:
            mat_real = xp.real(mat)
            mat_imag = xp.imag(mat)
            mcopy = xp.hstack((mat_real, -mat_imag))
        else:
            mcopy = mat.copy()

        input_range = None
        if self.vmm_input_percentile_scaling:
            input_range = self._set_limits_percentile(
                self.params.core.mapping.inputs.vmm,
                mcopy,
                reset=True,
            )

        if self.Ncores == 1:
            output = self.cores[0][0].run_xbar_vmm(mcopy, input_range)

        else:
            output = xp.zeros((mat.shape[0], self.ncol))
            for row, row_start, row_end in self.row_partition_bounds:
                for col, col_start, col_end in self.col_partition_bounds:
                    mat_in = mcopy[:, row_start:row_end]
                    output[:, col_start:col_end] += self.cores[row][col].run_xbar_vmm(
                        mat_in,
                        input_range,
                    )

        if self.complex_valued:
            output_real = output[:, : int(self.ncol // 2)]
            output_imag = output[:, int(self.ncol // 2) :]
            output = output_real - 1j * output_imag

        return output

    def dot(self, x: npt.ArrayLike) -> npt.ArrayLike:
        """Numpy-like ndarray.dot() function for 1D and 2D inputs.

        Performs a 1D or 2D matrix dot product with the programmed matrix. For 2D
        inputs this will decompose the matrix into a series for 1D inputs or use a
        (generally faster) matrix-matrix approximation if possible given the simulation
        parameters. In the error free case this should be identical to A.dot(x) where A
        is the numpy array programmed with set_matrix().

        Args:
            x: A 1D or 2D numpy-like array to be multiplied.

        Returns:
            A 1D or 2D numpy-like array result.
        """
        x = xp.asarray(x)

        if x.ndim == 1 or (x.ndim == 2 and x.shape[1] == 1):
            return self.matvec(x)
        elif x.ndim == 2:
            # Stacking fails for shape (X, 0), revert to matmat which handles this
            # Empty matix is weird so ignoring user preference here
            if not (self.fast_matmul or x.shape[1] == 0):
                return xp.hstack(
                    [
                        self.matvec(
                            col.reshape(
                                -1,
                            ),
                        ).reshape(-1, 1)
                        for col in x.T
                    ],
                )
            else:
                return self.matmat(x)
        else:
            raise ValueError("Input must be 1D or 2D")

    def rdot(self, x: npt.ArrayLike) -> npt.ArrayLike:
        """Numpy-like ndarray.dot() function for 1D and 2D inputs.

        Performs a 1D or 2D matrix dot product with the programmed matrix. For 2D
        inputs this will decompose the matrix into a series for 1D inputs or use a
        (generally faster) matrix-matrix approximation if possible given the simulation
        parameters. In the error free case this should be identical to x.dot(A) where A
        is the numpy array programmed with set_matrix().

        Args:
            x: A 1D or 2D numpy-like array to be multiplied.

        Returns:
            A 1D or 2D numpy-like array result.
        """
        x = xp.asarray(x)
        # Need to check shape[0] because (X, 1) @ (1, X) is a 2D output meaning matmat
        if x.ndim == 1 or (x.ndim == 2 and x.shape[0] == 1):
            return self.vecmat(x)
        elif x.ndim == 2:
            # Stacking fails for shape (0, X), revert to matmat which handles this
            # Empty matix is weird so ignoring user preference here
            if not (self.fast_matmul or x.shape[0] == 0):
                return xp.vstack([self.vecmat(row) for row in x])
            else:
                return self.rmatmat(x)
        else:
            raise ValueError("Input must be 1D or 2D")

    def mat_multivec(self, vec):
        """Perform matrix-vector multiply on multiple analog vectors packed into the
        "vec" object. A single MVM op in the simulation models multiple MVMs in the
        physical hardware.

        The "vec" object will be reshaped into the following 2D shape: (Ncopy, N)
        where Ncopy is the number of input vectors packed into the MVM simulation
        and N is the length of a single input vector

        Args:
            vec: ...

        Raises:
            NotImplementedError: ...
            ValueError: ...

        Returns:
            NDArray: ...
        """
        if self.complex_valued:
            raise NotImplementedError(
                "MVM packing not supported for complex-valued MVMs",
            )

        if self.Ncores == 1:
            return self.matvec(vec.flatten(), bypass_dimcheck=True)

        else:
            Ncopy = (
                self.params.simulation.convolution.x_par
                * self.params.simulation.convolution.y_par
            )
            if vec.size != Ncopy * self.ncol:
                raise ValueError("Packed vector size incompatible with core parameters")
            if vec.shape != (Ncopy, self.ncol):
                vec = vec.reshape((Ncopy, self.ncol))

            output = xp.zeros((Ncopy, self.nrow))
            for i in range(self.num_cores_col):
                output_i = xp.zeros((Ncopy, self.nrow))
                i_start = np.sum(self.NcolsVec[:i])
                i_end = np.sum(self.NcolsVec[: i + 1])
                vec_i = vec[:, i_start:i_end].flatten()
                for j in range(self.num_cores_row):
                    j_start = np.sum(self.NrowsVec[:j])
                    j_end = np.sum(self.NrowsVec[: j + 1])
                    output_ij = self.cores[j][i].run_xbar_mvm(vec_i.copy())
                    output_i[:, j_start:j_end] = output_ij.reshape(
                        (Ncopy, j_start - j_end),
                    )
                output += output_i
            return output.flatten()

    def transpose(self):
        return TransposedCore(parent=self)

    T = property(transpose)

    def __getitem__(self, item):
        rslice, cslice, full_mask, flatten = self._create_mask(item)
        if full_mask:
            return self
        return MaskedCore(self, rslice, cslice, flatten)

    def __setitem__(self, key, value):
        rslice, cslice, full_mask, _ = self._create_mask(key)
        expanded_mat = self.get_matrix()
        expanded_mat[rslice, cslice] = np.asarray(value)
        error_mask = None if full_mask else (rslice, cslice)
        self.set_matrix(expanded_mat, error_mask=error_mask)

    def _create_mask(self, item) -> tuple[slice, slice, bool, bool]:
        """Converts an input item int, slice, tuple of int/slices into a tuple of slices."""
        if not isinstance(item, tuple):
            # Single value passed, convert to tuple then pad with empty slice
            item = (item, slice(None, None, None))
        if not all(isinstance(i, (int, slice)) for i in item):
            raise TypeError("Index must be int, slice or tuple of those types")
        if len(item) > 2:
            # Case of length one is accounted for above
            raise ValueError("Index must be of length 1 or 2")

        # Numpy flattens arrays if any of the slices are integers
        flatten = any(isinstance(i, int) for i in item)

        # Tracks if the mask covers the full matrix, if so we can just ignore the mask
        full_mask = False

        # For an example with a negative step size and None initialized as start and
        # stop for slice, indices incorrectly makes the
        # stop index -1 when trying to encapsulate the whole range of what is being sliced.

        # For example, for a slice x = slice(None, None, -1), slice(*x.indices(5))
        # results in slice(4,-1,-1), which is incorrect, because the slice always
        # results in an empty value. It must be noted that stop is not inclusive,
        # and indices is attempting to include the 0th index by making the stop index
        # one lower. This is flawed due to a -1 index being also the len() - 1 index
        # of some structure. Changing to None ensures the encapsulation of the whole
        # structure.

        # NOTE - we may need to add to each conditional an and 'self.*_slice.stop < 0'
        # in the case where y in .indices(y) is less than the total size of the structure.

        rslice, cslice = item
        if isinstance(rslice, int):
            if rslice < 0:
                rslice = slice(self.shape[0] + rslice, self.shape[0] + rslice + 1)
            else:
                rslice = slice(rslice, rslice + 1)
        else:
            rslice = slice(*rslice.indices(self.shape[0]))
            if rslice.step < 0:
                rslice = slice(rslice.start, None, rslice.step)

            full_mask = (
                len(range(*rslice.indices(self.shape[0]))) == self.shape[0]
                and rslice.step > 0
            )

        if self.ndim == 1:
            cslice = None
        else:
            if isinstance(cslice, int):
                if cslice < 0:
                    cslice = slice(self.shape[1] + cslice, self.shape[1] + cslice + 1)
                else:
                    cslice = slice(cslice, cslice + 1)
            else:
                cslice = slice(*cslice.indices(self.shape[1]))
                if cslice.step < 0:
                    cslice = slice(cslice.start, None, cslice.step)

            full_mask &= (
                len(range(*cslice.indices(self.shape[1]))) == self.shape[1]
                and cslice.step > 0
            )

        return (rslice, cslice, full_mask, flatten)

    @staticmethod
    def _set_limits_percentile(constraints, input_, reset=False):
        """Set the min and max of the params object based on input data using
        the percentile option, if the min and max have not been set yet
        If min and max are already set, this function does nothing if reset=False
        constraints must have the following params:
            min: float
            max: float
            percentile: float.
        """
        if (constraints.min is None or constraints.max is None) or reset:
            if constraints.percentile >= 1.0:
                X_max = np.max(np.abs(input_))
                X_max *= constraints.percentile
                min_ = -X_max
                max_ = X_max

            elif constraints.percentile < 1.0:
                X_posmax = np.percentile(input_, 100 * constraints.percentile)
                X_negmax = np.percentile(input_, 100 - 100 * constraints.percentile)
                X_max = np.max(np.abs(np.array([X_posmax, X_negmax])))
                min_ = -X_max
                max_ = X_max

        # Ensure min_ and max_ aren't the same for uniform inputs
        if min_ == max_:
            eps = np.finfo(float).eps
            min_ -= eps
            max_ += eps
        return (min_, max_)

    @staticmethod
    def _make_core(params):
        """Creates the inner and outer cores.  A separate top level function is needed in case a periodic carry is set.

        :param params: All parameters
        :type params: Parameters

        :return: An outer core initialized with the appropriate inner core and parameters
        :rtype: WrapperCore
        :rtype: ICore or WrapperCore
        """
        # run checks for parameter validity (and run manual post sets) (re-run if using periodic carry)
        # verify_parameters(params)

        def inner_factory():
            return NumericCore(params)

        def inner_factory_independent():
            new_params = params.copy()
            # verify_parameters(new_params)
            return NumericCore(new_params)

        # set the outer core type
        if params.core.style == CoreStyle.OFFSET:
            return OffsetCore(inner_factory, params)

        elif params.core.style == CoreStyle.BALANCED:
            return BalancedCore(inner_factory, params)

        elif params.core.style == CoreStyle.BITSLICED:
            return BitslicedCore(inner_factory_independent, params)

        else:
            raise ValueError(
                "Core type "
                + str(params.core.style)
                + " is unknown: should be OFFSET, BALANCED, or BITSLICED",
            )

    def __matmul__(self, other: npt.ArrayLike) -> npt.ArrayLike:
        return self.dot(other)

    def __rmatmul__(self, other: npt.ArrayLike) -> npt.ArrayLike:
        return self.rdot(other)

    def __repr__(self):
        prefix = "AnalogCore("
        mid = np.array2string(self.get_matrix(), separator=", ", prefix=prefix)
        suffix = ")"
        return prefix + mid + suffix

    def __str__(self):
        return self.get_matrix().__str__()

    def __array__(self):
        return self.get_matrix()


class TransposedCore(AnalogCore):
    def __init__(self, parent):
        self.parent = parent

        self.shape = tuple(reversed(self.parent.shape))
        self.ndim = 2

    @property
    def rslice(self):
        return self.parent.rslice

    @property
    def cslice(self):
        return self.parent.cslice

    @property
    def fast_matmul(self):
        return self.parent.fast_matmul

    # Mostly needed because of how some tests are written, potentially could be removed
    @property
    def cores(self):
        return self.parent.cores

    def transpose(self):
        return self.parent

    T = property(transpose)

    def get_matrix(self):
        return self.parent.get_matrix().T

    def set_matrix(self, matrix: npt.ArrayLike, error_mask=None):
        matrix = np.asarray(matrix)
        self.parent.set_matrix(matrix.T, error_mask=error_mask)

    def matvec(self, other: npt.ArrayLike):
        return self.parent.vecmat(other)

    def matmat(self, other: npt.ArrayLike):
        return self.parent.rmatmat(other.T).T

    def vecmat(self, other: npt.ArrayLike):
        return self.parent.matvec(other)

    def rmatmat(self, other: npt.ArrayLike):
        return self.parent.matmat(other.T).T

    def __repr__(self):
        prefix = "TransposedCore("
        mid = np.array2string(self.get_matrix(), separator=", ", prefix=prefix)
        suffix = ")"
        return prefix + mid + suffix


class MaskedCore(AnalogCore):
    def __init__(self, parent, rslice, cslice, flatten):
        self.parent = parent
        self.rslice = rslice
        self.cslice = cslice

        rows = len(range(*rslice.indices(parent.shape[0])))

        cols = 0
        if self.parent.ndim == 2:
            cols = len(range(*cslice.indices(parent.shape[1])))

        self.shape = (rows, cols)
        self.ndim = 2
        if flatten:
            self.shape = (np.max(self.shape),)
            self.ndim = 1

    @property
    def fast_matmul(self):
        return self.parent.fast_matmul

    # Mostly needed because of how some tests are written, potentially could be removed
    @property
    def cores(self):
        return self.parent.cores

    def transpose(self):
        # Numpy defines the transpose of a 1D matrix as itself
        if self.ndim == 1:
            return self
        else:
            return TransposedCore(parent=self)

    T = property(transpose)

    def get_matrix(self):
        if self.ndim == 1 or self.parent.ndim == 1:
            return self.parent.get_matrix()[self.rslice].flatten()
        else:
            return self.parent.get_matrix()[self.rslice, self.cslice]

    def set_matrix(self, matrix: npt.ArrayLike, error_mask=None):
        expanded_mat = self.parent.get_matrix()
        expanded_mat[self.rslice, self.cslice] = np.asarray(matrix)
        self.parent.set_matrix(expanded_mat, error_mask=(self.rslice, self.cslice))

    def matvec(self, other: npt.ArrayLike):
        vec_in = np.zeros(self.parent.shape[1], dtype=other.dtype)
        vec_in[self.cslice] = other.flatten()

        vec_out = self.parent.matvec(vec_in)
        return vec_out[self.rslice]

    def matmat(self, other: npt.ArrayLike):
        # For row slices we're just ignoring the outputs corrosponding to the out-of-slice rows
        # For col slices we're just leaving empty entires in the input matrix corrosponding to missing rows
        mat_in = np.zeros((self.parent.shape[1], other.shape[1]), dtype=other.dtype)
        for i in range(self.parent.shape[1])[self.cslice]:
            mat_in[i] = other[(i - self.cslice.start) // self.cslice.step]
        mat_out = self.parent.matmat(mat_in)
        return mat_out[self.rslice]

    def vecmat(self, other: npt.ArrayLike):
        vec_in = np.zeros(self.parent.shape[0], dtype=other.dtype)
        vec_in[self.rslice] = other.flatten()

        vec_out = self.parent.vecmat(vec_in)
        return vec_out[self.cslice]

    def rmatmat(self, other: npt.ArrayLike):
        mat_in = np.zeros((other.shape[0], self.parent.shape[0]), dtype=other.dtype)
        for i in range(self.parent.shape[0])[self.rslice]:
            mat_in.T[i] = other.T[(i - self.rslice.start) // self.rslice.step]

        mat_out = self.parent.rmatmat(mat_in)
        return mat_out[:, self.cslice]

    def __repr__(self):
        prefix = "MaskedCore("
        mid = np.array2string(self.get_matrix(), separator=", ", prefix=prefix)
        suffix = ")"
        return prefix + mid + suffix
