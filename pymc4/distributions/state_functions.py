import tensorflow as tf
import tensorflow_probability as tfp
from tensorflow_probability.python.mcmc.internal import util as mcmc_util

tfd = tfp.distributions

__all__ = ["categorical_uniform_fn", "bernoulli_fn", "gaussian_round_fn", "poisson_fn"]


def categorical_uniform_fn(event_shape, scale=1.0, name=None):
    """Returns a callable that samples new proposal from Categorical distribution with uniform probabilites
    Args:
       scale: a `Tensor` or Python `list` of `Tensor`s of any shapes and `dtypes`
         controlling the upper and lower bound of the uniform proposal
         distribution.
       name: Python `str` name prefixed to Ops created by this function.
           Default value: 'categorical_uniform_fn'.
    Returns:
        categorical_uniform_fn: A callable accepting a Python `list` of `Tensor`s
         representing the state parts of the `current_state` and an `int`
         representing the random seed used to generate the proposal. The callable
         returns the same-type `list` of `Tensor`s as the input and represents the
         proposal for the RWM algorithm.
    """

    def _fn(state_parts, seed):
        with tf.name_scope(name or "categorical_uniform_fn"):
            scales = scale if mcmc_util.is_list_like(scale) else [scale]
            if len(scales) == 1:
                scales *= len(state_parts)
            if len(state_parts) != len(scales):
                raise ValueError("`scale` must broadcast with `state_parts`")
            deltas = [
                tfd.Categorical(probs=[0.5] * event_shape).sample(
                    seed=seed, sample_shape=tf.shape(state_part)
                )
                for scale_part, state_part in zip(scales, state_parts)
            ]
            return deltas

    return _fn


def bernoulli_fn(scale=1.0, name=None):
    """Returns a callable that samples new proposal from Bernoulli distribution
    Args:
       scale: a `Tensor` or Python `list` of `Tensor`s of any shapes and `dtypes`
         controlling the upper and lower bound of the uniform proposal
         distribution.
       name: Python `str` name prefixed to Ops created by this function.
           Default value: 'bernoulli_fn'.
    Returns:
        bernoulli_fn: A callable accepting a Python `list` of `Tensor`s
         representing the state parts of the `current_state` and an `int`
         representing the random seed used to generate the proposal. The callable
         returns the same-type `list` of `Tensor`s as the input and represents the
         proposal for the RWM algorithm.
    """

    def _fn(state_parts, seed):
        with tf.name_scope(name or "bernoulli_fn"):
            scales = scale if mcmc_util.is_list_like(scale) else [scale]
            if len(scales) == 1:
                scales *= len(state_parts)
            if len(state_parts) != len(scales):
                raise ValueError("`scale` must broadcast with `state_parts`")

            def generate_new_values(state_part, scale_part):
                delta = tfd.Bernoulli(probs=0.5 * scale_part).sample(
                    seed=seed, sample_shape=(tf.shape(state_part))
                )
                state_part += delta
                state_part = state_part % 2
                return state_part

            deltas = [
                generate_new_values(state_part, scale_part)
                for scale_part, state_part in zip(scales, state_parts)
            ]
            return deltas

    return _fn


def gaussian_round_fn(scale=1.0, name=None):
    """Returns a callable that samples new proposal from Normal distribution with round
    Args:
       scale: a `Tensor` or Python `list` of `Tensor`s of any shapes and `dtypes`
         controlling the upper and lower bound of the uniform proposal
         distribution.
       name: Python `str` name prefixed to Ops created by this function.
           Default value: 'gaussian_round_fn'.
    Returns:
        gaussian_round_fn: A callable accepting a Python `list` of `Tensor`s
         representing the state parts of the `current_state` and an `int`
         representing the random seed used to generate the proposal. The callable
         returns the same-type `list` of `Tensor`s as the input and represents the
         proposal for the RWM algorithm.
    """

    def _fn(state_parts, seed):
        with tf.name_scope(name or "bernoulli_uniform_fn"):
            scales = scale if mcmc_util.is_list_like(scale) else [scale]
            if len(scales) == 1:
                scales *= len(state_parts)
            if len(state_parts) != len(scales):
                raise ValueError("`scale` must broadcast with `state_parts`")

            def generate_new_values(state_part, scale_part):
                delta = tfd.Normal(0.0, scale_part * 1.0).sample(
                    seed=seed, sample_shape=(tf.shape(state_part))
                )
                state_part += delta
                return tf.round(state_part)

            deltas = [
                generate_new_values(state_part, scale_part)
                for scale_part, state_part in zip(scales, state_parts)
            ]
            return deltas

    return _fn

def poisson_fn(scale=1.0, name=None):
    """Returns a callable that samples new proposal from Poisson
    Args:
       scale: a `Tensor` or Python `list` of `Tensor`s of any shapes and `dtypes`
         controlling the upper and lower bound of the uniform proposal
         distribution.
       name: Python `str` name prefixed to Ops created by this function.
           Default value: 'poisson_fn'.
    Returns:
        poisson_fn: A callable accepting a Python `list` of `Tensor`s
         representing the state parts of the `current_state` and an `int`
         representing the random seed used to generate the proposal. The callable
         returns the same-type `list` of `Tensor`s as the input and represents the
         proposal for the RWM algorithm.
    """

    def _fn(state_parts, seed):
        with tf.name_scope(name or "bernoulli_uniform_fn"):
            scales = scale if mcmc_util.is_list_like(scale) else [scale]
            if len(scales) == 1:
                scales *= len(state_parts)
            if len(state_parts) != len(scales):
                raise ValueError("`scale` must broadcast with `state_parts`")

            def generate_new_values(state_part, scale_part):
                delta = tfd.Poisson(scale_part * 1.0).sample(
                    seed=seed, sample_shape=(tf.shape(state_part))
                )
                return delta

            deltas = [
                generate_new_values(state_part, scale_part)
                for scale_part, state_part in zip(scales, state_parts)
            ]
            return deltas

    return _fn
