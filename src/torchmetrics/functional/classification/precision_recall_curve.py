# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import List, Optional, Sequence, Tuple, Union

import torch
from torch import Tensor, tensor
from torch.nn import functional as F

from torchmetrics.utilities import rank_zero_warn
from torchmetrics.utilities.checks import _check_same_shape
from torchmetrics.utilities.compute import _safe_divide
from torchmetrics.utilities.data import _bincount


def _binary_clf_curve(
    preds: Tensor,
    target: Tensor,
    sample_weights: Optional[Sequence] = None,
    pos_label: int = 1,
) -> Tuple[Tensor, Tensor, Tensor]:
    """adapted from https://github.com/scikit-learn/scikit- learn/blob/master/sklearn/metrics/_ranking.py."""
    if sample_weights is not None and not isinstance(sample_weights, Tensor):
        sample_weights = tensor(sample_weights, device=preds.device, dtype=torch.float)

    # remove class dimension if necessary
    if preds.ndim > target.ndim:
        preds = preds[:, 0]
    desc_score_indices = torch.argsort(preds, descending=True)

    preds = preds[desc_score_indices]
    target = target[desc_score_indices]

    if sample_weights is not None:
        weight = sample_weights[desc_score_indices]
    else:
        weight = 1.0

    # pred typically has many tied values. Here we extract
    # the indices associated with the distinct values. We also
    # concatenate a value for the end of the curve.
    distinct_value_indices = torch.where(preds[1:] - preds[:-1])[0]
    threshold_idxs = F.pad(distinct_value_indices, [0, 1], value=target.size(0) - 1)
    target = (target == pos_label).to(torch.long)
    tps = torch.cumsum(target * weight, dim=0)[threshold_idxs]

    if sample_weights is not None:
        # express fps as a cumsum to ensure fps is increasing even in
        # the presence of floating point errors
        fps = torch.cumsum((1 - target) * weight, dim=0)[threshold_idxs]
    else:
        fps = 1 + threshold_idxs - tps

    return fps, tps, preds[threshold_idxs]


def _binary_precision_recall_curve_arg_validation(
    thresholds: Optional[Union[int, List[float], Tensor]] = 100,
    ignore_index: Optional[int] = None,
) -> None:
    if thresholds is not None and not isinstance(thresholds, (list, int, Tensor)):
        raise ValueError(
            "Expected argument `thresholds` to either be an integer, list of floats or"
            f" tensor of floats, but got {thresholds}"
        )
    else:
        if isinstance(thresholds, int) and thresholds < 2:
            raise ValueError(
                f"If argument `thresholds` is an integer, expected it to be larger than 1, but got {thresholds}"
            )
        if isinstance(thresholds, list) and not all(isinstance(t, float) and 0 <= t <= 1 for t in thresholds):
            raise ValueError(
                "If argument `thresholds` is a list, expected all elements to be floats in the [0,1] range,"
                f" but got {thresholds}"
            )
        if isinstance(thresholds, Tensor) and not thresholds.ndim == 1:
            raise ValueError("If argument `thresholds` is an tensor, expected the tensor to be 1d")

    if ignore_index is not None and not isinstance(ignore_index, int):
        raise ValueError(f"Expected argument `ignore_index` to either be `None` or an integer, but got {ignore_index}")


def _adjust_threshold_arg(
    thresholds: Optional[Union[int, List[float], Tensor]] = 100, device: Optional[torch.device] = None
) -> Tensor:
    if isinstance(thresholds, int):
        thresholds = torch.linspace(0, 1, thresholds, device=device)
    if isinstance(thresholds, list):
        thresholds = torch.tensor(thresholds, device=device)
    return thresholds


def _binary_precision_recall_curve_tensor_validation(
    preds: Tensor, target: Tensor, ignore_index: Optional[int] = None
) -> None:
    _check_same_shape(preds, target)

    if not preds.is_floating_point():
        raise ValueError(
            "Expected argument `preds` to be an floating tensor with probability/logit scores,"
            f" but got tensor with dtype {preds.dtype}"
        )

    # Check that target only contains {0,1} values or value in ignore_index
    unique_values = torch.unique(target)
    if ignore_index is None:
        check = torch.any((unique_values != 0) & (unique_values != 1))
    else:
        check = torch.any((unique_values != 0) & (unique_values != 1) & (unique_values != ignore_index))
    if check:
        raise RuntimeError(
            f"Detected the following values in `target`: {unique_values} but expected only"
            f" the following values {[0,1] + [] if ignore_index is None else [ignore_index]}."
        )


