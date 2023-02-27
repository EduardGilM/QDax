# Copyright 2022 The Flax Authors.
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

"""seq2seq example: Mode code."""

# See issue #620.
# pytype: disable=wrong-keyword-args

import functools
from typing import Any, Tuple

import jax
import jax.numpy as jnp
import numpy as np
from flax import linen as nn

Array = Any
PRNGKey = Any


class EncoderLSTM(nn.Module):
    """EncoderLSTM Module wrapped in a lifted scan transform."""

    @functools.partial(
        nn.scan,
        variable_broadcast="params",
        in_axes=1,
        out_axes=1,
        split_rngs={"params": False},
    )
    @nn.compact
    def __call__(
        self, carry: Tuple[Array, Array], x: Array
    ) -> Tuple[Tuple[Array, Array], Array]:
        """Applies the module."""
        lstm_state, is_eos = carry
        new_lstm_state, y = nn.LSTMCell()(lstm_state, x)

        def select_carried_state(new_state, old_state):
            return jnp.where(is_eos[:, np.newaxis], old_state, new_state)

        # LSTM state is a tuple (c, h).
        carried_lstm_state = tuple(
            select_carried_state(*s) for s in zip(new_lstm_state, lstm_state)
        )
        # Update `is_eos`.
        # is_eos = jnp.logical_or(is_eos, x[:, 8])
        return (carried_lstm_state, is_eos), y

    @staticmethod
    def initialize_carry(batch_size: int, hidden_size: int):
        # Use a dummy key since the default state init fn is just zeros.
        return nn.LSTMCell.initialize_carry(
            jax.random.PRNGKey(0), (batch_size,), hidden_size
        )


class Encoder(nn.Module):
    """LSTM encoder, returning state after finding the EOS token in the input."""

    hidden_size: int

    @nn.compact
    def __call__(self, inputs: Array):
        # inputs.shape = (batch_size, seq_length, vocab_size).
        batch_size = inputs.shape[0]
        lstm = EncoderLSTM(name="encoder_lstm")
        init_lstm_state = lstm.initialize_carry(batch_size, self.hidden_size)
        # We use the `is_eos` array to determine whether the encoder should carry
        # over the last lstm state, or apply the LSTM cell on the previous state.
        init_is_eos = jnp.zeros(batch_size, dtype=bool)
        init_carry = (init_lstm_state, init_is_eos)
        (final_state, _), _ = lstm(init_carry, inputs)
        return final_state


class DecoderLSTM(nn.Module):
    """DecoderLSTM Module wrapped in a lifted scan transform.

    Attributes:
      teacher_force: See docstring on Seq2seq module.
      obs_size: Size of the observations.
    """

    teacher_force: bool
    obs_size: int

    @functools.partial(
        nn.scan,
        variable_broadcast="params",
        in_axes=1,
        out_axes=1,
        split_rngs={"params": False, "lstm": True},
    )
    @nn.compact
    def __call__(self, carry: Tuple[Array, Array], x: Array) -> Array:
        """Applies the DecoderLSTM model."""
        lstm_state, last_prediction = carry
        if not self.teacher_force:
            x = last_prediction
        lstm_state, y = nn.LSTMCell()(lstm_state, x)
        logits = nn.Dense(features=self.obs_size)(y)

        return (lstm_state, logits), (logits, logits)


class Decoder(nn.Module):
    """LSTM decoder.

    Attributes:
      init_state: [batch_size, hidden_size]
        Initial state of the decoder (i.e., the final state of the encoder).
      teacher_force: See docstring on Seq2seq module.
      obs_size: Size of the observations.
    """

    teacher_force: bool
    obs_size: int

    @nn.compact
    def __call__(self, inputs: Array, init_state: Any) -> Tuple[Array, Array]:
        """Applies the decoder model.

        Args:
          inputs: [batch_size, max_output_len-1, obs_size]
            Contains the inputs to the decoder at each time step (only used when not
            using teacher forcing). Since each token at position i is fed as input
            to the decoder at position i+1, the last token is not provided.

        Returns:
          Pair (logits, predictions), which are two arrays of respectively decoded
          logits and predictions (in one hot-encoding format).
        """
        lstm = DecoderLSTM(teacher_force=self.teacher_force, obs_size=self.obs_size)
        init_carry = (init_state, inputs[:, 0])
        _, (logits, predictions) = lstm(init_carry, inputs)
        return logits, predictions


class Seq2seq(nn.Module):
    """Sequence-to-sequence class using encoder/decoder architecture.

    Attributes:
      teacher_force: whether to use `decoder_inputs` as input to the decoder at
        every step. If False, only the first input (i.e., the "=" token) is used,
        followed by samples taken from the previous output logits.
      hidden_size: int, the number of hidden dimensions in the encoder and decoder
        LSTMs.
      obs_size: the size of the observations.
      eos_id: EOS id.
    """

    teacher_force: bool
    hidden_size: int
    obs_size: int

    def setup(self):
        self.encoder = Encoder(hidden_size=self.hidden_size)
        self.decoder = Decoder(teacher_force=self.teacher_force, obs_size=self.obs_size)

    @nn.compact
    def __call__(
        self, encoder_inputs: Array, decoder_inputs: Array
    ) -> Tuple[Array, Array]:
        """Applies the seq2seq model.

        Args:
          encoder_inputs: [batch_size, max_input_length, obs_size].
            padded batch of input sequences to encode.
          decoder_inputs: [batch_size, max_output_length, obs_size].
            padded batch of expected decoded sequences for teacher forcing.
            When sampling (i.e., `teacher_force = False`), only the first token is
            input into the decoder (which is the token "="), and samples are used
            for the following inputs. The second dimension of this tensor determines
            how many steps will be decoded, regardless of the value of
            `teacher_force`.

        Returns:
          Pair (logits, predictions), which are two arrays of length `batch_size`
          containing respectively decoded logits and predictions (in one hot
          encoding format).
        """
        # Encode inputs.
        # print(encoder_inputs)
        init_decoder_state = self.encoder(encoder_inputs)
        # print(init_decoder_state)
        # Encoder(hidden_size=self.hidden_size)
        # Decode outputs.
        logits, predictions = self.decoder(decoder_inputs, init_decoder_state)
        # Decoder(
        #     init_state=init_decoder_state,
        #     teacher_force=self.teacher_force,
        #     obs_size=self.obs_size)(decoder_inputs[:, :-1],init_decoder_state)

        return logits, predictions

    def encode(self, encoder_inputs: Array):
        # Encode inputs.
        init_decoder_state = self.encoder(encoder_inputs)
        final_output, hidden_state = init_decoder_state
        return final_output
