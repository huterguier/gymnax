"""JAX implementation of Freeway MinAtar environment.


Source:
github.com/kenjyoung/MinAtar/blob/master/minatar/environments/freeway.py


ENVIRONMENT DESCRIPTION - 'Freeway-MinAtar'
- Player starts at bottom of screen and can travel up/down.
- Player speed is restricted s.t. player only moves every 3 frames.
- Reward +1 given when player reaches top of screen -> returns to bottom.
- 8 cars travel horizontally on screen and teleport to other side at edge.
- When player is hit by a car, he is returned to the bottom of the screen.
- Car direction and speed are indicated by 5 trail channels.
- Each time player reaches top of screen, car speeds are randomized.
- Termination occurs after 2500 frames.
- Channels are encoded as follows: 'chicken':0, 'car':1, 'speed1':2,
- 'speed2':3, 'speed3':4, 'speed4':5, 'speed5':6
- Observation has dimensionality (10, 10, 4)
- Actions are encoded as follows: ['n', 'u', 'd']
"""

from typing import Any

import jax
import jax.numpy as jnp
from flax import struct

from gymnax.environments import environment, spaces


@struct.dataclass
class EnvState(environment.EnvState):
    pos: int
    cars: jax.Array
    move_timer: int
    time: int
    terminal: bool


@struct.dataclass
class EnvParams(environment.EnvParams):
    player_speed: int = 3
    max_steps_in_episode: int = 2500


