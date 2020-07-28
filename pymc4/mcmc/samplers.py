import abc
import inspect
import functools
from typing import Optional, List
import tensorflow as tf
from tensorflow_probability import mcmc
from pymc4.mcmc.utils import (
    initialize_sampling_state,
    trace_to_arviz,
    initialize_state,
    KERNEL_KWARGS_SET,
)

from pymc4.coroutine_model import Model
from pymc4.utils import NameParts
from pymc4 import flow
from pymc4.mcmc.tf_support import _CompoundStepTF


__all__ = ["HMC", "NUTS", "RandomWalkM", "CompoundStep"]

reg_samplers = {}


def register_sampler(cls):
    reg_samplers[cls._name] = cls
    return cls


class _BaseSampler(metaclass=abc.ABCMeta):
    _grad = False

    def __init__(
        self, model: Model, **kwargs,
    ):
        if not isinstance(model, Model):
            raise TypeError(
                "`sample` function only supports `pymc4.Model` objects, but you've passed `{}`".format(
                    type(model)
                )
            )

        non_sampling_state, disc_names, cont_names, _ = initialize_state(model)
        if self._grad is True and disc_names:
            raise ValueError("Discrete distributions can't be used with gradient-based sampler")

        self.model = model
        self._stat_names: List = []
        # assign arguments from **kwargs to distinct kwargs for `kernel`, `adaptation_kernel`, `chain_sampler`
        self._assign_arguments(kwargs)
        self._check_arguments()
        self._bound_kwargs()

    def sample(
        self,
        *,
        num_samples=1000,
        num_chains=10,
        burn_in=100,
        observed: Optional[dict] = None,
        state: Optional[flow.SamplingState] = None,
        use_auto_batching: bool = True,
        xla=False,
        is_compound=False,
    ):
        """
            Docs
        """
        if state is not None and observed is not None:
            raise ValueError("Can't use both `state` and `observed` arguments")

        (
            logpfn,
            init,
            _deterministics_callback,
            deterministic_names,
            state_,
        ) = build_logp_and_deterministic_functions(
            self.model,
            num_chains=num_chains,
            state=state,
            observed=observed,
            collect_reduced_log_prob=use_auto_batching,
        )

        init_state = list(init.values())
        init_keys = list(init.keys())
        if use_auto_batching:
            self.parallel_logpfn = vectorize_logp_function(logpfn)
            self.deterministics_callback = vectorize_logp_function(_deterministics_callback)
            init_state = tile_init(init_state, num_chains)
        else:
            self.parallel_logpfn = logpfn
            self.deterministics_callback = _deterministics_callback
            init_state = tile_init(init_state, num_chains)

        # TODO: problem with tf.function when passing as argument to self._run_chains
        self._num_samples = num_samples

        if xla:
            results, sample_stats = tf.xla.experimental.compile(
                self._run_chains, inputs=[init_state, burn_in,],
            )
        else:
            results, sample_stats = self._run_chains(init_state, burn_in,)



        posterior = dict(zip(init_keys, results))
        # Keep in sync with pymc3 naming convention
        if len(sample_stats) > len(self._stat_names):
            deterministic_values = sample_stats[len(self._stat_names) :]
            sample_stats = sample_stats[: len(self._stat_names)]
        sampler_stats = dict(zip(self._stat_names, sample_stats))
        if len(deterministic_names) > 0:
            posterior.update(dict(zip(deterministic_names, deterministic_values)))

        if is_compound is True:
            sampler_stats = None

        return trace_to_arviz(posterior, sampler_stats, observed_data=state_.observed_values)

    @tf.function(autograph=False)
    def _run_chains(self, init, burn_in):
        """
            Docs
        """
        kernel = self._kernel(target_log_prob_fn=self.parallel_logpfn, **self.kernel_kwargs)
        if self._adaptation:
            adapt_kernel = self._adaptation(inner_kernel=kernel, **self.adaptation_kwargs,)
        else:
            adapt_kernel = kernel

        results, sample_stats = mcmc.sample_chain(
            self._num_samples,
            current_state=init,
            kernel=adapt_kernel,
            num_burnin_steps=burn_in,
            trace_fn=self._trace_fn,
            **self.chain_kwargs,
        )
        return results, sample_stats

    @abc.abstractmethod
    def _trace_fn(self, current_state, pkr):
        pass

    def _assign_arguments(self, kwargs):
        kwargs_keys = set(kwargs.keys())
        adaptation_keys = set(
            list(inspect.signature(self._adaptation.__init__).parameters.keys())[1:]
        )
        kernel_keys = set(list(inspect.signature(self._kernel.__init__).parameters.keys())[1:])
        chain_keys = set(list(inspect.signature(mcmc.sample_chain).parameters.keys()))

        self.adaptation_kwargs = {k: kwargs[k] for k in (adaptation_keys & kwargs_keys)}
        self.kernel_kwargs = {k: kwargs[k] for k in (kernel_keys & kwargs_keys)}
        self.chain_kwargs = {k: kwargs[k] for k in (chain_keys & kwargs_keys)}

    def _check_arguments(self):
        if (
            (self.adaptation_kwargs.keys() & self.kernel_kwargs.keys())
            or (self.adaptation_kwargs.keys() & self.chain_kwargs.keys())
            or (self.kernel_kwargs.keys() & self.chain_kwargs.keys())
        ):
            raise ValueError(
                "Ambiguity in setting kwargs for `kernel`, `adaptation_kernel`, `chain_sampler`"
            )

        method_kwargs_pairs = [
            (self._adaptation, self.adaptation_kwargs),
            (self._kernel, self.kernel_kwargs),
            (mcmc.sample_chain, self.chain_kwargs),
        ]
        self._check_kwargs(method_kwargs_pairs)

    def _check_kwargs(self, method_kwargs_pairs):
        for (class_method, object_kwargs) in method_kwargs_pairs:
            if not class_method:
                continue
            if not callable(class_method):
                class_method = class_method.__init__
                class_keys = set(list(inspect.signature(class_method).parameters.keys()[1:]))
            else:
                class_keys = set(list(inspect.signature(class_method).parameters.keys()))
            if len(class_keys & object_kwargs.keys()) > len(class_keys):
                raise "{} does not support passed arguments".format(class_method.__name__)

    def __call__(self, *args, **kwargs):
        return self.sample(*args, **kwargs)

    def _bound_kwargs(self, *args):
        for k, v in self._default_kernel_kwargs.items():
            self.kernel_kwargs.setdefault(k, v)
        for k, v in self._default_adapter_kwargs.items():
            self.adaptation_kwargs.setdefault(k, v)

    @classmethod
    def _default_kernel_maker(cls):
        # TODO: maybe can be done with partial, but not sure how to do it recursively
        kernel_collection = KERNEL_KWARGS_SET(
            kernel=cls._kernel,
            adaptive_kernel=cls._adaptation,
            kernel_kwargs=cls._default_kernel_kwargs,
            adaptive_kwargs=cls._default_adapter_kwargs,
        )
        return kernel_collection