def _binary_precision_recall_curve_format(
    preds: Tensor,
    target: Tensor,
    thresholds: Optional[Union[int, List[float], Tensor]] = 100,
    ignore_index: Optional[int] = None,
) -> Tuple[Tensor, Tensor, Optional[Tensor]]:
    preds = preds.flatten()
    target = target.flatten()
    if ignore_index is not None:
        idx = target != ignore_index
        preds = preds[idx]
        target = target[idx]

    if not torch.all((0 <= preds) * (preds <= 1)):
        preds = preds.sigmoid()

    thresholds = _adjust_threshold_arg(thresholds, preds.device)
    return preds, target, thresholds


def _binary_precision_recall_curve_update(
    preds: Tensor,
    target: Tensor,
    thresholds: Optional[Tensor],
) -> Union[Tensor, Tuple[Tensor, Tensor]]:
    if thresholds is None:
        return preds, target
    len_t = len(thresholds)
    preds_t = (preds.unsqueeze(-1) >= thresholds.unsqueeze(0)).long()  # num_samples x num_thresholds
    unique_mapping = preds_t + 2 * target.unsqueeze(-1) + 4 * torch.arange(len_t, device=target.device)
    bins = _bincount(unique_mapping.flatten(), minlength=4 * len_t)
    return bins.reshape(len_t, 2, 2)


def _binary_precision_recall_curve_compute(
    state: Union[Tensor, Tuple[Tensor, Tensor]],
    thresholds: Optional[Tensor],
    pos_label: int = 1,
):
    if isinstance(state, Tensor):
        tps = state[:, 1, 1]
        fps = state[:, 0, 1]
        fns = state[:, 1, 0]
        precision = _safe_divide(tps, tps + fps)
        recall = _safe_divide(tps, tps + fns)
        precision = torch.cat([precision, torch.ones(1, dtype=precision.dtype, device=precision.device)])
        recall = torch.cat([recall, torch.zeros(1, dtype=recall.dtype, device=recall.device)])
        return precision, recall, thresholds
    else:
        fps, tps, thresholds = _binary_clf_curve(state[0], state[1], pos_label=pos_label)
        precision = tps / (tps + fps)
        recall = tps / tps[-1]

        # stop when full recall attained and reverse the outputs so recall is decreasing
        last_ind = torch.where(tps == tps[-1])[0][0]
        sl = slice(0, last_ind.item() + 1)

        # need to call reversed explicitly, since including that to slice would
        # introduce negative strides that are not yet supported in pytorch
        precision = torch.cat([reversed(precision[sl]), torch.ones(1, dtype=precision.dtype, device=precision.device)])
        recall = torch.cat([reversed(recall[sl]), torch.zeros(1, dtype=recall.dtype, device=recall.device)])
        thresholds = reversed(thresholds[sl]).detach().clone()  # type: ignore

    return precision, recall, thresholds


def binary_precision_recall_curve(
    preds: Tensor,
    target: Tensor,
    thresholds: Optional[Union[int, List[float], Tensor]] = 100,
    ignore_index: Optional[int] = None,
    validate_args: bool = True,
) -> Tuple[Tensor, Tensor, Tensor]:
    if validate_args:
        _binary_precision_recall_curve_arg_validation(thresholds, ignore_index)
        _binary_precision_recall_curve_tensor_validation(preds, target, ignore_index)
    preds, target, thresholds = _binary_precision_recall_curve_format(preds, target, thresholds, ignore_index)
    state = _binary_precision_recall_curve_update(preds, target, thresholds)
    return _binary_precision_recall_curve_compute(state, thresholds)


def _multiclass_precision_recall_curve_arg_validation(
    num_classes: int,
    thresholds: Optional[Union[int, List[float], Tensor]] = 100,
    ignore_index: Optional[int] = None,
) -> None:
    if not isinstance(num_classes, int) or num_classes < 2:
        raise ValueError(f"Expected argument `num_classes` to be an integer larger than 1, but got {num_classes}")
    _binary_precision_recall_curve_arg_validation(thresholds, ignore_index)


