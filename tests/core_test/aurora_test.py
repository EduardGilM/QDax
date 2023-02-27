"""Tests AURORA implementation"""

import functools
from typing import Any, Dict, Tuple

import jax
import jax.numpy as jnp
import pytest

from qdax import environments
from qdax.core.aurora import AURORA
from qdax.core.containers.mapelites_repertoire import MapElitesRepertoire
from qdax.core.emitters.mutation_operators import isoline_variation
from qdax.core.emitters.standard_emitters import MixingEmitter
from qdax.core.neuroevolution.buffers.buffer import QDTransition
from qdax.core.neuroevolution.networks.networks import MLP
from qdax.environments.bd_extractors import get_aurora_bd
from qdax.tasks.brax_envs import scoring_aurora_function
from qdax.types import EnvState, Params, RNGKey
from qdax.utils import train_seq2seq


@pytest.mark.parametrize(
    "env_name, batch_size",
    [("halfcheetah_uni", 10), ("walker2d_uni", 10), ("hopper_uni", 10)],
)
def test_aurora(env_name: str, batch_size: int) -> None:
    batch_size = batch_size
    env_name = env_name
    episode_length = 100
    num_iterations = 5
    seed = 42
    policy_hidden_layer_sizes = (64, 64)
    num_centroids = 50

    observation_option = "only_sd"
    hidden_size = 5
    l_value_init = 0.2

    log_freq = 5

    # Init environment
    env = environments.create(env_name, episode_length=episode_length)

    # Init a random key
    random_key = jax.random.PRNGKey(seed)

    # Init policy network
    policy_layer_sizes = policy_hidden_layer_sizes + (env.action_size,)
    policy_network = MLP(
        layer_sizes=policy_layer_sizes,
        kernel_init=jax.nn.initializers.lecun_uniform(),
        final_activation=jnp.tanh,
    )

    # Init population of controllers
    random_key, subkey = jax.random.split(random_key)
    keys = jax.random.split(subkey, num=batch_size)
    fake_batch = jnp.zeros(shape=(batch_size, env.observation_size))
    init_variables = jax.vmap(policy_network.init)(keys, fake_batch)

    # Create the initial environment states
    random_key, subkey = jax.random.split(random_key)
    keys = jnp.repeat(jnp.expand_dims(subkey, axis=0), repeats=batch_size, axis=0)
    reset_fn = jax.jit(jax.vmap(env.reset))
    init_states = reset_fn(keys)

    # Define the fonction to play a step with the policy in the environment
    def play_step_fn(
        env_state: EnvState,
        policy_params: Params,
        random_key: RNGKey,
    ) -> Tuple[EnvState, Params, RNGKey, QDTransition]:
        """
        Play an environment step and return the updated state and the transition.
        """

        actions = policy_network.apply(policy_params, env_state.obs)

        state_desc = env_state.info["state_descriptor"]
        next_state = env.step(env_state, actions)

        transition = QDTransition(
            obs=env_state.obs,
            next_obs=next_state.obs,
            rewards=next_state.reward,
            dones=next_state.done,
            actions=actions,
            truncations=next_state.info["truncation"],
            state_desc=state_desc,
            next_state_desc=next_state.info["state_descriptor"],
        )

        return next_state, policy_params, random_key, transition

    # Prepare the scoring function
    bd_extraction_fn = functools.partial(
        get_aurora_bd,
        option=observation_option,
        hidden_size=hidden_size,
    )
    scoring_fn = functools.partial(
        scoring_aurora_function,
        init_states=init_states,
        episode_length=episode_length,
        play_step_fn=play_step_fn,
        behavior_descriptor_extractor=bd_extraction_fn,
    )

    # Define emitter
    variation_fn = functools.partial(isoline_variation, iso_sigma=0.05, line_sigma=0.1)
    mixing_emitter = MixingEmitter(
        mutation_fn=lambda x, y: (x, y),
        variation_fn=variation_fn,
        variation_percentage=1.0,
        batch_size=batch_size,
    )

    # Get minimum reward value to make sure qd_score are positive
    reward_offset = environments.reward_offset[env_name]

    # Define a metrics function
    def metrics_fn(repertoire: MapElitesRepertoire) -> Dict:

        # Get metrics
        grid_empty = repertoire.fitnesses == -jnp.inf
        qd_score = jnp.sum(repertoire.fitnesses, where=~grid_empty)
        # Add offset for positive qd_score
        qd_score += reward_offset * episode_length * jnp.sum(1.0 - grid_empty)
        coverage = 100 * jnp.mean(1.0 - grid_empty)
        max_fitness = jnp.max(repertoire.fitnesses)

        return {"qd_score": qd_score, "max_fitness": max_fitness, "coverage": coverage}

    # Instantiate MAP-Elites
    aurora = AURORA(
        scoring_function=scoring_fn,
        emitter=mixing_emitter,
        metrics_function=metrics_fn,
    )

    aurora_dims = hidden_size
    centroids = jnp.zeros(shape=(num_centroids, aurora_dims))

    @jax.jit
    def update_scan_fn(carry: Any, unused: Any) -> Any:
        # iterate over grid
        (
            repertoire,
            random_key,
            model_params,
            mean_observations,
            std_observations,
        ) = carry
        (repertoire, _, metrics, random_key,) = aurora.update(
            repertoire,
            None,
            random_key,
            model_params,
            mean_observations,
            std_observations,
        )

        return (
            (repertoire, random_key, model_params, mean_observations, std_observations),
            metrics,
        )

    # Init algorithm
    ## AutoEncoder Params and INIT
    # observations_dims = (20, 25)
    obs_dim = jnp.minimum(env.observation_size, 25)
    if observation_option == "full":
        observations_dims = (25, obs_dim + 2)  # 250 / 10, 25 + 2
    if observation_option == "no_sd":
        observations_dims = (25, obs_dim)  # 250 / 10, 25
    if observation_option == "only_sd":
        observations_dims = (25, 2)  # 250 / 10, 2

    model = train_seq2seq.get_model(
        observations_dims[-1], True, hidden_size=hidden_size
    )
    random_key, subkey = jax.random.split(random_key)

    # design aurora's schedule
    default_update_base = 10
    update_base = int(jnp.ceil(default_update_base / log_freq))
    schedules = jnp.cumsum(jnp.arange(update_base, 1000, update_base))
    print("Schedules: ", schedules)

    model_params = train_seq2seq.get_initial_params(
        model, subkey, (1, observations_dims[0], observations_dims[-1])
    )
    # model_params = train_seq2seq.get_initial_params(model,subkey,(1,repertoire.observations.shape[1],repertoire.observations.shape[-1]))
    print(jax.tree_map(lambda x: x.shape, model_params))

    mean_observations = jnp.zeros(observations_dims[-1])

    std_observations = jnp.ones(observations_dims[-1])

    repertoire, _, random_key = aurora.init(
        init_variables,
        centroids,
        random_key,
        model_params,
        mean_observations,
        std_observations,
        l_value_init,
    )

    ## Initializing Means and stds and Aurora
    random_key, subkey = jax.random.split(random_key)
    model_params, mean_observations, std_observations = train_seq2seq.lstm_ae_train(
        subkey, repertoire, model_params, 0, hidden_size=hidden_size
    )

    current_step_estimation = 0
    num_iterations = 0

    # Main loop
    n_target = 1024

    previous_error = jnp.sum(repertoire.fitnesses != -jnp.inf) - n_target

    iteration = 1  # to be consistent with other exp scripts
    while iteration < num_iterations:

        (
            (repertoire, random_key, model_params, mean_observations, std_observations),
            metrics,
        ) = jax.lax.scan(
            update_scan_fn,
            (repertoire, random_key, model_params, mean_observations, std_observations),
            (),
            length=log_freq,
        )

        num_iterations = iteration * log_freq

        # update nb steps estimation
        current_step_estimation += batch_size * episode_length * log_freq

        ## Autoencoder Steps and CVC
        # individuals_in_repo = jnp.sum(repertoire.fitnesses != -jnp.inf)

        if (iteration + 1) in schedules:
            random_key, subkey = jax.random.split(random_key)

            (
                model_params,
                mean_observations,
                std_observations,
            ) = train_seq2seq.lstm_ae_train(
                subkey,
                repertoire,
                model_params,
                iteration,
                hidden_size=hidden_size,
            )
            ### RE-ADDITION OF ALL THE NEW BEHAVIOURAL DESCRIPTORS WITH THE NEW AE

            # model = train_seq2seq.get_model(repertoire.observations.shape[-1],True) ## lstm seq2seq
            normalized_observations = (
                repertoire.observations - mean_observations
            ) / std_observations
            new_descriptors = model.apply(
                {"params": model_params}, normalized_observations, method=model.encode
            )
            repertoire = repertoire.init(
                genotypes=repertoire.genotypes,
                centroids=repertoire.centroids,
                fitnesses=repertoire.fitnesses,
                descriptors=new_descriptors,
                observations=repertoire.observations,
                l_value=repertoire.l_value,
            )
            num_indivs = jnp.sum(repertoire.fitnesses != -jnp.inf)

        elif iteration % 2 == 0:

            num_indivs = jnp.sum(repertoire.fitnesses != -jnp.inf)

            # l_value =  repertoire.l_value * (1+1*10e-7*(num_indivs-n_target))
            current_error = num_indivs - n_target
            change_rate = current_error - previous_error
            prop_gain = 1 * 10e-6
            l_value = (
                repertoire.l_value
                + (prop_gain * (current_error))
                + (prop_gain * change_rate)
            )
            print(change_rate, current_error)
            previous_error = current_error
            ## CVC Implementation to keep a Constant number of individuals in the Archive
            repertoire = repertoire.init(
                genotypes=repertoire.genotypes,
                centroids=repertoire.centroids,
                fitnesses=repertoire.fitnesses,
                descriptors=repertoire.descriptors,
                observations=repertoire.observations,
                l_value=l_value,
            )
            new_num_indivs = jnp.sum(repertoire.fitnesses != -jnp.inf)

    pytest.assume(repertoire is not None)


if __name__ == "__main__":
    test_aurora(env_name="pointmaze", batch_size=10)