class MinFreeway(environment.Environment[EnvState, EnvParams]):
    """JAX implementation of Freeway MinAtar environment."""

    def __init__(self, use_minimal_action_set: bool = True):
        super().__init__()
        self.obs_shape = (10, 10, 7)
        # Full action set: ['n','l','u','r','d','f']
        self.full_action_set = jnp.array([0, 1, 2, 3, 4, 5])
        # Minimal action set: ['n', 'u', 'd']
        self.minimal_action_set = jnp.array([0, 2, 4])
        # Set active action set for environment
        # If minimal map to integer in full action set
        if use_minimal_action_set:
            self.action_set = self.minimal_action_set
        else:
            self.action_set = self.full_action_set

    @property
    def default_params(self) -> EnvParams:
        # Default environment parameters
        return EnvParams()

    def step_env(
        self,
        key: jax.Array,
        state: EnvState,
        action: int | float | jax.Array,
        params: EnvParams,
    ) -> tuple[jax.Array, EnvState, jax.Array, jax.Array, dict[Any, Any]]:
        """Perform single timestep state transition."""
        # 1. Update position of agent only if timer condition is met!
        a = self.action_set[action]
        state, reward, win_cond = step_agent(a, state, params)

        # 2. Sample new config for cars if agent 'won' - bool step_agent
        # Note: At each step we are sampling speed and dir to avoid if cond
        # by masking - still faster after compilation than numpy version!
        key_speed, key_dirs = jax.random.split(key)
        speeds = jax.random.randint(key_speed, shape=(8,), minval=1, maxval=6)
        directions = jax.random.choice(key_dirs, jnp.array([-1, 1]), shape=(8,))
        win_cars = randomize_cars(speeds, directions, state.cars, False)
        state = state.replace(cars=jax.lax.select(win_cond, win_cars, state.cars))

        # 3. Update cars and check for collisions! - respawn agent at bottom
        state = step_cars(state)

        # Check game condition & no. steps for termination condition
        state = state.replace(time=state.time + 1)
        done = self.is_terminal(state, params)
        state = state.replace(terminal=done)
        info = {"discount": self.discount(state, params)}
        return (
            jax.lax.stop_gradient(self.get_obs(state)),
            jax.lax.stop_gradient(state),
            reward.astype(jnp.float32),
            done,
            info,
        )

    def reset_env(
        self, key: jax.Array, params: EnvParams
    ) -> tuple[jax.Array, EnvState]:
        """Reset environment state by sampling initial position."""
        # Sample the initial speeds and directions of the cars
        key_speed, key_dirs = jax.random.split(key)
        speeds = jax.random.randint(key_speed, shape=(8,), minval=1, maxval=6)
        directions = jax.random.choice(key_dirs, jnp.array([-1, 1]), shape=(8,))
        state = EnvState(
            pos=9,
            cars=randomize_cars(speeds, directions, jnp.zeros((8, 4), dtype=int), True),
            move_timer=params.player_speed,
            time=0,
            last_action=jnp.array(0, dtype=jnp.int32),
            terminal=False,
        )
        return self.get_obs(state), state

    def get_obs(self, state: EnvState, params=None, key=None) -> jax.Array:
        """Return observation from raw state trafo."""
        obs = jnp.zeros(self.obs_shape, dtype=bool)
        # Set the position of the chicken agent, cars, and trails
        obs = obs.at[state.pos, 4, 0].set(True)
        for car_id in range(8):
            car = state.cars[car_id]
            obs = obs.at[car[1], car[0], 1].set(True)
            # Boundary conditions for cars
            back_x = (car[3] > 0).astype(jnp.int32) * (car[0] - 1) + (
                1 - (car[3] > 0).astype(jnp.int32)
            ) * (car[0] + 1)
            left_out = (back_x < 0).astype(jnp.int32)
            right_out = (back_x > 9).astype(jnp.int32)
            back_x = left_out * 9 + (1 - left_out) * back_x
            back_x = right_out * 0 + (1 - right_out) * back_x
            # Set trail to be on
            trail_channel = (
                2 * (jnp.abs(car[3]) == 1).astype(jnp.int32)
                + 3 * (jnp.abs(car[3]) == 2).astype(jnp.int32)
                + 4 * (jnp.abs(car[3]) == 3).astype(jnp.int32)
                + 5 * (jnp.abs(car[3]) == 4).astype(jnp.int32)
                + 6 * (jnp.abs(car[3]) == 5).astype(jnp.int32)
            )
            obs = obs.at[car[1], back_x, trail_channel].set(True)
        return obs.astype(jnp.float32)

    def is_terminal(self, state: EnvState, params: EnvParams) -> jax.Array:
        """Check whether state is terminal."""
        done_steps = state.time >= params.max_steps_in_episode
        return jnp.array(done_steps)

    @property
    def name(self) -> str:
        """Environment name."""
        return "Freeway-MinAtar"

    @property
    def num_actions(self) -> int:
        """Number of actions possible in environment."""
        return len(self.action_set)

    def action_space(self, params: EnvParams | None = None) -> spaces.Discrete:
        """Action space of the environment."""
        return spaces.Discrete(len(self.action_set))

    def observation_space(self, params: EnvParams) -> spaces.Box:
        """Observation space of the environment."""
        return spaces.Box(0, 1, self.obs_shape)

    def state_space(self, params: EnvParams) -> spaces.Dict:
        """State space of the environment."""
        return spaces.Dict(
            {
                "pos": spaces.Discrete(10),
                "cars": spaces.Box(0, 1, jnp.zeros((8, 4)), dtype=jnp.int32),
                "move_timer": spaces.Discrete(params.player_speed),
                "time": spaces.Discrete(params.max_steps_in_episode),
                "terminal": spaces.Discrete(2),
            }
        )


