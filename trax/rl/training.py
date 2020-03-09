# coding=utf-8
# Copyright 2020 The Trax Authors.
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

# Lint as: python3
"""Classes for RL training in Trax."""

import time

from trax import layers as tl
from trax import lr_schedules as lr
from trax import supervised


class RLTrainer:
  """Abstract class for RL Trainers, presenting the required API."""

  def __init__(self, task, output_dir=None):
    """Configures the RL Trainer.

    Note that subclasses can have many more arguments, which will be configured
    using defaults and gin. But task and output_dir are passed explicitly.

    Args:
      task: RLTask instance, which defines the environment to train on.
      output_dir: Path telling where to save outputs such as checkpoints.
    """
    self._task = task
    self._output_dir = output_dir

  def policy(self, trajectory):
    """Policy function that allows to play using this trainer.

    Args:
      trajectory: an instance of trax.rl.task.Trajectory

    Returns:
      a pair (action, log_prob) where action is the action taken and log_prob
      is the probability assigned to this action (for future use, can be None).
    """
    raise NotImplementedError

  def run(self, n_epochs=1):
    """Runs the training loop for this Trainer for n epochs.

    Args:
      n_epochs: Stop training after completing n steps.
    """
    raise NotImplementedError


class ExamplePolicyTrainer(RLTrainer):
  """Trains a policy model using Reinforce on the given RLTask.

  This is meant just as an example for RL loops for other RL algorithms,
  to be used for testing puropses and as an idea for other classes.
  """

  def __init__(self, task, model=None, optimizer=None,
               lr_schedule=lr.MultifactorSchedule, batch_size=64,
               train_steps_per_epoch=500, collect_per_epoch=50,
               max_slice_length=1, output_dir=None):
    """Configures the Reinforce loop.

    Args:
      task: RLTask instance, which defines the environment to train on.
      model: Trax layer, representing the policy model.
          functions and eval functions (a.k.a. metrics) are considered to be
          outside the core model, taking core model output and data labels as
          their two inputs.
      optimizer: the optimizer to use to train the model.
      lr_schedule: learning rate schedule to use to train the model.
      batch_size: batch size used to train the model.
      train_steps_per_epoch: how long to train in each RL epoch.
      collect_per_epoch: how many trajectories to collect per epoch.
      max_slice_length: the maximum length of trajectory slices to use.
      output_dir: Path telling where to save outputs (evals and checkpoints).
          Can be None if both `eval_task` and `checkpoint_at` are None.
    """
    super(ExamplePolicyTrainer, self).__init__(task, output_dir=output_dir)
    self._batch_size = batch_size
    self._train_steps_per_epoch = train_steps_per_epoch
    self._collect_per_epoch = collect_per_epoch
    self._max_slice_length = max_slice_length
    self._epoch = 0
    self._eval_model = model(mode='eval')
    example_batch = next(self._batches_stream())
    self._eval_model.init(example_batch)

    # Inputs to the policy model are produced by self._batches_stream.
    # As you can see below, the stream returns (observation, action, return)
    # from the RLTask, which the model uses as (inputs, targets, loss weights).
    self._inputs = supervised.Inputs(
        train_stream=lambda _: self._batches_stream())

    # This is the main Trainer that will be used to train the policy using
    # a policy gradient loss. Note a few of the choices here:
    #
    # * this is a policy trainer, so:
    #     inputs are states and targets are actions + loss weights (see below)
    # * we are using CrossEntropyLoss
    #     This is because we are training a policy model, so targets are
    #     actions and they are integers -- CrossEntropyLoss will calculate
    #     the probability of each action in the state, pi(s, a).
    # * we are using has_weights=True
    #     We set has_weights = True because pi(s, a) will be multiplied by
    #     a number -- a factor that can change depending on which policy
    #     gradient algorithms you use; here, we just use the return from
    #     from this state and action, but many other variants can be tried.
    #  * we use id_to_mask=0
    #     This is because we reserved 0 for padding actions - so true actions
    #     start from 1 and we want to remove any loss on the 0 padding.
    self._trainer = supervised.Trainer(
        model=model, optimizer=optimizer, lr_schedule=lr_schedule,
        loss_fn=tl.CrossEntropyLoss, inputs=self._inputs, output_dir=output_dir,
        has_weights=True, id_to_mask=0)

  def _batches_stream(self):
    """Use the RLTask self._task to create inputs to the policy model."""
    for (obs, act, logp, rew, ret) in self._task.batches_stream(
        self._batch_size, max_slice_length=self._max_slice_length):
      del logp, rew  # We're not using log-probs or rewards here.
      # We return a triple (observation, action, discounted return) which is
      # later used by the model as (inputs, targets, loss weights).
      yield obs, act, ret

  @property
  def current_epoch(self):
    """Returns current step number in this training session."""
    return self._epoch

  def policy(self, trajectory):
    model = self._eval_model
    model.weights = self._trainer.model_weights
    pred = model(trajectory.last_state[None, ...], n_accelerators=1)
    sample = tl.gumbel_sample(pred[0, 1:])
    return sample, pred[0, sample+1]

  def run(self, n_epochs=1):
    """Runs this loop for n epochs.

    Args:
      n_epochs: Stop training after completing n steps.
    """
    for _ in range(n_epochs):
      self._epoch += 1
      self._trainer.train_epoch(self._train_steps_per_epoch, 1)
      cur_time = time.time()
      avg_return = self._task.collect_trajectories(
          self.policy, self._collect_per_epoch, self._epoch)
      print('Collecting %d episodes took %.2f seconds.'
            % (self._collect_per_epoch, time.time() - cur_time))
      print('Average return in epoch %d was %.2f.' % (self._epoch, avg_return))
