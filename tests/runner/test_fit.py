#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import unittest
from typing import Tuple

import torch
from torch import nn

from torchtnt.runner._test_utils import DummyFitUnit, generate_random_dataloader
from torchtnt.runner.fit import fit
from torchtnt.runner.state import State
from torchtnt.runner.unit import EvalUnit, TrainUnit


class FitTest(unittest.TestCase):
    def test_fit_evaluate_every_n_epochs(self) -> None:
        """
        Test fit entry point with evaluate_every_n_epochs=1
        """
        input_dim = 2
        train_dataset_len = 8
        eval_dataset_len = 4
        batch_size = 2
        max_epochs = 3
        evaluate_every_n_epochs = 1
        expected_train_steps_per_epoch = train_dataset_len / batch_size
        expected_eval_steps_per_epoch = eval_dataset_len / batch_size
        expected_num_evaluate_calls = max_epochs / evaluate_every_n_epochs

        my_unit = DummyFitUnit(input_dim=input_dim)

        train_dataloader = generate_random_dataloader(
            train_dataset_len, input_dim, batch_size
        )
        eval_dataloader = generate_random_dataloader(
            eval_dataset_len, input_dim, batch_size
        )

        state = fit(
            my_unit,
            train_dataloader,
            eval_dataloader,
            max_epochs=max_epochs,
            evaluate_every_n_epochs=evaluate_every_n_epochs,
        )

        self.assertEqual(state.train_state.progress.num_epochs_completed, max_epochs)
        self.assertEqual(state.train_state.progress.num_steps_completed_in_epoch, 0)
        self.assertEqual(
            state.train_state.progress.num_steps_completed,
            max_epochs * expected_train_steps_per_epoch,
        )

        self.assertEqual(
            state.eval_state.progress.num_epochs_completed,
            expected_num_evaluate_calls,
        )
        self.assertEqual(state.eval_state.progress.num_steps_completed_in_epoch, 0)
        self.assertEqual(
            state.eval_state.progress.num_steps_completed,
            max_epochs * expected_eval_steps_per_epoch,
        )

        # step_output should be reset to None
        self.assertEqual(state.eval_state.step_output, None)
        self.assertEqual(state.train_state.step_output, None)

    def test_fit_evaluate_every_n_steps(self) -> None:
        """
        Test fit entry point with evaluate_every_n_steps=2
        """
        input_dim = 2
        train_dataset_len = 16
        eval_dataset_len = 4
        batch_size = 2
        max_epochs = 3
        evaluate_every_n_steps = 2
        expected_train_steps_per_epoch = train_dataset_len / batch_size
        expected_eval_steps_per_epoch = eval_dataset_len / batch_size
        expected_num_evaluate_calls_per_train_epoch = (
            expected_train_steps_per_epoch / evaluate_every_n_steps
        )
        expected_num_evaluate_calls = (
            expected_num_evaluate_calls_per_train_epoch * max_epochs
        )

        my_unit = DummyFitUnit(input_dim=input_dim)

        train_dataloader = generate_random_dataloader(
            train_dataset_len, input_dim, batch_size
        )
        eval_dataloader = generate_random_dataloader(
            eval_dataset_len, input_dim, batch_size
        )

        state = fit(
            my_unit,
            train_dataloader,
            eval_dataloader,
            max_epochs=max_epochs,
            evaluate_every_n_epochs=None,
            evaluate_every_n_steps=evaluate_every_n_steps,
        )

        self.assertEqual(state.train_state.progress.num_epochs_completed, max_epochs)
        self.assertEqual(state.train_state.progress.num_steps_completed_in_epoch, 0)
        self.assertEqual(
            state.train_state.progress.num_steps_completed,
            max_epochs * expected_train_steps_per_epoch,
        )

        self.assertEqual(
            state.eval_state.progress.num_epochs_completed,
            expected_num_evaluate_calls,
        )
        self.assertEqual(state.eval_state.progress.num_steps_completed_in_epoch, 0)
        self.assertEqual(
            state.eval_state.progress.num_steps_completed,
            expected_num_evaluate_calls * expected_eval_steps_per_epoch,
        )

        # step_output should be reset to None
        self.assertEqual(state.eval_state.step_output, None)
        self.assertEqual(state.train_state.step_output, None)

    def test_fit_stop(self) -> None:
        Batch = Tuple[torch.Tensor, torch.Tensor]

        class FitStop(TrainUnit[Batch], EvalUnit[Batch]):
            def __init__(self, input_dim: int, steps_before_stopping: int) -> None:
                super().__init__()
                # initialize module, loss_fn, & optimizer
                self.module = nn.Linear(input_dim, 2)
                self.loss_fn = nn.CrossEntropyLoss()
                self.optimizer = torch.optim.SGD(self.module.parameters(), lr=0.01)
                self.steps_processed = 0
                self.steps_before_stopping = steps_before_stopping

            def train_step(
                self, state: State, data: Batch
            ) -> Tuple[torch.Tensor, torch.Tensor]:
                inputs, targets = data

                outputs = self.module(inputs)
                loss = self.loss_fn(outputs, targets)
                loss.backward()

                self.optimizer.step()
                self.optimizer.zero_grad()

                assert state.train_state
                if (
                    state.train_state.progress.num_steps_completed_in_epoch + 1
                    == self.steps_before_stopping
                ):
                    state.stop()

                self.steps_processed += 1
                return loss, outputs

            def eval_step(
                self, state: State, data: Batch
            ) -> Tuple[torch.Tensor, torch.Tensor]:
                inputs, targets = data
                outputs = self.module(inputs)
                loss = self.loss_fn(outputs, targets)
                self.steps_processed += 1
                return loss, outputs

        input_dim = 2
        dataset_len = 10
        batch_size = 2
        max_epochs = 3
        max_steps_per_epoch = 4
        steps_before_stopping = 2

        my_unit = FitStop(
            input_dim=input_dim, steps_before_stopping=steps_before_stopping
        )
        train_dl = generate_random_dataloader(dataset_len, input_dim, batch_size)
        eval_dl = generate_random_dataloader(dataset_len, input_dim, batch_size)
        state = fit(
            my_unit,
            train_dl,
            eval_dl,
            max_epochs=max_epochs,
            max_train_steps_per_epoch=max_steps_per_epoch,
        )
        self.assertEqual(state.train_state.progress.num_epochs_completed, 1)
        self.assertEqual(state.train_state.progress.num_steps_completed_in_epoch, 0)
        self.assertEqual(
            my_unit.steps_processed, state.train_state.progress.num_steps_completed
        )
        self.assertEqual(my_unit.steps_processed, steps_before_stopping)
        self.assertEqual(state.eval_state.progress.num_epochs_completed, 1)
        self.assertEqual(state.eval_state.progress.num_steps_completed, 0)
        self.assertEqual(state.eval_state.progress.num_steps_completed_in_epoch, 0)