def step_agent(
    action: jax.Array, state: EnvState, params: EnvParams
) -> tuple[EnvState, jax.Array, bool]:
    """Perform 1st part of step transition for agent."""
    cond_up = jnp.logical_and(action == 2, state.move_timer == 0)
    cond_down = jnp.logical_and(action == 4, state.move_timer == 0)
    any_cond = jnp.logical_or(cond_up, cond_down)
    state_up = jnp.maximum(0, state.pos - 1)
    state_down = jnp.minimum(9, state.pos + 1)
    pos = (
        (1 - any_cond.astype(jnp.int32)) * state.pos
        + cond_up.astype(jnp.int32) * state_up
        + cond_down.astype(jnp.int32) * state_down
    )
    move_timer = jax.lax.select(any_cond, params.player_speed, state.move_timer)
    # Check win cond. - increase reward, randomize cars, reset agent position
    win_cond = pos == 0
    reward = win_cond.astype(jnp.int32) * 1.0
    pos = jax.lax.select(win_cond, 9, pos)
    return state.replace(pos=pos, move_timer=move_timer), reward, win_cond


def step_cars(state: EnvState) -> EnvState:
    """Perform 3rd part of step transition for car."""
    # Update cars and check for collisions! - respawn agent at bottom
    pos = state.pos
    cars = state.cars
    for car_id in range(8):
        # Check for agent collision with car and if so reset agent
        collision_cond = jnp.logical_and(
            cars[car_id][0] == 4,
            cars[car_id][1] == pos,
        )

        pos = jax.lax.select(collision_cond, 9, pos)

        # Check for exiting frame, reset car and then check collision again
        car_cond = cars[car_id][2] == 0
        upd_2 = jax.lax.select(car_cond, jnp.abs(cars[car_id][3]), cars[car_id][2])

        cars = cars.at[car_id, 2].set(upd_2)
        upd_0 = jax.lax.select(
            car_cond,
            (
                cars[car_id][0]
                + 1 * (cars[car_id][3] > 0)
                - 1 * (1 - (cars[car_id][3] > 0))
            ),
            cars[car_id][0],
        )
        cars = cars.at[car_id, 0].set(upd_0)

        cond_sm_0 = jnp.logical_and(car_cond, cars[car_id][0] < 0)
        upd_0_sm = jax.lax.select(cond_sm_0, 9, cars[car_id][0])
        cars = cars.at[car_id, 0].set(upd_0_sm)
        cond_gr_9 = jnp.logical_and(car_cond, cars[car_id][0] > 9)
        upd_0_gr = jax.lax.select(cond_gr_9, 0, cars[car_id][0])
        cars = cars.at[car_id, 0].set(upd_0_gr)

        # Check collision after car position update - respawn agent
        # Note: Need to reevaluate collision condition since cars change!
        collision_cond = jnp.logical_and(
            cars[car_id][0] == 4,
            cars[car_id][1] == pos,
        )
        cond_pos = jnp.logical_and(car_cond, collision_cond)
        pos = jax.lax.select(cond_pos, 9, pos)
        # Move car if no previous car_cond update
        alt_upd_2 = jax.lax.select(car_cond, cars[car_id][2], cars[car_id][2] - 1)
        cars = cars.at[car_id, 2].set(alt_upd_2)
    # 4. Update various timers
    move_timer = state.move_timer - (state.move_timer > 0)
    return state.replace(pos=pos, cars=cars, move_timer=move_timer)


def randomize_cars(
    speeds: jax.Array,
    directions: jax.Array,
    old_cars: jax.Array,
    initialize: bool,
) -> jax.Array:
    """Randomize car speeds & directions. Reset position if initialize."""
    speeds_new = directions * speeds
    new_cars = jnp.zeros((8, 4), dtype=jnp.int32)

    # Loop over all 8 cars and set their data
    for i in range(8):
        # Reset both speeds, directions and positions
        new_cars = new_cars.at[i, :].set(
            [0, i + 1, jnp.abs(speeds_new[i]), speeds_new[i]],
        )
        # Reset only speeds and directions
        old_cars = old_cars.at[i, 2:4].set(
            [jnp.abs(speeds_new[i]), speeds_new[i]],
        )

    # Mask the car array manipulation according to initialize
    cars = jax.lax.select(initialize, new_cars, old_cars)
    return jnp.array(cars, dtype=jnp.int32)
