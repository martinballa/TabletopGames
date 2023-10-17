# various helper functions
import gymnasium as gym
from gymnasium.wrappers.frame_stack import FrameStack
import numpy as np
import torch

import jpype
import jpype.imports
from pytag import PyTAG, MultiAgentPyTAG
from pytag.utils.wrappers import StrategoWrapper, SushiGoWrapper, MASushiGoWrapper


def make_ma_env(env_id, seed, opponent, n_players, framestack=1, obs_type="vector", randomise_order=False):
    def thunk():
        # always have a python agent first (at least in our experiments)
        agent_ids = ["python"] * n_players
        for i in range(n_players - 1):
            agent_ids.append(opponent)
        # if randomise_order:
        #     np.random.shuffle(agent_ids)
        # env = MultiAgentPyTAG(agent_ids=agent_ids, game_id=env_id, seed=seed, obs_type=obs_type)
        # obs_type = "json" if "Sushi" in env_id else "vector" # , obs_type=obs_type
        env = gym.make(env_id, seed=seed, agent_ids=agent_ids, obs_type=obs_type)
        # todo maybe these wrappers are not compatible
        if "Stratego" in env_id:
            env = StrategoWrapper(env)
        if "Sushi" in env_id:
            env = MASushiGoWrapper(env)
        if framestack > 1:
            env = FrameStack(env, framestack)
        return env
    return thunk
def make_env(env_id, seed, opponent, n_players, framestack=1, obs_type="vector", randomise_order=False):
    def thunk():
        # always have a python agent first (at least in our experiments)
        agent_ids = ["python"]
        for i in range(n_players - 1):
            agent_ids.append(opponent)
        if randomise_order:
            np.random.shuffle(agent_ids)
        # obs_type = "json" if "Sushi" in env_id else "vector" # , obs_type=obs_type
        env = gym.make(env_id, seed=seed, agent_ids=agent_ids, obs_type=obs_type)
        if "Stratego" in env_id:
            env = StrategoWrapper(env)
        if "Sushi" in env_id:
            env = SushiGoWrapper(env)
        if framestack > 1:
            env = FrameStack(env, framestack)
        return env
    return thunk
def get_agent_list():
    return ["random", "mcts", "osla", "python"]

def get_agent_class(agent_name):
    if agent_name == "random":
        return jpype.JClass("players.simple.RandomPlayer")
    if agent_name == "mcts":
        return jpype.JClass("players.mcts.MCTSPlayer")
    if agent_name == "osla":
        return jpype.JClass("players.simple.OSLAPlayer")
    if agent_name == "python":
        return jpype.JClass("players.python.PythonAgent")
    return None

def layer_init(layer, std=np.sqrt(2), bias_const=0.0):
    torch.nn.init.orthogonal_(layer.weight, std)
    torch.nn.init.constant_(layer.bias, bias_const)
    return layer