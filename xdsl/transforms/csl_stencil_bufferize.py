from collections.abc import Sequence
from dataclasses import dataclass

from xdsl.context import MLContext
from xdsl.dialects import arith, bufferization, func, memref, stencil, tensor
from xdsl.dialects.builtin import (
    DenseArrayBase,
    DenseIntOrFPElementsAttr,
    FunctionType,
    ModuleOp,
    TensorType,
    i64,
)
from xdsl.dialects.csl import csl_stencil
from xdsl.ir import Attribute, Block, BlockArgument, Operation, Region, SSAValue
from xdsl.passes import ModulePass
from xdsl.pattern_rewriter import (
    GreedyRewritePatternApplier,
    PatternRewriter,
    PatternRewriteWalker,
    RewritePattern,
    TypeConversionPattern,
    attr_type_rewrite_pattern,
    op_type_rewrite_pattern,
)
from xdsl.rewriter import InsertPoint
from xdsl.utils.hints import isa
from xdsl.utils.isa import isattr


def tensor_to_memref_type(t: TensorType[Attribute]) -> memref.MemRefType[Attribute]:
    """Type conversion from tensor to memref."""
    return memref.MemRefType(t.get_element_type(), t.get_shape())


def to_memref_op(op: SSAValue) -> bufferization.ToMemrefOp:
    """Creates a `bufferization.to_memref` operation."""
    assert isa(op.type, TensorType[Attribute])
    r_type = memref.MemRefType(
        op.type.get_element_type(), op.type.get_shape()
    )  # todo set strided+offset here?
    return bufferization.ToMemrefOp(operands=[op], result_types=[r_type])


def to_tensor_op(
    op: SSAValue, writable: bool = False, restrict: bool = True
) -> bufferization.ToTensorOp:
    """Creates a `bufferization.to_tensor` operation."""
    assert isa(op.type, memref.MemRefType[Attribute])
    return bufferization.ToTensorOp(op, restrict, writable)


class StencilTypeConversion(TypeConversionPattern):
    """
    Converts from tensorised stencil.field types to memref by extracting the element type which is a tensor
    and converting it to memref.

    For instance:
        `!stencil.field<[-1,1023]x[-1,511]xtensor<512xf32>` to `memref<512xf32>`
    """

    @attr_type_rewrite_pattern
    def convert_type(
        self, typ: stencil.FieldType[TensorType[Attribute]]
    ) -> memref.MemRefType[Attribute]:
        # todo should this convert to `memref` or `stencil.field<..xmemref<..>>`?
        return tensor_to_memref_type(typ.get_element_type())


@dataclass(frozen=True)
class ApplyOpBufferize(RewritePattern):
    """
    Bufferizes csl_stencil.apply, rewriting args and block args, changing them from tensor to memref types.
    For each converted arg, creates a `bufferization.to_memref` before the apply op.
    For each converted block arg, creates a `bufferization.to_tensor` at the start of the block.
    """

    @op_type_rewrite_pattern
    def match_and_rewrite(self, op: csl_stencil.ApplyOp, rewriter: PatternRewriter, /):
        if isa(op.iter_arg.type, memref.MemRefType[Attribute]):
            return

        # convert args
        buf_args: list[SSAValue] = []
        to_memrefs: list[Operation] = [buf_iter_arg := to_memref_op(op.iter_arg)]
        for arg in op.args:
            if isa(arg.type, TensorType[Attribute]):
                to_memrefs.append(new_arg := to_memref_op(arg))
                buf_args.append(new_arg.memref)
            else:
                buf_args.append(arg)

        # create new op
        buf_apply_op = csl_stencil.ApplyOp(
            operands=[op.communicated_stencil, buf_iter_arg.memref, op.args, op.dest],
            result_types=op.res.types or [[]],
            regions=[
                self._get_empty_bufferized_region(op.chunk_reduce.block.args),
                self._get_empty_bufferized_region(op.post_process.block.args),
            ],
            properties=op.properties,
            attributes=op.attributes,
        )

        # insert to_tensor ops and create arg mappings for block inlining
        chunk_reduce_arg_mapping: Sequence[SSAValue] = []
        for idx, (old_arg, arg) in enumerate(
            zip(op.chunk_reduce.block.args, buf_apply_op.chunk_reduce.block.args)
        ):
            # arg0 has special meaning and does not need a `to_tensor` op
            if isattr(old_arg.type, TensorType) and idx != 0:
                rewriter.insert_op(
                    # ensure iter_arg is writable
                    t := to_tensor_op(arg, writable=idx == 2),
                    InsertPoint.at_end(buf_apply_op.chunk_reduce.block),
                )
                if idx == 2:
                    offset_arg = buf_apply_op.chunk_reduce.block.args[1]
                    rewriter.insert_op(
                        self._build_extract_slice(op, t, offset_arg),
                        InsertPoint.at_end(buf_apply_op.chunk_reduce.block),
                    )
                chunk_reduce_arg_mapping.append(t.tensor)
            else:
                chunk_reduce_arg_mapping.append(arg)

        post_process_arg_mapping: Sequence[SSAValue] = []
        for idx, (old_arg, arg) in enumerate(
            zip(op.post_process.block.args, buf_apply_op.post_process.block.args)
        ):
            if isattr(old_arg.type, TensorType):
                rewriter.insert_op(
                    # ensure iter_arg is writable
                    t := to_tensor_op(arg, writable=idx == 1),
                    InsertPoint.at_end(buf_apply_op.post_process.block),
                )
                post_process_arg_mapping.append(t.tensor)
            else:
                post_process_arg_mapping.append(arg)

        # inline blocks from old into new regions
        rewriter.inline_block(
            op.chunk_reduce.block,
            InsertPoint.at_end(buf_apply_op.chunk_reduce.block),
            chunk_reduce_arg_mapping,
        )

        rewriter.inline_block(
            op.post_process.block,
            InsertPoint.at_end(buf_apply_op.post_process.block),
            post_process_arg_mapping,
        )

        # insert new op
        rewriter.replace_matched_op(new_ops=[*to_memrefs, buf_apply_op])

    @staticmethod
    def _get_empty_bufferized_region(args: Sequence[BlockArgument]) -> Region:
        """Helper function to create a new region with bufferized arg types."""
        return Region(
            Block(
                arg_types=[
                    (
                        tensor_to_memref_type(arg.type)
                        if isattr(arg.type, TensorType)
                        else arg.type
                    )
                    for arg in args
                ]
            )
        )

    @staticmethod
    def _build_extract_slice(
        op: csl_stencil.ApplyOp, to_tensor: bufferization.ToTensorOp, offset: SSAValue
    ) -> tensor.ExtractSliceOp:
        """
        Helper function to create an early tensor.extract_slice in the apply.chunk_reduce region needed for bufferization.
        """

        # this is the unbufferized `tensor<(neighbours)x(ZDim)x(type)>` value
        assert isa(typ := op.chunk_reduce.block.args[0].type, TensorType[Attribute])

        return tensor.ExtractSliceOp(
            operands=[to_tensor.tensor, [offset], [], []],
            result_types=[TensorType(typ.get_element_type(), typ.get_shape()[1:])],
            properties={
                "static_offsets": DenseArrayBase.from_list(
                    i64, (memref.Subview.DYNAMIC_INDEX,)
                ),
                "static_sizes": DenseArrayBase.from_list(i64, typ.get_shape()[1:]),
                "static_strides": DenseArrayBase.from_list(i64, (1,)),
            },
        )