def _multiclass_precision_recall_curve_tensor_validation(
    preds: Tensor, target: Tensor, num_classes: int, ignore_index: Optional[int] = None
) -> None:
    if not preds.ndim == target.ndim + 1:
        raise ValueError(
            f"Expected `preds` to have one more dimension than `target` but got {preds.ndim} and {target.ndim}"
        )
    if not preds.is_floating_point():
        raise ValueError(f"Expected `preds` to be a float tensor, but got {preds.dtype}")
    if preds.shape[1] != num_classes:
        raise ValueError(
            "Expected `preds.shape[1]` to be equal to the number of classes but"
            f" got {preds.shape[1]} and {num_classes}."
        )
    if preds.shape[0] != target.shape[0] or preds.shape[2:] != target.shape[1:]:
        raise ValueError(
            "Expected the shape of `preds` should be (N, C, ...) and the shape of `target` should be (N, ...)"
            f" but got {preds.shape} and {target.shape}"
        )

    num_unique_values = len(torch.unique(target))
    if ignore_index is None:
        check = num_unique_values > num_classes
    else:
        check = num_unique_values > num_classes + 1
    if check:
        raise RuntimeError(
            "Detected more unique values in `target` than `num_classes`. Expected only "
            f"{num_classes if ignore_index is None else num_classes + 1} but found "
            f"{num_unique_values} in `target`."
        )


def _multiclass_precision_recall_curve_format(
    preds: Tensor,
    target: Tensor,
    num_classes: int,
    thresholds: Optional[Union[int, List[float], Tensor]] = 100,
    ignore_index: Optional[int] = None,
) -> Tuple[Tensor, Tensor, Optional[Tensor]]:
    preds = preds.transpose(0, 1).reshape(num_classes, -1).T
    target = target.flatten()

    if ignore_index is not None:
        idx = target != ignore_index
        preds = preds[idx]
        target = target[idx]

    if not torch.all((0 <= preds) * (preds <= 1)):
        preds = preds.softmax(1)

    thresholds = _adjust_threshold_arg(thresholds, preds.device)
    return preds, target, thresholds


def _multiclass_precision_recall_curve_update(
    preds: Tensor,
    target: Tensor,
    num_classes: int,
    thresholds: Optional[Tensor],
) -> Union[Tensor, Tuple[Tensor, Tensor]]:
    if thresholds is None:
        return preds, target
    len_t = len(thresholds)
    # num_samples x num_classes x num_thresholds
    preds_t = (preds.unsqueeze(-1) >= thresholds.unsqueeze(0).unsqueeze(0)).long()
    target_t = torch.nn.functional.one_hot(target, num_classes=num_classes)
    unique_mapping = preds_t + 2 * target_t.unsqueeze(-1)
    unique_mapping += 4 * torch.arange(num_classes, device=preds.device).unsqueeze(0).unsqueeze(-1)
    unique_mapping += 4 * num_classes * torch.arange(len_t, device=preds.device)
    bins = _bincount(unique_mapping.flatten(), minlength=4 * num_classes * len_t)
    return bins.reshape(len_t, num_classes, 2, 2)


def _multiclass_precision_recall_curve_compute(
    state: Union[Tensor, Tuple[Tensor, Tensor]],
    num_classes: int,
    thresholds: Optional[Tensor],
) -> Union[Tuple[Tensor, Tensor, Tensor], Tuple[List[Tensor], List[Tensor], List[Tensor]]]:

    if isinstance(state, Tensor):
        tps = state[:, :, 1, 1]
        fps = state[:, :, 0, 1]
        fns = state[:, :, 1, 0]
        precision = _safe_divide(tps, tps + fps)
        recall = _safe_divide(tps, tps + fns)
        precision = torch.cat([precision, torch.ones(1, num_classes, dtype=precision.dtype, device=precision.device)])
        recall = torch.cat([recall, torch.zeros(1, num_classes, dtype=recall.dtype, device=recall.device)])
        return precision.T, recall.T, thresholds
    else:
        precision, recall, thresholds = [], [], []
        for i in range(num_classes):
            res = _binary_precision_recall_curve_compute([state[0][:, i], state[1]], thresholds, pos_label=i)
            precision.append(res[0])
            recall.append(res[1])
            thresholds.append(res[2])
    return precision, recall, thresholds


