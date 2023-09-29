from functools import cache
from typing import Any, Callable, Dict, Iterable, Optional, Set, Tuple, Union, Iterator

import torch

from nanotron.core.gradient_accumulator import GradientAccumulator
from nanotron.core.optimizer.base import BaseOptimizer
from nanotron.core.optimizer.inherit_from_other_optimizer import InheritFromOtherOptimizer
from nanotron.core.parallelism.parameters import NanotronParameter


class OptimizerFromGradientAccumulator(InheritFromOtherOptimizer):
    def __init__(
        self,
        gradient_accumulator_builder: Callable[[Iterator[Tuple[str, NanotronParameter]]], GradientAccumulator],
        named_params_or_groups: Iterator[Union[Tuple[str, torch.Tensor], Dict[str, Any]]],
        optimizer_builder: Callable[[Iterable[Dict[str, Any]]], BaseOptimizer],
    ):
        named_param_groups = list(named_params_or_groups)
        if len(named_param_groups) == 0 or not isinstance(named_param_groups[0], dict):
            named_param_groups = [{"named_params": named_param_groups}]

        name_to_param = {}
        for named_param_group in named_param_groups:
            for name, param in named_param_group["named_params"]:
                if name in name_to_param:
                    raise ValueError(f"Duplicate key. {name} is already in `name_to_param`")
                else:
                    name_to_param[name] = param

        # Build gradient accumulator
        gradient_accumulator = gradient_accumulator_builder(name_to_param.items())
        self.gradient_accumulator = gradient_accumulator

        # Obtained new params depending on the gradient accumulator
        converted_named_param_group = [
            {
                **{k: v for k, v in named_param_group.items() if k != "named_params"},
                "named_params": [
                    (name, gradient_accumulator.get_parameter_for_optimizer(name))
                    for name, _ in named_param_group["named_params"]
                ],
            }
            for named_param_group in named_param_groups
        ]
        optimizer = optimizer_builder(converted_named_param_group)

        super().__init__(optimizer=optimizer, id_to_name=optimizer.id_to_name)

    def step(self, closure: Optional[Callable[[], float]] = None) -> Optional[float]:
        loss = super().step(closure)
        self.gradient_accumulator.step()
        return loss

    def zero_grad(self, set_to_none: bool = False):
        super().zero_grad(set_to_none=set_to_none)
        return self.gradient_accumulator.zero_grad(set_to_none=set_to_none)

    @cache
    def state_dict_additional_keys(self) -> Set[str]:
        return super().state_dict_additional_keys() | {"gradient_accumulator"}

    def state_dict(self) -> dict:
        state_dict = super().state_dict()

        assert "gradient_accumulator" not in state_dict
        state_dict["gradient_accumulator"] = self.gradient_accumulator.state_dict()
        return state_dict

    def load_state_dict(self, state_dict: dict) -> None:
        gradient_accumulator_state_dict = state_dict.pop("gradient_accumulator")
        super().load_state_dict(state_dict)
        self.gradient_accumulator.load_state_dict(gradient_accumulator_state_dict)