@dataclass(frozen=True)
class AccessOpBufferize(RewritePattern):
    """Bufferizes AccessOp."""

    @op_type_rewrite_pattern
    def match_and_rewrite(self, op: csl_stencil.AccessOp, rewriter: PatternRewriter, /):
        if not isa(op.result.type, TensorType[Attribute]):
            return
        rewriter.replace_matched_op(
            [
                access := csl_stencil.AccessOp(
                    op.op,
                    op.offset,
                    tensor_to_memref_type(op.result.type),
                    op.offset_mapping,
                ),
                to_tensor_op(access.result),
            ]
        )


@dataclass(frozen=True)
class YieldOpBufferize(RewritePattern):
    """Bufferizes YieldOp."""

    @op_type_rewrite_pattern
    def match_and_rewrite(self, op: csl_stencil.YieldOp, rewriter: PatternRewriter, /):
        to_memrefs: list[Operation] = []
        args: list[SSAValue] = []
        for arg in op.arguments:
            if isa(arg.type, TensorType[Attribute]):
                to_memrefs.append(new_arg := to_memref_op(arg))
                args.append(new_arg.memref)
            else:
                args.append(arg)

        if len(to_memrefs) == 0:
            return

        rewriter.replace_matched_op([*to_memrefs, csl_stencil.YieldOp(*args)])


@dataclass(frozen=True)
class FuncOpBufferize(RewritePattern):
    """
    Replace the function_type and let a separate type conversion pass handle the block args.
    """

    @op_type_rewrite_pattern
    def match_and_rewrite(self, op: func.FuncOp, rewriter: PatternRewriter, /):
        function_type = FunctionType.from_lists(
            [
                (
                    tensor_to_memref_type(t.get_element_type())
                    if isa(t, stencil.FieldType[TensorType[Attribute]])
                    else t
                )
                for t in op.function_type.inputs
            ],
            [
                (
                    tensor_to_memref_type(t.get_element_type())
                    if isa(t, stencil.FieldType[TensorType[Attribute]])
                    else t
                )
                for t in op.function_type.outputs
            ],
        )
        if function_type == op.function_type:
            return
        rewriter.replace_matched_op(
            func.FuncOp.build(
                operands=op.operands,
                result_types=op.result_types,
                regions=[op.detach_region(op.body)],
                properties={**op.properties, "function_type": function_type},
                attributes=op.attributes.copy(),
            )
        )


@dataclass(frozen=True)
class ArithConstBufferize(RewritePattern):
    """
    Bufferize arith tensor constants to prevent mlir bufferize from promoting them to globals.
    """

    @op_type_rewrite_pattern
    def match_and_rewrite(self, op: arith.Constant, rewriter: PatternRewriter, /):
        if not isa(op.result.type, TensorType[Attribute]):
            return
        assert isinstance(op.value, DenseIntOrFPElementsAttr)
        assert isa(op.value.type, TensorType[Attribute])
        typ = DenseIntOrFPElementsAttr(
            [tensor_to_memref_type(op.value.type), op.value.data]
        )
        rewriter.replace_matched_op(
            [
                c := arith.Constant(typ),
                to_tensor_op(c.result),
            ]
        )


@dataclass(frozen=True)
class CslStencilBufferize(ModulePass):
    """
    Bufferizes the csl_stencil dialect.

    Creates a `tensor.extract_slice` op needed by `lift-arith-to-linalg` and should be run without `cse` in between.
    """

    name = "csl-stencil-bufferize"

    def apply(self, ctx: MLContext, op: ModuleOp) -> None:
        module_pass = PatternRewriteWalker(
            GreedyRewritePatternApplier(
                [
                    StencilTypeConversion(),
                    ApplyOpBufferize(),
                    AccessOpBufferize(),
                    YieldOpBufferize(),
                    FuncOpBufferize(),
                    ArithConstBufferize(),
                ]
            )
        )
        module_pass.rewrite_module(op)