def multiclass_precision_recall_curve(
    preds: Tensor,
    target: Tensor,
    num_classes: int,
    thresholds: Optional[Union[int, List[float], Tensor]] = 100,
    ignore_index: Optional[int] = None,
    validate_args: bool = True,
) -> Tuple[Tensor, Tensor, Tensor]:
    if validate_args:
        _multiclass_precision_recall_curve_arg_validation(num_classes, thresholds, ignore_index)
        _multiclass_precision_recall_curve_tensor_validation(preds, target, num_classes, ignore_index)
    preds, target, thresholds = _multiclass_precision_recall_curve_format(
        preds, target, num_classes, thresholds, ignore_index
    )
    state = _multiclass_precision_recall_curve_update(preds, target, num_classes, thresholds)
    return _multiclass_precision_recall_curve_compute(state, num_classes, thresholds)


def _multilabel_precision_recall_curve_arg_validation(
    num_labels: int,
    thresholds: Optional[Union[int, List[float], Tensor]] = 100,
    ignore_index: Optional[int] = None,
) -> None:
    _multiclass_precision_recall_curve_arg_validation(num_labels, thresholds, ignore_index)


def _multilabel_precision_recall_curve_tensor_validation(
    preds: Tensor, target: Tensor, num_labels: int, ignore_index: Optional[int] = None
) -> None:
    _binary_precision_recall_curve_tensor_validation(preds, target, ignore_index)
    if preds.shape[1] != num_labels:
        raise ValueError(
            "Expected both `target.shape[1]` and `preds.shape[1]` to be equal to the number of labels"
            f" but got {preds.shape[1]} and expected {num_labels}"
        )


def _multilabel_precision_recall_curve_format(
    preds: Tensor,
    target: Tensor,
    num_labels: int,
    thresholds: Optional[Union[int, List[float], Tensor]] = 100,
    ignore_index: Optional[int] = None,
) -> Tuple[Tensor, Tensor, Optional[Tensor]]:
    preds = preds.transpose(0, 1).reshape(num_labels, -1).T
    target = target.transpose(0, 1).reshape(num_labels, -1).T

    if not torch.all((0 <= preds) * (preds <= 1)):
        preds = preds.sigmoid()

    thresholds = _adjust_threshold_arg(thresholds, preds.device)
    if ignore_index is not None:
        preds = preds.clone()
        target = target.clone()
        # Make sure that when we map, it will always result in a negative number that we can filter away
        idx = target == ignore_index
        preds[idx] = -4 * num_labels * (len(thresholds) if thresholds is not None else 1)
        target[idx] = -4 * num_labels * (len(thresholds) if thresholds is not None else 1)

    return preds, target, thresholds


def _multilabel_precision_recall_curve_update(
    preds: Tensor,
    target: Tensor,
    num_labels: int,
    thresholds: Optional[Tensor],
) -> Union[Tensor, Tuple[Tensor, Tensor]]:
    if thresholds is None:
        return preds, target
    len_t = len(thresholds)
    # num_samples x num_labels x num_thresholds
    preds_t = (preds.unsqueeze(-1) >= thresholds.unsqueeze(0).unsqueeze(0)).long()
    unique_mapping = preds_t + 2 * target.unsqueeze(-1)
    unique_mapping += 4 * torch.arange(num_labels, device=preds.device).unsqueeze(0).unsqueeze(-1)
    unique_mapping += 4 * num_labels * torch.arange(len_t, device=preds.device)
    unique_mapping = unique_mapping[unique_mapping >= 0]
    bins = _bincount(unique_mapping, minlength=4 * num_labels * len_t)
    return bins.reshape(len_t, num_labels, 2, 2)