@register_sampler
class HMC(_BaseSampler):
    _name = "hmc"
    _adaptation = mcmc.DualAveragingStepSizeAdaptation
    _kernel = mcmc.HamiltonianMonteCarlo
    _grad = True

    _default_kernel_kwargs: dict = {"step_size": 0.1, "num_leapfrog_steps": 3}
    _default_adapter_kwargs: dict = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._stat_names = {"mean_tree_accept"}

    def _trace_fn(self, current_state, pkr):
        return (pkr.inner_results.log_accept_ratio,) + tuple(
            self.deterministics_callback(*current_state)
        )


@register_sampler
class HMCSimple(HMC):
    _name = "hmc_simple"
    _adaptation = mcmc.SimpleStepSizeAdaptation


@register_sampler
class NUTS(_BaseSampler):
    _name = "nuts"
    _adaptation = mcmc.DualAveragingStepSizeAdaptation
    _kernel = mcmc.NoUTurnSampler
    _grad = True

    _default_adapter_kwargs: dict = {
        "num_adaptation_steps": 100,  # TODO: why thoud?
        "step_size_getter_fn": lambda pkr: pkr.step_size,
        "log_accept_prob_getter_fn": lambda pkr: pkr.log_accept_ratio,
        "step_size_setter_fn": lambda pkr, new_step_size: pkr._replace(step_size=new_step_size),
    }
    _default_kernel_kwargs: dict = {"step_size": 0.1}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._stat_names = ["lp", "tree_size", "diverging", "energy", "mean_tree_accept"]

    def _trace_fn(self, current_state, pkr):
        return (
            pkr.inner_results.target_log_prob,
            pkr.inner_results.leapfrogs_taken,
            pkr.inner_results.has_divergence,
            pkr.inner_results.energy,
            pkr.inner_results.log_accept_ratio,
        ) + tuple(self.deterministics_callback(*current_state))


