import math
from typing import Any, Dict, List, Tuple

import torch
from torch import nn
from torch.optim import Optimizer

from nanotron.fp8.parameter import FP8Parameter


class FP8Adam(Optimizer):
    def __init__(
        self,
        params: List[FP8Parameter],
        lr: float = 1e-3,
        betas: Tuple[float, float] = (0.9, 0.999),
        eps: float = 1e-8,
        weight_decay: float = 0,
        amsgrad: bool = False,
    ):
        assert [isinstance(p, FP8Parameter) for p in params], "All parameters should be FP8Parameter"

        if not 0.0 <= lr:
            raise ValueError("Invalid learning rate: {}".format(lr))
        if not 0.0 <= eps:
            raise ValueError("Invalid epsilon value: {}".format(eps))
        if not 0.0 <= betas[0] < 1.0:
            raise ValueError("Invalid beta parameter at index 0: {}".format(betas[0]))
        if not 0.0 <= betas[1] < 1.0:
            raise ValueError("Invalid beta parameter at index 1: {}".format(betas[1]))
        defaults = {"lr": lr, "betas": betas, "eps": eps, "weight_decay": weight_decay, "amsgrad": amsgrad}

        super().__init__(params, defaults)

    def __setstate__(self, state):
        super().__setstate__(state)
        for group in self.param_groups:
            group.setdefault("amsgrad", False)

    def _init_optim_states(self, state: Dict[str, Any], p: nn.Parameter, amsgrad: bool) -> None:
        state["step"] = 0
        # Exponential moving average of gradient values
        state["exp_avg"] = torch.zeros_like(p.data, memory_format=torch.preserve_format)
        # Exponential moving average of squared gradient values
        state["exp_avg_sq"] = torch.zeros_like(p.data, memory_format=torch.preserve_format)
        if amsgrad:
            # Maintains max of all exp. moving avg. of sq. grad. values
            state["max_exp_avg_sq"] = torch.zeros_like(p.data, memory_format=torch.preserve_format)

    def step(self):
        for group in self.param_groups:
            for p in group["params"]:
                if p.grad is None:
                    continue

                grad = p.grad.data

                # if grad.is_sparse:
                #     raise RuntimeError('Adam does not support sparse gradients, please consider SparseAdam instead')

                amsgrad = group["amsgrad"]

                state = self.state[p]

                if len(state) == 0:
                    self._init_optim_states(state, p, amsgrad)

                exp_avg, exp_avg_sq = state["exp_avg"], state["exp_avg_sq"]
                if amsgrad:
                    max_exp_avg_sq = state["max_exp_avg_sq"]
                beta1, beta2 = group["betas"]

                state["step"] += 1
                bias_correction1 = 1 - beta1 ** state["step"]
                bias_correction2 = 1 - beta2 ** state["step"]

                if group["weight_decay"] != 0:
                    grad = grad.add(group["weight_decay"], p.data)

                # Decay the first and second moment running average coefficient
                exp_avg.mul_(beta1).add_(1 - beta1, grad)
                exp_avg_sq.mul_(beta2).addcmul_(1 - beta2, grad, grad)
                if amsgrad:
                    # Maintains the maximum of all 2nd moment running avg. till now
                    torch.max(max_exp_avg_sq, exp_avg_sq, out=max_exp_avg_sq)
                    # Use the max. for normalizing running avg. of gradient
                    denom = (max_exp_avg_sq.sqrt() / math.sqrt(bias_correction2)).add_(group["eps"])
                else:
                    denom = (exp_avg_sq.sqrt() / math.sqrt(bias_correction2)).add_(group["eps"])

                step_size = group["lr"] / bias_correction1

                p.data.addcdiv_(-step_size, exp_avg, denom)

    # TODO(xrsrke): refactor using strategy pattern
    def _update_scaling_factors(self):
        for p in self.param_groups:
            for param in p["params"]:
                if param.grad is None:
                    continue