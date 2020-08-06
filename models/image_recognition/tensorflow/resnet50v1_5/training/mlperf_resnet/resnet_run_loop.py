# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
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
# ==============================================================================
"""Contains utility and supporting functions for ResNet.

  This module contains ResNet code which does not directly build layers. This
includes dataset management, hyperparameter and optimizer code, and argument
parsing. Code for defining the ResNet layers can be found in resnet_model.py.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import os

import tensorflow as tf  # pylint: disable=g-bad-import-order

from mlperf_compliance import mlperf_log
from mlperf_compliance import tf_mlperf_log
from mlperf_resnet import resnet_model
from mlperf_utils.arg_parsers import parsers
from mlperf_utils.export import export
from mlperf_utils.logs import hooks_helper
from mlperf_utils.logs import logger
from mlperf_utils.misc import model_helpers

global is_mpi
try:
    import horovod.tensorflow as hvd
    hvd.init()
    is_mpi = hvd.size()
except ImportError:
    is_mpi = 0
    print("No MPI horovod support, this is running in no-MPI mode!")


_NUM_EXAMPLES_NAME = "num_examples"
_NUM_IMAGES = {
        'train': 1281167,
        'validation': 50000
}


################################################################################
# Functions for input processing.
################################################################################
def process_record_dataset(dataset, is_training, batch_size, shuffle_buffer,
                           parse_record_fn, num_epochs=1, num_gpus=None,
                           examples_per_epoch=None, dtype=tf.float32):
  """Given a Dataset with raw records, return an iterator over the records.

  Args:
    dataset: A Dataset representing raw records
    is_training: A boolean denoting whether the input is for training.
    batch_size: The number of samples per batch.
    shuffle_buffer: The buffer size to use when shuffling records. A larger
      value results in better randomness, but smaller values reduce startup
      time and use less memory.
    parse_record_fn: A function that takes a raw record and returns the
      corresponding (image, label) pair.
    num_epochs: The number of epochs to repeat the dataset.
    num_gpus: The number of gpus used for training.
    examples_per_epoch: The number of examples in an epoch.
    dtype: Data type to use for images/features.

  Returns:
    Dataset of (image, label) pairs ready for iteration.
  """

  # We prefetch a batch at a time, This can help smooth out the time taken to
  # load input files as we go through shuffling and processing.
  dataset = dataset.prefetch(buffer_size=batch_size)
  if is_training:
    if is_mpi:
      dataset = dataset.shard(hvd.size(), hvd.rank())
    # Shuffle the records. Note that we shuffle before repeating to ensure
    # that the shuffling respects epoch boundaries.
    mlperf_log.resnet_print(key=mlperf_log.INPUT_ORDER)
    dataset = dataset.shuffle(buffer_size=shuffle_buffer)

  # If we are training over multiple epochs before evaluating, repeat the
  # dataset for the appropriate number of epochs.
  # dataset = dataset.repeat(num_epochs)
  #
  # # Parse the raw records into images and labels. Testing has shown that setting
  # # num_parallel_batches > 1 produces no improvement in throughput, since
  # # batch_size is almost always much greater than the number of CPU cores.
  # dataset = dataset.apply(
  #     tf.data.experimental.map_and_batch(
  #         lambda value: parse_record_fn(value, is_training, dtype),
  #         batch_size=batch_size,
  #         num_parallel_batches=1))

  dataset = dataset.map(lambda value: parse_record_fn(value, is_training, dtype), num_parallel_calls=tf.data.experimental.AUTOTUNE)

  # Operations between the final prefetch and the get_next call to the iterator
  # will happen synchronously during run time. We prefetch here again to
  # background all of the above processing work and keep it out of the
  # critical training path. Setting buffer_size to tf.contrib.data.AUTOTUNE
  # allows DistributionStrategies to adjust how many batches to fetch based
  # on how many devices are present.
  dataset = dataset.prefetch(buffer_size=tf.data.experimental.AUTOTUNE)

  return dataset


def get_synth_input_fn(height, width, num_channels, num_classes):
  """Returns an input function that returns a dataset with zeroes.

  This is useful in debugging input pipeline performance, as it removes all
  elements of file reading and image preprocessing.

  Args:
    height: Integer height that will be used to create a fake image tensor.
    width: Integer width that will be used to create a fake image tensor.
    num_channels: Integer depth that will be used to create a fake image tensor.
    num_classes: Number of classes that should be represented in the fake labels
      tensor

  Returns:
    An input_fn that can be used in place of a real one to return a dataset
    that can be used for iteration.
  """
  def input_fn(is_training, data_dir, batch_size, *args, **kwargs):  # pylint: disable=unused-argument
    images = tf.zeros((batch_size * 10, height, width, num_channels), tf.float32)
    labels = tf.zeros((batch_size * 10,), tf.int32)
    return tf.data.Dataset.from_tensor_slices((images, labels))

  return input_fn


################################################################################
# Functions for running training/eval/validation loops for the model.
################################################################################
def learning_rate_with_decay(
    batch_size, batch_denom, num_images, boundary_epochs, decay_rates,
    base_lr=0.1, enable_lars=False):
  """Get a learning rate that decays step-wise as training progresses.

  Args:
    batch_size: the number of examples processed in each training batch.
    batch_denom: this value will be used to scale the base learning rate.
      `0.1 * batch size` is divided by this number, such that when
      batch_denom == batch_size, the initial learning rate will be 0.1.
    num_images: total number of images that will be used for training.
    boundary_epochs: list of ints representing the epochs at which we
      decay the learning rate.
    decay_rates: list of floats representing the decay rates to be used
      for scaling the learning rate. It should have one more element
      than `boundary_epochs`, and all elements should have the same type.
    base_lr: Initial learning rate scaled based on batch_denom.

  Returns:
    Returns a function that takes a single argument - the number of batches
    trained so far (global_step)- and returns the learning rate to be used
    for training the next batch.
  """
  initial_learning_rate = base_lr * batch_size / batch_denom
  batches_per_epoch = num_images / batch_size

  # Multiply the learning rate by 0.1 at 100, 150, and 200 epochs.
  boundaries = [int(batches_per_epoch * epoch) for epoch in boundary_epochs]
  vals = [initial_learning_rate * decay for decay in decay_rates]

  def learning_rate_fn(global_step):
    lr = tf.compat.v1.train.piecewise_constant(global_step, boundaries, vals)
    warmup_steps = int(batches_per_epoch * 5)
    warmup_lr = (
        initial_learning_rate * tf.cast(global_step, tf.float32) / tf.cast(
        warmup_steps, tf.float32))
    return tf.cond(pred=global_step < warmup_steps, true_fn=lambda: warmup_lr, false_fn=lambda: lr)

  def poly_rate_fn(global_step):
    """Handles linear scaling rule, gradual warmup, and LR decay.

    The learning rate starts at 0, then it increases linearly per step.  After
    flags.poly_warmup_epochs, we reach the base learning rate (scaled to account
    for batch size). The learning rate is then decayed using a polynomial rate
    decay schedule with power 2.0.

    Args:
    global_step: the current global_step

    Returns:
    returns the current learning rate
    """

    # Learning rate schedule for LARS polynomial schedule
    if batch_size <= 4096:
      plr = 5.0
      w_epochs = 5
    elif batch_size <= 8192:
      plr = 10.0
      w_epochs = 5
    elif batch_size <= 16384:
      plr = 25.0
      w_epochs = 5
    else: # e.g. 32768
      plr = 33.0
      w_epochs = 25

    w_steps = int(w_epochs * batches_per_epoch)
    wrate = (plr * tf.cast(global_step, tf.float32) / tf.cast(
        w_steps, tf.float32))

    num_epochs = flags.train_epochs
    train_steps = batches_per_epoch * num_epochs

    min_step = tf.constant(1, dtype=tf.int64)
    decay_steps = tf.maximum(min_step, tf.subtract(global_step, w_steps))
    poly_rate = tf.compat.v1.train.polynomial_decay(
        plr,
        decay_steps,
        train_steps - w_steps + 1,
        power=2.0)
    return tf.compat.v1.where(global_step <= w_steps, wrate, poly_rate)

  # For LARS we have a new learning rate schedule
  if enable_lars:
    return poly_rate_fn

  return learning_rate_fn


def resnet_model_fn(features, labels, mode, model_class,
                    resnet_size, weight_decay, learning_rate_fn, momentum,
                    data_format, version, loss_scale, loss_filter_fn=None,
                    dtype=resnet_model.DEFAULT_DTYPE,
                    label_smoothing=0.0, enable_lars=False,
                    use_bfloat16=False):
  """Shared functionality for different resnet model_fns.

  Initializes the ResnetModel representing the model layers
  and uses that model to build the necessary EstimatorSpecs for
  the `mode` in question. For training, this means building losses,
  the optimizer, and the train op that get passed into the EstimatorSpec.
  For evaluation and prediction, the EstimatorSpec is returned without
  a train op, but with the necessary parameters for the given mode.

  Args:
    features: tensor representing input images
    labels: tensor representing class labels for all input images
    mode: current estimator mode; should be one of
      `tf.estimator.ModeKeys.TRAIN`, `EVALUATE`, `PREDICT`
    model_class: a class representing a TensorFlow model that has a __call__
      function. We assume here that this is a subclass of ResnetModel.
    resnet_size: A single integer for the size of the ResNet model.
    weight_decay: weight decay loss rate used to regularize learned variables.
    learning_rate_fn: function that returns the current learning rate given
      the current global_step
    momentum: momentum term used for optimization
    data_format: Input format ('channels_last', 'channels_first', or None).
      If set to None, the format is dependent on whether a GPU is available.
    version: Integer representing which version of the ResNet network to use.
      See README for details. Valid values: [1, 2]
    loss_scale: The factor to scale the loss for numerical stability. A detailed
      summary is present in the arg parser help text.
    loss_filter_fn: function that takes a string variable name and returns
      True if the var should be included in loss calculation, and False
      otherwise. If None, batch_normalization variables will be excluded
      from the loss.
    dtype: the TensorFlow dtype to use for calculations.
    use_bfloat16: Whether to use bfloat16 type for calculations.

  Returns:
    EstimatorSpec parameterized according to the input params and the
    current mode.
  """

  # Generate a summary node for the images
  tf.compat.v1.summary.image('images', features, max_outputs=6)

  # Checks that features/images have same data type being used for calculations.
  assert features.dtype == dtype

  if use_bfloat16 == True:
    dtype = tf.bfloat16

  features = tf.cast(features, dtype)

  model = model_class(resnet_size, data_format, version=version, dtype=dtype)

  logits = model(features, mode == tf.estimator.ModeKeys.TRAIN)

  # This acts as a no-op if the logits are already in fp32 (provided logits are
  # not a SparseTensor). If dtype is is low precision, logits must be cast to
  # fp32 for numerical stability.
  logits = tf.cast(logits, tf.float32)

  logits = tf.reshape(logits, shape=(-1, 1001))

  num_examples_metric = tf_mlperf_log.sum_metric(tensor=tf.shape(input=logits)[0], name=_NUM_EXAMPLES_NAME)
  from tensorflow_estimator.python.estimator.canned import prediction_keys
  predictions = {
      'classes': tf.argmax(input=logits, axis=1),
      'probabilities': tf.nn.softmax(logits, name='softmax_tensor'),
      prediction_keys.PredictionKeys.LOGITS: logits
  }


  if mode == tf.estimator.ModeKeys.PREDICT:
    # Return the predictions and the specification for serving a SavedModel
    return tf.estimator.EstimatorSpec(
        mode=mode,
        predictions=predictions,
        export_outputs={
            'predict': tf.estimator.export.PredictOutput(predictions)
        })

  # Calculate loss, which includes softmax cross entropy and L2 regularization.
  mlperf_log.resnet_print(key=mlperf_log.MODEL_HP_LOSS_FN, value=mlperf_log.CCE)

  if label_smoothing != 0.0:
    one_hot_labels = tf.one_hot(labels, 1001)
    cross_entropy = tf.compat.v1.losses.softmax_cross_entropy(
        logits=logits, onehot_labels=one_hot_labels,
        label_smoothing=label_smoothing)
  else:
    cross_entropy = tf.compat.v1.losses.sparse_softmax_cross_entropy(
        logits=logits, labels=labels)

  # Create a tensor named cross_entropy for logging purposes.
  tf.identity(cross_entropy, name='cross_entropy')
  tf.compat.v1.summary.scalar('cross_entropy', cross_entropy)

  # If no loss_filter_fn is passed, assume we want the default behavior,
  # which is that batch_normalization variables are excluded from loss.
  def exclude_batch_norm(name):
    return 'batch_normalization' not in name
  loss_filter_fn = loss_filter_fn or exclude_batch_norm

  mlperf_log.resnet_print(key=mlperf_log.MODEL_EXCLUDE_BN_FROM_L2,
                          value=not loss_filter_fn('batch_normalization'))

  # Add weight decay to the loss.
  mlperf_log.resnet_print(key=mlperf_log.MODEL_L2_REGULARIZATION,
                          value=weight_decay)
  l2_loss = weight_decay * tf.add_n(
      # loss is computed using fp32 for numerical stability.
      [tf.nn.l2_loss(tf.cast(v, tf.float32)) for v in tf.compat.v1.trainable_variables()
       if loss_filter_fn(v.name)])
  tf.compat.v1.summary.scalar('l2_loss', l2_loss)
  loss = cross_entropy + l2_loss

  if mode == tf.estimator.ModeKeys.TRAIN:
    global_step = tf.compat.v1.train.get_or_create_global_step()

    learning_rate = learning_rate_fn(global_step)

    log_id = mlperf_log.resnet_print(key=mlperf_log.OPT_LR, deferred=True)
    learning_rate = tf_mlperf_log.log_deferred(op=learning_rate, log_id=log_id,
                                               every_n=100)

    # Create a tensor named learning_rate for logging purposes
    tf.identity(learning_rate, name='learning_rate')
    tf.compat.v1.summary.scalar('learning_rate', learning_rate)

    mlperf_log.resnet_print(key=mlperf_log.OPT_NAME,
                            value=mlperf_log.SGD_WITH_MOMENTUM)
    mlperf_log.resnet_print(key=mlperf_log.OPT_MOMENTUM, value=momentum)

    if enable_lars:
      optimizer = tf.contrib.opt.LARSOptimizer(
          learning_rate,
          momentum=momentum,
          weight_decay=weight_decay,
          skip_list=['batch_normalization', 'bias'])
    else:
      optimizer = tf.compat.v1.train.MomentumOptimizer(
          learning_rate=learning_rate,
          momentum=momentum
      )

    from zoo.tfpark.zoo_optimizer import ZooOptimizer
    optimizer = ZooOptimizer(optimizer)
    if is_mpi:
      optimizer = hvd.DistributedOptimizer(optimizer)

    if loss_scale != 1:
      # When computing fp16 gradients, often intermediate tensor values are
      # so small, they underflow to 0. To avoid this, we multiply the loss by
      # loss_scale to make these tensor values loss_scale times bigger.
      scaled_grad_vars = optimizer.compute_gradients(loss * loss_scale)

      # Once the gradient computation is complete we can scale the gradients
      # back to the correct scale before passing them to the optimizer.
      unscaled_grad_vars = [(grad / loss_scale, var)
                            for grad, var in scaled_grad_vars]
      minimize_op = optimizer.apply_gradients(unscaled_grad_vars, global_step)
    else:
      minimize_op = optimizer.minimize(loss, global_step)

    update_ops = tf.compat.v1.get_collection(tf.compat.v1.GraphKeys.UPDATE_OPS)
    # UPDATE_OPS collection will be called automatically by TFPark
    # train_op = tf.group(minimize_op, update_ops, num_examples_metric[1])
    train_op = minimize_op
  else:
    train_op = None

  accuracy = tf.compat.v1.metrics.accuracy(labels, predictions['classes'])
  accuracy_top_5 = tf.compat.v1.metrics.mean(tf.nn.in_top_k(predictions=logits,
                                                  targets=labels,
                                                  k=5,
                                                  name='top_5_op'))

  metrics = {'accuracy': accuracy,
             'accuracy_top_5': accuracy_top_5,
             _NUM_EXAMPLES_NAME: num_examples_metric}

  # Create a tensor named train_accuracy for logging purposes
  tf.identity(accuracy[1], name='train_accuracy')
  tf.identity(accuracy_top_5[1], name='train_accuracy_top_5')
  tf.compat.v1.summary.scalar('train_accuracy', accuracy[1])
  tf.compat.v1.summary.scalar('train_accuracy_top_5', accuracy_top_5[1])

  return tf.estimator.EstimatorSpec(
      mode=mode,
      predictions=predictions,
      loss=loss,
      train_op=train_op,
      eval_metric_ops=metrics)


def per_device_batch_size(batch_size, num_gpus):
  """For multi-gpu, batch-size must be a multiple of the number of GPUs.

  Note that this should eventually be handled by DistributionStrategies
  directly. Multi-GPU support is currently experimental, however,
  so doing the work here until that feature is in place.

  Args:
    batch_size: Global batch size to be divided among devices. This should be
      equal to num_gpus times the single-GPU batch_size for multi-gpu training.
    num_gpus: How many GPUs are used with DistributionStrategies.

  Returns:
    Batch size per device.

  Raises:
    ValueError: if batch_size is not divisible by number of devices
  """
  if num_gpus <= 1:
    return batch_size

  remainder = batch_size % num_gpus
  if remainder:
    err = ('When running with multiple GPUs, batch size '
           'must be a multiple of the number of available GPUs. Found {} '
           'GPUs with a batch size of {}; try --batch_size={} instead.'
          ).format(num_gpus, batch_size, batch_size - remainder)
    raise ValueError(err)
  return int(batch_size / num_gpus)


def resnet_main(seed, flags, model_function, input_function, shape=None):
  """Shared main loop for ResNet Models.

  Args:
    flags: FLAGS object that contains the params for running. See
      ResnetArgParser for created flags.
    model_function: the function that instantiates the Model and builds the
      ops for train/eval. This will be passed directly into the estimator.
    input_function: the function that processes the dataset and returns a
      dataset that the estimator can train on. This will be wrapped with
      all the relevant flags for running and passed to estimator.
    shape: list of ints representing the shape of the images used for training.
      This is only used if flags.export_dir is passed.
  """

  mlperf_log.resnet_print(key=mlperf_log.RUN_START)

  # Using the Winograd non-fused algorithms provides a small performance boost.
  os.environ['TF_ENABLE_WINOGRAD_NONFUSED'] = '1'

  # Create session config based on values of inter_op_parallelism_threads and
  # intra_op_parallelism_threads. Note that we default to having
  # allow_soft_placement = True, which is required for multi-GPU and not
  # harmful for other modes.
  session_config = tf.compat.v1.ConfigProto(
      inter_op_parallelism_threads=flags.inter_op_parallelism_threads,
      intra_op_parallelism_threads=flags.intra_op_parallelism_threads,
      allow_soft_placement=True)

  mlperf_log.resnet_print(key=mlperf_log.RUN_SET_RANDOM_SEED, value=seed)
  run_config = tf.estimator.RunConfig(session_config=session_config,
                                      log_step_count_steps=10, # output logs more frequently
                                      tf_random_seed=seed)


  model_dir = flags.model_dir
  # benchmark_log_dir = flags.benchmark_log_dir

  classifier = tf.estimator.Estimator(
      model_fn=model_function, model_dir=model_dir, config=run_config,
      params={
          'resnet_size': flags.resnet_size,
          'data_format': flags.data_format,
          'batch_size': flags.batch_size,
          'version': flags.version,
          'loss_scale': flags.loss_scale,
          'dtype': flags.dtype,
          'label_smoothing': flags.label_smoothing,
          'enable_lars': flags.enable_lars,
          'weight_decay': flags.weight_decay,
          'fine_tune': flags.fine_tune,
          'use_bfloat16': flags.use_bfloat16
      })

  print('Starting a training cycle.')
  from zoo import init_nncontext
  sc = init_nncontext()

  from zoo import get_node_and_core_number

  node_num, core_num = get_node_and_core_number()
  num_workers = node_num

  def input_fn_train():
    dataset = input_function(
          is_training=True,
          batch_size=flags.batch_size, # this takes no effect
          data_dir=flags.data_dir,
          dtype=flags.dtype
      )
    # dataset = dataset.take(20000)
    from zoo.tfpark import TFDataset
    dataset = TFDataset.from_tf_data_dataset(dataset, batch_size=flags.batch_size * num_workers)
    return dataset

  def input_fn_eval():
    dataset = input_function(
        is_training=False,
        data_dir=flags.data_dir,
        batch_size=per_device_batch_size(flags.batch_size, flags.num_gpus),
        num_epochs=1,
        dtype=flags.dtype
    )
    # dataset = dataset.take(400)
    from zoo.tfpark import TFDataset
    dataset = TFDataset.from_tf_data_dataset(dataset, batch_per_thread=flags.batch_size // core_num)
    return dataset

  from zoo import init_nncontext
  sc = init_nncontext()

  from zoo.tfpark.estimator import TFEstimator

  estimator = TFEstimator(classifier)
  steps_per_epoch = _NUM_IMAGES['train'] // flags.batch_size

  for i in range(flags.train_epochs // flags.epochs_between_evals):

    print("Starting to train epoch {} to {}".format(i * flags.epochs_between_evals + 1, (i + 1) * flags.epochs_between_evals))
    estimator.train(input_fn_train, steps=steps_per_epoch * flags.epochs_between_evals)# , session_config=tf.ConfigProto(inter_op_parallelism_threads=1, intra_op_parallelism_threads=6))
    print('Starting to evaluate.')
    eval_results = estimator.evaluate(input_fn=input_fn_eval, eval_methods=["acc", "top5acc"])
    print(eval_results)

class ResnetArgParser(argparse.ArgumentParser):
  """Arguments for configuring and running a Resnet Model."""

  def __init__(self, resnet_size_choices=None):
    super(ResnetArgParser, self).__init__(parents=[
        parsers.BaseParser(multi_gpu=False),
        parsers.PerformanceParser(num_parallel_calls=False),
        parsers.ImageModelParser(),
        parsers.ExportParser(),
        parsers.BenchmarkParser(),
    ])

    self.add_argument(
        '--version', '-v', type=int, choices=[1, 2],
        default=resnet_model.DEFAULT_VERSION,
        help='Version of ResNet. (1 or 2) See README.md for details.'
    )

    self.add_argument(
        '--resnet_size', '-rs', type=int, default=50,
        choices=resnet_size_choices,
        help='[default: %(default)s] The size of the ResNet model to use.',
        metavar='<RS>' if resnet_size_choices is None else None
    )

    self.add_argument(
        '--use_bfloat16', action='store_true', default=False,
        help='Whether to use bfloat16 type for computations.'
    )

  def parse_args(self, args=None, namespace=None):
    args = super(ResnetArgParser, self).parse_args(
        args=args, namespace=namespace)

    # handle coupling between dtype and loss_scale
    parsers.parse_dtype_info(args)

    return args