@register_sampler
class NUTSSimple(NUTS):
    _name = "nuts_simple"
    _adaptation = mcmc.SimpleStepSizeAdaptation


@register_sampler
class RandomWalkM(_BaseSampler):
    _name = "randomwalkm"
    _adaptation = None
    _kernel = mcmc.RandomWalkMetropolis
    _grad = False

    _default_kernel_kwargs: dict = {}
    _default_adapter_kwargs: dict = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._stat_names = ["mean_accept"]

    def _trace_fn(self, current_state, pkr):
        return (pkr.log_accept_ratio,) + tuple(self.deterministics_callback(*current_state))


@register_sampler
class CompoundStep(_BaseSampler):
    """The basic implementation of the compound step"""

    _name = "compound"
    _adaptation = None
    _kernel = _CompoundStepTF
    _grad = False

    _default_adapter_kwargs: dict = {}
    _default_kernel_kwargs: dict = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._stat_names = ["compound_results"]

    def _trace_fn(self, current_state, pkr):
        return (pkr, ) + tuple(self.deterministics_callback(*current_state))

    @staticmethod
    def _convert_sampler_methods(sampler_methods):
        sampler_methods_dict = {}
        for (var, sampler) in sampler_methods:
            if isinstance(var, (list, tuple)):
                for var_ in var:
                    sampler_methods_dict[var_] = sampler
            else:
                sampler_methods_dict[var] = sampler
        return sampler_methods_dict

    def _assign_default_methods(
        self,
        *,
        sampler_methods=None,
        state: Optional[flow.SamplingState] = None,
        observed: Optional[dict] = None,
    ):
        if sampler_methods is not None:
            sampler_methods = CompoundStep._convert_sampler_methods(sampler_methods)

        (state, _, _, _) = initialize_state(self.model, observed=observed, state=state)
        init = state.all_unobserved_values
        init_state = list(init.values())
        init_keys = list(init.keys())
        make_kernel_fn: list = []
        part_kernel_kwargs: list = []

        rwm_len = nuts_len = 0

        for i, state_part in enumerate(init_state):
            unscoped_var = init_keys[i].rsplit("/", 1)[-1]
            distr = state.continuous_distributions.get(init_keys[i], None)
            if distr is None:
                distr = state.discrete_distributions[init_keys[i]]

            # simplest way of assigning sampling methods
            # if the sampler_methods was passed and if a var is provided
            # then the var will be assigned to the given sampler
            # but will also be checked if the sampler supports the distr

            if sampler_methods and unscoped_var in sampler_methods:
                part_kernel_kwargs.append({})
                # add the default `new_state_fn` for each distribution
                func = distr._default_new_state_part
                if callable(func):
                    part_kernel_kwargs[i]["new_state_fn"] = functools.partial(func)()

                sampler = sampler_methods[unscoped_var]
                if not distr._grad_support and sampler._grad:
                    raise ValueError(
                        "The `{}` doesn't support gradient, please provide an appropriate sampler method".format(
                            unscoped_var
                        )
                    )

                make_kernel_fn.append(sampler._default_kernel_maker())
                # TODO: fix hard coding
                if sampler.__name__ == "NUTS":
                    nuts_len += 1
                if sampler.__name__ == "RandomWalkM":
                    rwm_len += 1
                else:
                    raise NotImplementedError

            # TODO: fix logic
            elif distr._grad_support:
                if nuts_len == 0:
                    make_kernel_fn.append(NUTS._default_kernel_maker())
                    part_kernel_kwargs.append({})
                nuts_len += 1
            else:
                if rwm_len == 0:
                    make_kernel_fn.append(RandomWalkM._default_kernel_maker())
                    part_kernel_kwargs.append({})
                rwm_len += 1

        self.kernel_kwargs["make_kernel_fn"] = make_kernel_fn
        self.kernel_kwargs["kernel_kwargs"] = part_kernel_kwargs
        kkw = [rwm_len, nuts_len]
        kkw = [i for i in kkw if i != 0]
        self.kernel_kwargs["li"] = kkw

    def __call__(self, *args, **kwargs):
        return self.sample(*args, is_compound=True, **kwargs)