def _multilabel_precision_recall_curve_compute(
    state: Union[Tensor, Tuple[Tensor, Tensor]],
    num_labels: int,
    thresholds: Optional[Tensor],
    ignore_index: Optional[int] = None,
) -> Union[Tuple[Tensor, Tensor, Tensor], Tuple[List[Tensor], List[Tensor], List[Tensor]]]:

    if isinstance(state, Tensor):
        tps = state[:, :, 1, 1]
        fps = state[:, :, 0, 1]
        fns = state[:, :, 1, 0]
        precision = _safe_divide(tps, tps + fps)
        recall = _safe_divide(tps, tps + fns)
        precision = torch.cat([precision, torch.ones(1, num_labels, dtype=precision.dtype, device=precision.device)])
        recall = torch.cat([recall, torch.zeros(1, num_labels, dtype=recall.dtype, device=recall.device)])
        return precision.T, recall.T, thresholds
    else:
        precision, recall, thresholds = [], [], []
        for i in range(num_labels):
            preds = state[0][:, i]
            target = state[1][:, i]
            if ignore_index is not None:
                idx = target == ignore_index
                preds = preds[~idx]
                target = target[~idx]
            res = _binary_precision_recall_curve_compute([preds, target], thresholds, pos_label=1)
            precision.append(res[0])
            recall.append(res[1])
            thresholds.append(res[2])
    return precision, recall, thresholds


def multilabel_precision_recall_curve(
    preds: Tensor,
    target: Tensor,
    num_labels: int,
    thresholds: Optional[Union[int, List[float], Tensor]] = 100,
    ignore_index: Optional[int] = None,
    validate_args: bool = True,
) -> Tuple[Tensor, Tensor, Tensor]:
    if validate_args:
        _multilabel_precision_recall_curve_arg_validation(num_labels, thresholds, ignore_index)
        _multilabel_precision_recall_curve_tensor_validation(preds, target, num_labels, ignore_index)
    preds, target, thresholds = _multilabel_precision_recall_curve_format(
        preds, target, num_labels, thresholds, ignore_index
    )
    state = _multilabel_precision_recall_curve_update(preds, target, num_labels, thresholds)
    return _multilabel_precision_recall_curve_compute(state, num_labels, thresholds, ignore_index)


# -------------------------- Old stuff --------------------------


def _precision_recall_curve_update(
    preds: Tensor,
    target: Tensor,
    num_classes: Optional[int] = None,
    pos_label: Optional[int] = None,
) -> Tuple[Tensor, Tensor, int, Optional[int]]:
    """Updates and returns variables required to compute the precision-recall pairs for different thresholds.

    Args:
        preds: Predicted tensor
        target: Ground truth tensor
        num_classes: integer with number of classes for multi-label and multiclass problems.
            Should be set to ``None`` for binary problems.
        pos_label: integer determining the positive class. Default is ``None``
            which for binary problem is translated to 1. For multiclass problems
            this argument should not be set as we iteratively change it in the
            range [0,num_classes-1]
    """

    if len(preds.shape) == len(target.shape):
        if pos_label is None:
            pos_label = 1
        if num_classes is not None and num_classes != 1:
            # multilabel problem
            if num_classes != preds.shape[1]:
                raise ValueError(
                    f"Argument `num_classes` was set to {num_classes} in"
                    f" metric `precision_recall_curve` but detected {preds.shape[1]}"
                    " number of classes from predictions"
                )
            preds = preds.transpose(0, 1).reshape(num_classes, -1).transpose(0, 1)
            target = target.transpose(0, 1).reshape(num_classes, -1).transpose(0, 1)
        else:
            # binary problem
            preds = preds.flatten()
            target = target.flatten()
            num_classes = 1

    # multi class problem
    elif len(preds.shape) == len(target.shape) + 1:
        if pos_label is not None:
            rank_zero_warn(
                "Argument `pos_label` should be `None` when running"
                f" multiclass precision recall curve. Got {pos_label}"
            )
        if num_classes != preds.shape[1]:
            raise ValueError(
                f"Argument `num_classes` was set to {num_classes} in"
                f" metric `precision_recall_curve` but detected {preds.shape[1]}"
                " number of classes from predictions"
            )
        preds = preds.transpose(0, 1).reshape(num_classes, -1).transpose(0, 1)
        target = target.flatten()

    else:
        raise ValueError("preds and target must have same number of dimensions, or one additional dimension for preds")

    return preds, target, num_classes, pos_label


