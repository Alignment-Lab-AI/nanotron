import pytest
import torch

from nanotron.fp8.tensor import FP8Tensor
from nanotron.fp8.dtypes import DTypes
from nanotron.fp8.meta import FP8Meta
from nanotron.fp8.parameter import FP8Parameter


@pytest.mark.parametrize("dtype", [DTypes.FP8E4M3, DTypes.FP8E5M2])
def test_create_fp8_parameter(dtype):
    # TODO(xrsrke): test FP8E5M2 format
    # TODO(xrsrke): test take a cpu tensor
    tensor = torch.randn(16, 16, device="cuda", dtype=torch.float32)

    fp8_parameter = FP8Parameter(tensor, dtype)

    assert isinstance(fp8_parameter.data, FP8Tensor)
    assert fp8_parameter.requires_grad is True
    assert fp8_parameter.grad is None
    assert isinstance(fp8_parameter.fp8_meta, FP8Meta)
    assert isinstance(fp8_parameter.data.fp8_meta, FP8Meta)


def test_fp8_parameter_grad_metadata():
    GRAD_META = ["input_grad", "weight_grad", "output_grad"]
    tensor = torch.randn(16, 16, device="cuda", dtype=torch.float32)
    fp8_parameter = FP8Parameter(tensor, DTypes.FP8E4M3)

    assert all(hasattr(fp8_parameter.fp8_grad_meta, attr) for attr in GRAD_META)
    assert all([isinstance(getattr(fp8_parameter.fp8_grad_meta, attr), FP8Meta) for attr in GRAD_META])


# TODO(xrsrke): add test for preventing torch autograd do the backward pass
# on a FP8Parameter