def build_logp_and_deterministic_functions(
    model,
    num_chains: Optional[int] = None,
    observed: Optional[dict] = None,
    state: Optional[flow.SamplingState] = None,
    collect_reduced_log_prob: bool = True,
):
    if not isinstance(model, Model):
        raise TypeError(
            "`sample` function only supports `pymc4.Model` objects, but you've passed `{}`".format(
                type(model)
            )
        )
    if state is not None and observed is not None:
        raise ValueError("Can't use both `state` and `observed` arguments")

    state, deterministic_names = initialize_sampling_state(model, observed=observed, state=state)

    if not state.all_unobserved_values:
        raise ValueError(
            f"Can not calculate a log probability: the model {model.name or ''} has no unobserved values."
        )

    observed_var = state.observed_values
    unobserved_keys, unobserved_values = zip(*state.all_unobserved_values.items())

    if collect_reduced_log_prob:

        @tf.function(autograph=False)
        def logpfn(*values, **kwargs):
            if kwargs and values:
                raise TypeError("Either list state should be passed or a dict one")
            elif values:
                kwargs = dict(zip(unobserved_keys, values))
            st = flow.SamplingState.from_values(kwargs, observed_values=observed)
            _, st = flow.evaluate_model_transformed(model, state=st)
            return st.collect_log_prob()

    else:
        # When we use manual batching, we need to manually tile the chains axis
        # to the left of the observed tensors
        if num_chains is not None:
            obs = state.observed_values
            if observed is not None:
                obs.update(observed)
            else:
                observed = obs
            for k, o in obs.items():
                o = tf.convert_to_tensor(o)
                o = tf.tile(o[None, ...], [num_chains] + [1] * o.ndim)
                observed[k] = o

        @tf.function(autograph=False)
        def logpfn(*values, **kwargs):
            if kwargs and values:
                raise TypeError("Either list state should be passed or a dict one")
            elif values:
                kwargs = dict(zip(unobserved_keys, values))
            st = flow.SamplingState.from_values(kwargs, observed_values=observed)
            _, st = flow.evaluate_model_transformed(model, state=st)
            return st.collect_unreduced_log_prob()

    @tf.function(autograph=False)
    def deterministics_callback(*values, **kwargs):
        if kwargs and values:
            raise TypeError("Either list state should be passed or a dict one")
        elif values:
            kwargs = dict(zip(unobserved_keys, values))
        st = flow.SamplingState.from_values(kwargs, observed_values=observed_var)
        _, st = flow.evaluate_model_transformed(model, state=st)
        for transformed_name in st.transformed_values:
            untransformed_name = NameParts.from_name(transformed_name).full_untransformed_name
            st.deterministics[untransformed_name] = st.untransformed_values.pop(untransformed_name)
        return st.deterministics.values()

    return (
        logpfn,
        dict(state.all_unobserved_values),
        deterministics_callback,
        deterministic_names,
        state,
    )


def vectorize_logp_function(logpfn):
    # TODO: vectorize with dict
    def vectorized_logpfn(*state):
        return tf.vectorized_map(lambda mini_state: logpfn(*mini_state), state)

    return vectorized_logpfn


def tile_init(init, num_repeats):
    return [tf.tile(tf.expand_dims(tens, 0), [num_repeats] + [1] * tens.ndim) for tens in init]