def _precision_recall_curve_compute_single_class(
    preds: Tensor,
    target: Tensor,
    pos_label: int,
    sample_weights: Optional[Sequence] = None,
) -> Tuple[Tensor, Tensor, Tensor]:
    """Computes precision-recall pairs for single class inputs.

    Args:
        preds: Predicted tensor
        target: Ground truth tensor
        pos_label: integer determining the positive class.
        sample_weights: sample weights for each data point
    """

    fps, tps, thresholds = _binary_clf_curve(
        preds=preds, target=target, sample_weights=sample_weights, pos_label=pos_label
    )
    precision = tps / (tps + fps)
    recall = tps / tps[-1]

    # stop when full recall attained and reverse the outputs so recall is decreasing
    last_ind = torch.where(tps == tps[-1])[0][0]
    sl = slice(0, last_ind.item() + 1)

    # need to call reversed explicitly, since including that to slice would
    # introduce negative strides that are not yet supported in pytorch
    precision = torch.cat([reversed(precision[sl]), torch.ones(1, dtype=precision.dtype, device=precision.device)])

    recall = torch.cat([reversed(recall[sl]), torch.zeros(1, dtype=recall.dtype, device=recall.device)])

    thresholds = reversed(thresholds[sl]).detach().clone()  # type: ignore

    return precision, recall, thresholds


def _precision_recall_curve_compute_multi_class(
    preds: Tensor,
    target: Tensor,
    num_classes: int,
    sample_weights: Optional[Sequence] = None,
) -> Tuple[List[Tensor], List[Tensor], List[Tensor]]:
    """Computes precision-recall pairs for multi class inputs.

    Args:
        preds: Predicted tensor
        target: Ground truth tensor
        num_classes: integer with number of classes for multi-label and multiclass problems.
            Should be set to ``None`` for binary problems.
        sample_weights: sample weights for each data point
    """

    # Recursively call per class
    precision, recall, thresholds = [], [], []
    for cls in range(num_classes):
        preds_cls = preds[:, cls]

        prc_args = dict(
            preds=preds_cls,
            target=target,
            num_classes=1,
            pos_label=cls,
            sample_weights=sample_weights,
        )
        if target.ndim > 1:
            prc_args.update(
                dict(
                    target=target[:, cls],
                    pos_label=1,
                )
            )
        res = precision_recall_curve(**prc_args)
        precision.append(res[0])
        recall.append(res[1])
        thresholds.append(res[2])

    return precision, recall, thresholds


def _precision_recall_curve_compute(
    preds: Tensor,
    target: Tensor,
    num_classes: int,
    pos_label: Optional[int] = None,
    sample_weights: Optional[Sequence] = None,
) -> Union[Tuple[Tensor, Tensor, Tensor], Tuple[List[Tensor], List[Tensor], List[Tensor]]]:
    """Computes precision-recall pairs based on the number of classes.

    Args:
        preds: Predicted tensor
        target: Ground truth tensor
        num_classes: integer with number of classes for multi-label and multiclass problems.
            Should be set to ``None`` for binary problems.
        pos_label: integer determining the positive class. Default is ``None``
            which for binary problem is translated to 1. For multiclass problems
            this argument should not be set as we iteratively change it in the
            range ``[0,num_classes-1]``
        sample_weights: sample weights for each data point

    Example:
        >>> # binary case
        >>> preds = torch.tensor([0, 1, 2, 3])
        >>> target = torch.tensor([0, 1, 1, 0])
        >>> pos_label = 1
        >>> preds, target, num_classes, pos_label = _precision_recall_curve_update(preds, target, pos_label=pos_label)
        >>> precision, recall, thresholds = _precision_recall_curve_compute(preds, target, num_classes, pos_label)
        >>> precision
        tensor([0.6667, 0.5000, 0.0000, 1.0000])
        >>> recall
        tensor([1.0000, 0.5000, 0.0000, 0.0000])
        >>> thresholds
        tensor([1, 2, 3])

        >>> # multiclass case
        >>> preds = torch.tensor([[0.75, 0.05, 0.05, 0.05, 0.05],
        ...                      [0.05, 0.75, 0.05, 0.05, 0.05],
        ...                      [0.05, 0.05, 0.75, 0.05, 0.05],
        ...                      [0.05, 0.05, 0.05, 0.75, 0.05]])
        >>> target = torch.tensor([0, 1, 3, 2])
        >>> num_classes = 5
        >>> preds, target, num_classes, pos_label = _precision_recall_curve_update(preds, target, num_classes)
        >>> precision, recall, thresholds = _precision_recall_curve_compute(preds, target, num_classes)
        >>> precision
        [tensor([1., 1.]), tensor([1., 1.]), tensor([0.2500, 0.0000, 1.0000]),
         tensor([0.2500, 0.0000, 1.0000]), tensor([0., 1.])]
        >>> recall
        [tensor([1., 0.]), tensor([1., 0.]), tensor([1., 0., 0.]), tensor([1., 0., 0.]), tensor([nan, 0.])]
        >>> thresholds
        [tensor([0.7500]), tensor([0.7500]), tensor([0.0500, 0.7500]), tensor([0.0500, 0.7500]), tensor([0.0500])]
    """

    with torch.no_grad():
        if num_classes == 1:
            if pos_label is None:
                pos_label = 1
            return _precision_recall_curve_compute_single_class(preds, target, pos_label, sample_weights)
        return _precision_recall_curve_compute_multi_class(preds, target, num_classes, sample_weights)


