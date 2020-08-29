from typing import Optional, Dict, Any
import tensorflow as tf
import tensorflow_probability as tfp
from pymc4.coroutine_model import Model
from pymc4 import flow
from pymc4.inference.utils import initialize_sampling_state_smc, tile_init

import tensorflow_probability
from tensorflow_probability.python.internal import vectorization_util

from tensorflow_probability.python.experimental.mcmc.sample_sequential_monte_carlo import (
    make_rwmh_kernel_fn,
)

sample_sequential_monte_carlo_chain = tfp.experimental.mcmc.sample_sequential_monte_carlo


def sample_smc(
    model: Model,
    replicas: int = 5000,
    num_chains: int = 10,
    state: Optional[flow.SamplingState] = None,
    observed: Optional[Dict[str, Any]] = None,
    xla: bool = False,
):
    """
    Perform SMC algorithm
    Parameters:
    ----------
    model : pymc4.Model
        Model to sample posterior for
    replicas : int
        Population size
    num_chains : int
        Num chains to run
    state : Optional[pymc4.flow.SamplingState]
        Alternative way to pass specify initial values and observed values
    observed : Optional[Dict[str, Any]]
        New observed values (optional)
    xla : bool
        Enable experimental XLA
    Returns
    -------
    Final state : Tensor
        Posterior samples
    """
    (logpfn_prior, logpfn_lkh, init, state_,) = _build_logp_smc(
        model,
        replicas=replicas,
        state=state,
        observed=observed,
    )
    state_keys = list(init.keys())

    # we have stores samples alongside replicas dim to avoid singularity of
    # sample points, now we need to replace the values in init state
    for _ in init.keys():
        init[_] = state_.all_unobserved_values_batched[_]
    init_state = list(init.values())

    # add chain dim
    init_state = tile_init(init_state, num_chains, 1)

    # collect unvectorized shape ranks
    core_ndims = [x.shape.ndims - 2 for x in init_state]

    # vectorize alongside both replicas and chains dim
    parallel_logpfn_prior = vectorize_logp_function(logpfn_prior, core_ndims)
    parallel_logpfn_lkh = vectorize_logp_function(logpfn_lkh, core_ndims)

    @tf.function(autograph=False)
    def run_smc(init):
        (n_stage, final_state, final_kernel_results,) = sample_sequential_monte_carlo_chain(
            parallel_logpfn_prior,
            parallel_logpfn_lkh,
            init,
            make_kernel_fn=make_rwmh_kernel_fn,
            max_num_steps=50,
        )
        return n_stage, final_state, final_kernel_results

    if xla:
        _, final_state, _ = tf.xla.experimental.compile(run_smc, inputs=[init_state])
    else:
        _, final_state, _ = run_smc(init_state)
    mapped_samples = {name: value for name, value in zip(state_keys, final_state)}
    # TODO: transform values
    return final_state, mapped_samples


def _build_logp_smc(
    model,
    replicas,
    observed: Optional[dict] = None,
    state: Optional[flow.SamplingState] = None,
):
    if not isinstance(model, Model):
        raise TypeError(
            "`sample` function only supports `pymc4.Model` objects, but you've passed `{}`".format(
                type(model)
            )
        )
    if state is not None and observed is not None:
        raise ValueError("Can't use both `state` and `observed` arguments")

    state, _, lkh_n, prior_n = initialize_sampling_state_smc(
        model, observed=observed, state=state, smc_replicas=replicas
    )

    if lkh_n == 0 or prior_n == 0:
        raise ValueError(f"Can not run SMC: the model should contain both likelihood and prior")

    if not state.all_unobserved_values:
        raise ValueError(
            f"Can not calculate a log probability: the model {model.name or ''} has no unobserved values."
        )

    unobserved_keys, unobserved_values = zip(*state.all_unobserved_values.items())

    @tf.function(autograph=False)
    def logpfn_likelihood(*values, **kwargs):
        if kwargs and values:
            raise TypeError("Either list state should be passed or a dict one")
        elif values:
            kwargs = dict(zip(unobserved_keys, values))
        st = flow.SMCSamplingState.from_values(kwargs, observed_values=observed)
        _, st = flow.evaluate_model_smc(model, state=st)
        return st.collect_log_prob_smc(is_prior=False)

    @tf.function(autograph=False)
    def logpfn_prior(*values, **kwargs):
        if kwargs and values:
            raise TypeError("Either list state should be passed or a dict one")
        elif values:
            kwargs = dict(zip(unobserved_keys, values))
        st = flow.SMCSamplingState.from_values(kwargs, observed_values=observed)
        _, st = flow.evaluate_model_smc(model, state=st)
        return st.collect_log_prob_smc(is_prior=True)

    return (
        logpfn_prior,
        logpfn_likelihood,
        dict(state.all_unobserved_values),
        state,
    )


def vectorize_logp_function(logpfn, core_ndims):
    return vectorization_util.make_rank_polymorphic(
        logpfn, core_ndims=core_ndims
    )