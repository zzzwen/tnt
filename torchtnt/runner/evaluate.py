# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import logging
from typing import Iterable, List, Optional

import torch
from torchtnt.runner.callback import Callback

from torchtnt.runner.state import EntryPoint, PhaseState, State
from torchtnt.runner.unit import TEvalData, TEvalUnit
from torchtnt.runner.utils import (
    _is_epoch_done,
    _reset_module_training_mode,
    _run_callback_fn,
    _set_module_training_mode,
    _step_requires_iterator,
    log_api_usage,
)
from torchtnt.utils.timer import get_timer_summary

logger: logging.Logger = logging.getLogger(__name__)


def evaluate(
    eval_unit: TEvalUnit,
    dataloader: Iterable[TEvalData],
    *,
    callbacks: Optional[List[Callback]] = None,
    max_steps_per_epoch: Optional[int] = None,
) -> State:
    """
    The `evaluate` entry point takes in an EvalUnit and dataloader and runs the evaluation loop over the data.

    Args:
        eval_unit: an instance of EvalUnit which implements `eval_step`.
        dataloader: dataloader to be used during evaluation.
        callbacks: an optional list of callbacks.
        max_steps_per_epoch: the max number of steps to run per epoch. None means evaluate until the dataloader is exhausted.

    Returns:
        a State object containing metadata about the evaluation run.
    """
    log_api_usage("evaluate")
    callbacks = callbacks or []
    state = State(
        entry_point=EntryPoint.EVALUATE,
        eval_state=PhaseState(
            dataloader=dataloader,
            max_steps_per_epoch=max_steps_per_epoch,
        ),
    )
    try:
        _evaluate_impl(state, eval_unit, callbacks)
        logger.info("Finished evaluation")
        logger.debug(get_timer_summary(state.timer))
        return state
    except Exception as e:
        # TODO: log for diagnostics
        logger.info(e)
        eval_unit.on_exception(state, e)
        _run_callback_fn(callbacks, "on_exception", state, eval_unit, e)
        raise e


@torch.no_grad()
def _evaluate_impl(
    state: State,
    eval_unit: TEvalUnit,
    callbacks: List[Callback],
) -> None:
    # input validation
    eval_state = state.eval_state
    if not eval_state:
        raise RuntimeError("Expected eval_state to be initialized!")
    logger.info(
        f"Started evaluate with max_steps_per_epoch={eval_state.max_steps_per_epoch}"
    )

    # Set all modules to eval mode
    # access modules made available through _AppStateMixin
    tracked_modules = eval_unit.tracked_modules()
    prior_module_train_states = _set_module_training_mode(tracked_modules, False)

    with state.timer.time(f"eval.{eval_unit.__class__.__name__}.on_eval_start"):
        eval_unit.on_eval_start(state)
    _run_callback_fn(callbacks, "on_eval_start", state, eval_unit)

    # Conditionally run this to avoid running this multiple times
    # in the case of resuming from a checkpoint mid-epoch
    if eval_state.progress.num_steps_completed_in_epoch == 0:
        with state.timer.time(
            f"eval.{eval_unit.__class__.__name__}.on_eval_epoch_start"
        ):
            eval_unit.on_eval_epoch_start(state)
        _run_callback_fn(callbacks, "on_eval_epoch_start", state, eval_unit)

    data_iter = iter(eval_state.dataloader)
    step_input = data_iter

    pass_data_iter_to_step = _step_requires_iterator(eval_unit.eval_step)
    prev_steps_in_epoch = eval_state.progress.num_steps_completed_in_epoch

    while not (
        state.should_stop
        or _is_epoch_done(
            eval_state.progress, eval_state.max_steps_per_epoch, eval_state.max_steps
        )
    ):
        try:
            if not pass_data_iter_to_step:
                # get the next batch from the data iterator
                with state.timer.time("eval.data_iter_next"):
                    step_input = next(data_iter)
            _run_callback_fn(callbacks, "on_eval_step_start", state, eval_unit)
            with state.timer.time(f"eval.{eval_unit.__class__.__name__}.eval_step"):
                eval_state._step_output = eval_unit.eval_step(state, step_input)
            _run_callback_fn(callbacks, "on_eval_step_end", state, eval_unit)
            # clear step_output to avoid retaining extra memory
            eval_state._step_output = None
            eval_state.progress.increment_step()
        except StopIteration:
            break

    # Possibly warn about an empty dataloader
    any_steps_completed = (
        abs(eval_state.progress.num_steps_completed_in_epoch - prev_steps_in_epoch) > 0
    )
    if not any_steps_completed:
        logger.warning("No steps completed during evaluate epoch!")

    with state.timer.time(f"eval.{eval_unit.__class__.__name__}.on_eval_epoch_end"):
        eval_unit.on_eval_epoch_end(state)
    _run_callback_fn(callbacks, "on_eval_epoch_end", state, eval_unit)

    # set progress counters for the next epoch
    eval_state.progress.increment_epoch()

    with state.timer.time(f"eval.{eval_unit.__class__.__name__}.on_eval_end"):
        eval_unit.on_eval_end(state)
    _run_callback_fn(callbacks, "on_eval_end", state, eval_unit)

    # Reset training mode for modules at the end of the epoch
    # This ensures that side-effects made by the loop are reset before
    # returning back to the user
    _reset_module_training_mode(tracked_modules, prior_module_train_states)