def precision_recall_curve(
    preds: Tensor,
    target: Tensor,
    num_classes: Optional[int] = None,
    pos_label: Optional[int] = None,
    sample_weights: Optional[Sequence] = None,
) -> Union[Tuple[Tensor, Tensor, Tensor], Tuple[List[Tensor], List[Tensor], List[Tensor]]]:
    """Computes precision-recall pairs for different thresholds.

    Args:
        preds: predictions from model (probabilities)
        target: ground truth labels
        num_classes: integer with number of classes for multi-label and multiclass problems.
            Should be set to ``None`` for binary problems.
        pos_label: integer determining the positive class. Default is ``None`` which for binary problem is translated
            to 1. For multiclass problems this argument should not be set as we iteratively change it in the
            range ``[0, num_classes-1]``
        sample_weights: sample weights for each data point

    Returns:
        3-element tuple containing

        precision:
            tensor where element ``i`` is the precision of predictions with
            ``score >= thresholds[i]`` and the last element is 1.
            If multiclass, this is a list of such tensors, one for each class.
        recall:
            tensor where element ``i`` is the recall of predictions with
            ``score >= thresholds[i]`` and the last element is 0.
            If multiclass, this is a list of such tensors, one for each class.
        thresholds:
            Thresholds used for computing precision/recall scores

    Raises:
        ValueError:
            If ``preds`` and ``target`` don't have the same number of dimensions,
            or one additional dimension for ``preds``.
        ValueError:
            If the number of classes deduced from ``preds`` is not the same as the ``num_classes`` provided.

    Example (binary case):
        >>> from torchmetrics.functional import precision_recall_curve
        >>> pred = torch.tensor([0, 1, 2, 3])
        >>> target = torch.tensor([0, 1, 1, 0])
        >>> precision, recall, thresholds = precision_recall_curve(pred, target, pos_label=1)
        >>> precision
        tensor([0.6667, 0.5000, 0.0000, 1.0000])
        >>> recall
        tensor([1.0000, 0.5000, 0.0000, 0.0000])
        >>> thresholds
        tensor([1, 2, 3])

    Example (multiclass case):
        >>> pred = torch.tensor([[0.75, 0.05, 0.05, 0.05, 0.05],
        ...                      [0.05, 0.75, 0.05, 0.05, 0.05],
        ...                      [0.05, 0.05, 0.75, 0.05, 0.05],
        ...                      [0.05, 0.05, 0.05, 0.75, 0.05]])
        >>> target = torch.tensor([0, 1, 3, 2])
        >>> precision, recall, thresholds = precision_recall_curve(pred, target, num_classes=5)
        >>> precision
        [tensor([1., 1.]), tensor([1., 1.]), tensor([0.2500, 0.0000, 1.0000]),
         tensor([0.2500, 0.0000, 1.0000]), tensor([0., 1.])]
        >>> recall
        [tensor([1., 0.]), tensor([1., 0.]), tensor([1., 0., 0.]), tensor([1., 0., 0.]), tensor([nan, 0.])]
        >>> thresholds
        [tensor([0.7500]), tensor([0.7500]), tensor([0.0500, 0.7500]), tensor([0.0500, 0.7500]), tensor([0.0500])]
    """
    preds, target, num_classes, pos_label = _precision_recall_curve_update(preds, target, num_classes, pos_label)
    return _precision_recall_curve_compute(preds, target, num_classes, pos_label, sample_weights)