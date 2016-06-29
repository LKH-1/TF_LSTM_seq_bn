# Customized LSTM Cell

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import math
import numpy as np
import tensorflow as tf

from tensorflow.python.framework import ops
from tensorflow.python.framework import tensor_shape
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import clip_ops
from tensorflow.python.ops import embedding_ops
from tensorflow.python.ops import init_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import nn_ops
from tensorflow.python.ops import variable_scope as vs

from tensorflow.python.ops.math_ops import sigmoid
from tensorflow.python.ops.math_ops import tanh

from tensorflow.python.ops.init_ops import constant_initializer

from tensorflow.python.platform import tf_logging as logging

from tensorflow.python.ops.rnn_cell import RNNCell
from tensorflow.python.ops.rnn_cell import LSTMStateTuple
from tensorflow.python.ops.rnn_cell import _get_concat_variable
from tensorflow.python.ops.rnn_cell import _get_sharded_variable

from tensorflow.python.ops.nn import batch_normalization, moments
from tensorflow.python.training.moving_averages import ExponentialMovingAverage

from tensorflow.python.ops.control_flow_ops import cond

_LSTMStateTuple = collections.namedtuple("LSTMStateTuple", ("c", "h"))

def batch_norm(x, deterministic, alpha=0.9, shift=True, scope='bn'):
    with vs.variable_scope(scope):
        dtype = x.dtype
        input_shape = x.get_shape().as_list()
        feat_dim = input_shape[-1]
        axes = range(len(input_shape)-1)
        
        if shift:
            beta = vs.get_variable(
                    scope+"_beta", shape=[feat_dim],
                    initializer=init_ops.zeros_initializer, dtype=dtype)
        else:
            beta = vs.get_variable(
                scope+"_beta", shape=[feat_dim],
                initializer=init_ops.zeros_initializer, 
                dtype=dtype, trainable=False)
        
        gamma = vs.get_variable(
                    scope+"_gamma", shape=[feat_dim],
                    initializer=init_ops.constant_initializer(0.1), dtype=dtype)
        
        mean = vs.get_variable(scope+"_mean", shape=[feat_dim],
                                       initializer=init_ops.zeros_initializer,
                                       dtype=dtype, trainable=False)
        
        var = vs.get_variable(scope+"_var", shape=[feat_dim],
                                          initializer=init_ops.ones_initializer,
                                          dtype=dtype, trainable=False)
        
        counter = vs.get_variable(scope+"_counter", shape=[],
                                          initializer=init_ops.constant_initializer(0),
                                          dtype=tf.int64, trainable=False)
        
        zero_cnt = vs.get_variable(scope+"_zero_cnt", shape=[],
                                          initializer=init_ops.constant_initializer(0),
                                          dtype=tf.int64, trainable=False)
        
        batch_mean, batch_var = moments(x, axes, name=scope+'_moments')
        
        mean, var = cond(math_ops.equal(counter, zero_cnt), lambda: (batch_mean, batch_var), 
                         lambda: (mean, var))
        
         
        mean, var, counter = cond(deterministic, lambda: (mean, var, counter), 
                                 lambda: ((1-alpha) * batch_mean + alpha * mean, 
                                         (1-alpha) * batch_var + alpha * var, 
                                         counter + 1))
        normed = batch_normalization(x, mean, var, beta, gamma, 1e-8)
    return normed

class LSTMCell(RNNCell):
    def __init__(self, num_units, use_peepholes=False, 
                 cell_clip=None, initializer=None, 
                 num_proj=None, num_unit_shards=1, 
                 num_proj_shards=1, forget_bias=1.0, 
                 bn=False, return_gate=False,
                 deterministic=None, activation=tanh):
        """
        Initialize the parameters for an LSTM cell.

        Args:
          num_units: int, The number of units in the LSTM cell
          use_peepholes: bool, set True to enable diagonal/peephole connections.
          cell_clip: (optional) A float value, if provided the cell state is clipped
            by this value prior to the cell output activation.
          initializer: (optional) The initializer to use for the weight and
            projection matrices.
          num_proj: (optional) int, The output dimensionality for the projection
            matrices.  If None, no projection is performed.
          num_unit_shards: How to split the weight matrix.  If >1, the weight
            matrix is stored across num_unit_shards.
          num_proj_shards: How to split the projection matrix.  If >1, the
            projection matrix is stored across num_proj_shards.
          forget_bias: Biases of the forget gate are initialized by default to 1
            in order to reduce the scale of forgetting at the beginning of
            the training.
          return_gate: bool, set true to return the values of the gates.
          bn: bool, set True to enable sequence-wise batch normalization. Implemented
            according to arXiv:1603.09025
          deterministic: Tensor, control training and testing phase, decide whether to
            open batch normalization.
          activation: Activation function of the inner states.
        """
    
        self._num_units = num_units
        self._use_peepholes = use_peepholes
        self._cell_clip = cell_clip
        self._initializer = initializer
        self._num_proj = num_proj
        self._num_unit_shards = num_unit_shards
        self._num_proj_shards = num_proj_shards
        self._forget_bias = forget_bias
        self._activation = activation
        self._bn = bn
        self._return_gate = return_gate
        self._deterministic = deterministic
        self._return_gate = return_gate

        if num_proj:
            self._state_size = LSTMStateTuple(num_units, num_proj)
            self._output_size = num_proj
        else:
            self._state_size = LSTMStateTuple(num_units, num_units)
            self._output_size = num_units
    
    @property
    def state_size(self):
        return self._state_size

    @property
    def output_size(self):
        return self._output_size

    def __call__(self, inputs, state, scope=None):
        """Run one step of LSTM.

        Args:
          inputs: input Tensor, 2D, batch x num_units.
          state: if `state_is_tuple` is False, this must be a state Tensor,
            `2-D, batch x state_size`.  If `state_is_tuple` is True, this must be a
            tuple of state Tensors, both `2-D`, with column sizes `c_state` and
            `m_state`.
          scope: VariableScope for the created subgraph; defaults to "LSTMCell".

        Returns:
          A tuple containing:
          - A `2-D, [batch x output_dim]`, Tensor representing the output of the
            LSTM after reading `inputs` when previous state was `state`.
            Here output_dim is:
               num_proj if num_proj was set,
               num_units otherwise.
          - Tensor(s) representing the new state of LSTM after reading `inputs` when
            the previous state was `state`.  Same type and shape(s) as `state`.

        Raises:
          ValueError: If input size cannot be inferred from inputs via
            static shape inference.
        """
        num_proj = self._num_units if self._num_proj is None else self._num_proj

        (c_prev, m_prev) = state

        dtype = inputs.dtype
        input_size = inputs.get_shape().with_rank(2)[1]
        if input_size.value is None:
            raise ValueError("Could not infer input size from inputs.get_shape()[-1]")
        
        scope_name = scope or type(self).__name__
        with vs.variable_scope(scope_name, 
                               initializer=self._initializer):  # "LSTMCell"
            if self._bn:
                concat_w_i = _get_concat_variable(
                    "W_i", [input_size.value, 4 * self._num_units],
                    dtype, self._num_unit_shards)
                
                concat_w_r = _get_concat_variable(
                    "W_r", [num_proj, 4 * self._num_units],
                    dtype, self._num_unit_shards)

                b = vs.get_variable(
                    "B", shape=[4 * self._num_units],
                    initializer=array_ops.zeros_initializer, dtype=dtype)
            else:
                concat_w = _get_concat_variable(
                    "W", [input_size.value + num_proj, 4 * self._num_units],
                    dtype, self._num_unit_shards)

                b = vs.get_variable(
                    "B", shape=[4 * self._num_units],
                    initializer=array_ops.zeros_initializer, dtype=dtype)

            # i = input_gate, j = new_input, f = forget_gate, o = output_gate
            if self._bn:
                lstm_matrix_i = batch_norm(math_ops.matmul(inputs, concat_w_i), self._deterministic,
                                           shift=False, scope=scope_name+'bn_i')
                lstm_matrix_r = batch_norm(math_ops.matmul(m_prev, concat_w_r), self._deterministic,
                                           shift=False, scope=scope_name+'bn_r')
                lstm_matrix = nn_ops.bias_add(math_ops.add(lstm_matrix_i, lstm_matrix_r), b)

            else:
                cell_inputs = array_ops.concat(1, [inputs, m_prev])
                lstm_matrix = nn_ops.bias_add(math_ops.matmul(cell_inputs, concat_w), b)
            
            i, j, f, o = array_ops.split(1, 4, lstm_matrix)

            # Diagonal connections
            if self._use_peepholes:
                w_f_diag = vs.get_variable(
                    "W_F_diag", shape=[self._num_units], dtype=dtype)
                w_i_diag = vs.get_variable(
                    "W_I_diag", shape=[self._num_units], dtype=dtype)
                w_o_diag = vs.get_variable(
                    "W_O_diag", shape=[self._num_units], dtype=dtype)

            if self._use_peepholes:
                c = (sigmoid(f + self._forget_bias + w_f_diag * c_prev) * c_prev +
                 sigmoid(i + w_i_diag * c_prev) * self._activation(j))
            else:
                c = (sigmoid(f + self._forget_bias) * c_prev + sigmoid(i) *
                 self._activation(j))

            if self._cell_clip is not None:
                # pylint: disable=invalid-unary-operand-type
                c = clip_ops.clip_by_value(c, -self._cell_clip, self._cell_clip)
                # pylint: enable=invalid-unary-operand-type

            if self._use_peepholes:
                if self._bn:
                    m = sigmoid(o + w_o_diag * c) * self._activation(batch_norm(c, self._deterministic,
                                                                                scope=scope_name+'bn_m'))
                else:
                    m = sigmoid(o + w_o_diag * c) * self._activation(c)
            else:
                if self._bn:
                    m = sigmoid(o) * self._activation(batch_norm(c, self._deterministic,
                                                                 scope=scope_name+'bn_m'))
                else:
                    m = sigmoid(o) * self._activation(c)

            if self._num_proj is not None:
                concat_w_proj = _get_concat_variable(
                    "W_P", [self._num_units, self._num_proj],
                    dtype, self._num_proj_shards)

                m = math_ops.matmul(m, concat_w_proj)

        new_state = LSTMStateTuple(c, m)
        
        if not self._return_gate:
            return m, new_state
        else:
            return m, new_state, (i, j, f, o)
