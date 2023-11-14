# docs and experiment results can be found at https://docs.cleanrl.dev/rl-algorithms/ppo/#ppopy
import argparse
import os
import random
import time
from collections import deque
from distutils.util import strtobool

import gymnasium as gym
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim

from torch.utils.tensorboard import SummaryWriter

from pytag.utils.wrappers import MergeActionMaskWrapper, RecordEpisodeStatistics, RecordSelfPlayEpStats
from pytag.utils.common import make_env, make_sp_env
from examples.utils.networks import PPONet

class SelfPlayAssistant():
    '''
    Self-play assistant for PPO - handles checkpointing and opponent selection

    '''

    def __init__(self, checkpoint_freq=int(5e3), window=10, replace_freq=int(1e3), self_play_prob=0.7, save_checkpoints=False, checkpoint_dir="~/data/PPO-SP/checkpoints/", seed=None):
        self.checkpoint_freq = checkpoint_freq # how often we want to save a checkpoint
        self.window = window # number of previous checkpoints to store
        self.replace_freq = replace_freq # how often we want to replace the opponent
        self.self_play_prob = self_play_prob # probability to play against self
        self.checkpoints = deque(maxlen=self.window)
        self.save_steps = deque(maxlen=self.window)
        self.save_checkpoints = save_checkpoints
        self.checkpoint_dir = checkpoint_dir
        if not seed:
            seed = random.randint(0, 100000)
        self.seed = seed
        self.rnd = random.Random(seed)

        # create directory structure for saving checkpoints
        if self.save_checkpoints:
            if not os.path.exists(checkpoint_dir):
                os.makedirs(checkpoint_dir)

    def update_pool(self, agent, steps):
        # return sampled checkpoints as opponents
        if self.save_checkpoints:
            # save new checkpoint
            checkpoint_name = os.path.join(self.checkpoint_dir, f"checkpoint_{steps}.pt")
            torch.save(agent, checkpoint_name)
            self.checkpoints.append(checkpoint_name)
        else:
            # keep it in memory
            self.checkpoints.append(agent)
        self.save_steps.append(steps)

    def sample_opponent(self):
        if self.rnd.random() < self.self_play_prob:
            # choose the last ID
            checkpoint_id = len(self.checkpoints) -1
        else:
            checkpoint_id = self.rnd.randint(0, len(self.checkpoints)-1)
        print(f"sampled opponent {checkpoint_id} from timestep {self.save_steps[checkpoint_id]}")
        if self.save_checkpoints:
            agent = torch.load(self.checkpoints.get(checkpoint_id))
        else:
            agent = self.checkpoints[checkpoint_id]
        return agent

    def add_checkpoint(self, args, agent, step):
        # copies agent and add it to the pool
        # in case of step coming as a torch tensor we need to detach it
        if isinstance(step, torch.Tensor):
            step = step.item()
        if self.save_checkpoints:
            self.update_pool(agent, step)
        else:
            agent_copy = PPONet(args, envs).to(device)
            agent_copy.load_state_dict(agent.state_dict())
            self.update_pool(agent_copy, step)

def split_obs(obs, mask, filter):
    """Function used to split the observation into the player's own observation and the opponent's observation."""
    # Only used for acting - during optimisation we only work from our agent's point of view
    obs_filter = filter.unsqueeze(-1).repeat(1, obs.shape[-1])
    mask_filter = filter.unsqueeze(-1).repeat(1, mask.shape[-1])

    obs_, opp_obs = obs[obs_filter].reshape(-1, obs.shape[-1]), obs[~obs_filter].reshape(-1, obs.shape[-1])
    mask_, opp_mask = mask[mask_filter].reshape(-1, mask.shape[-1]), mask[~mask_filter].reshape(-1, mask.shape[-1])
    return (obs_, opp_obs), (mask_, opp_mask)

def merge_actions(train_ids, actions, opp_actions):
    """Function to merge back together the actions"""
    i = j = 0
    results = torch.zeros(actions.shape[0] + opp_actions.shape[0], dtype=actions.dtype)
    for id in train_ids:
        if id:
            results[i+j] = actions[i]
            i += 1
        else:
            results[i+j] = opp_actions[j]
            j += 1
    return results


def insert_at_indices(buffer, global_step, indices, values):
    # modifies buffer directly - inserts values into tensor at indices
    # used to populate the tensors during training with each env's corresponding transitions
    # buffer is [Batch, num-envs, ...]; note that len(indices) >= len(values)
    if len(values) > 0:
        j = 0
        for i in range(len(indices)):
            if indices[i]:
                buffer[global_step[i], i] = values[j]
                j += 1


def evaluate(args, agent, global_step, opponents=["random"]):
    for opponent in opponents:
        # todo maybe instead of making a new env, we could just store the eval envs
        obs_type = "vector"
        if "Sushi" in args.env_id:
            obs_type = "json"
        # could add:  randomise_order=True,
        envs = gym.vector.SyncVectorEnv(
            [make_env(args.env_id, int(global_step / args.seed) + i, opponent, args.n_players, framestack=args.framestack, obs_type=obs_type) for i in
             range(args.num_envs)]
        )
        # For environments in which the action-masks align (aka same amount of actions)
        # This wrapper will merge them all into one numpy array, instead of having an array of arrays
        envs = MergeActionMaskWrapper(envs)
        envs = RecordEpisodeStatistics(envs)

        # stats
        episodes = 0
        total_steps = 0
        rewards, lengths, outcomes = [], [], []
        wins, ties, losses = [], [], []

        start_time = time.time()
        next_obs, next_info = envs.reset()
        next_obs = torch.tensor(next_obs).to(device)
        if args.framestack > 1:
            next_obs = next_obs.view(next_obs.shape[0], -1)
        next_masks = torch.from_numpy(next_info["action_mask"]).to(device)

        while episodes < args.eval_episodes:
            total_steps += 1 * args.num_envs

            with torch.no_grad():
                # all actions are for our agent
                action, logprob, _, value = agent.get_action_and_value(next_obs, mask=next_masks)

            next_obs, reward, done, truncated, info = envs.step(action.cpu().numpy())

            # preparing for next step
            next_masks = torch.from_numpy(info["action_mask"]).to(device)
            next_obs = torch.Tensor(next_obs).to(device)
            if args.framestack > 1:
                next_obs = next_obs.view(next_obs.shape[0], -1)

            # collect stats about the episode
            if "episode" in info:
                for i in range(args.num_envs):
                    if info["_episode"][i]:
                        rewards.append(info["episode"]["r"][i])
                        lengths.append(info["episode"]["l"][i])
                        outcomes.append(info["episode"]["w"][i])
                        wins.append(info["episode"]["wins"][i])
                        ties.append(info["episode"]["ties"][i])
                        losses.append(info["episode"]["losses"][i])

                        episodes += 1
        writer.add_scalar(f"eval/{opponent}/episodic_wins", np.mean(wins), global_step)
        writer.add_scalar(f"eval/{opponent}/episodic_ties", np.mean(ties), global_step)
        writer.add_scalar(f"eval/{opponent}/episodic_losses", np.mean(losses), global_step)

        writer.add_scalar(f"eval/{opponent}/mean_return", np.mean(rewards), global_step)
        writer.add_scalar(f"eval/{opponent}/mean_length", np.mean(lengths), global_step)
        writer.add_scalar(f"eval/{opponent}/outcome", np.mean(outcomes), global_step)
        writer.add_scalar(f"eval/{opponent}/std_return", np.std(rewards), global_step)
        writer.add_scalar(f"eval/{opponent}/std_length", np.std(lengths), global_step)
        writer.add_scalar(f"eval/{opponent}/SPS", int(total_steps / (time.time() - start_time)), global_step)

def parse_args():
    # fmt: off
    parser = argparse.ArgumentParser()
    parser.add_argument("--exp-name", type=str, default=os.path.basename(__file__).rstrip(".py"),
        help="the name of this experiment")
    parser.add_argument("--logdir", type=str, default="~/data/pyTAG/",
        help="the name of this experiment")
    parser.add_argument("--seed", type=int, default=1,
        help="seed of the experiment")
    parser.add_argument("--gpu-id", type=int, default=-1,
        help="ID of the GPU to use: -1 for CPU")
    parser.add_argument("--torch-deterministic", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
        help="if toggled, `torch.backends.cudnn.deterministic=False`")
    parser.add_argument("--cuda", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
        help="if toggled, cuda will be enabled by default")
    parser.add_argument("--track", type=lambda x: bool(strtobool(x)), default=False, nargs="?", const=True,
        help="if toggled, this experiment will be tracked with Weights and Biases")
    parser.add_argument("--wandb-project-name", type=str, default="cleanRL",
        help="the wandb's project name")
    parser.add_argument("--wandb-entity", type=str, default=None,
        help="the entity (team) of wandb's project")
    parser.add_argument("--capture-video", type=lambda x: bool(strtobool(x)), default=False, nargs="?", const=True,
        help="whether to capture videos of the agent performances (check out `videos` folder)")
    # eval_freq
    parser.add_argument("--eval-freq", type=int, default=10000,
        help="Evaluation frequency")
    parser.add_argument("--eval-episodes", type=int, default=5,
        help="Evaluation episodes per setup")

    # Algorithm specific arguments
    parser.add_argument("--env-id", type=str, default="TAG/Diamant-v0",
        help="the id of the environment")
    parser.add_argument("--total-timesteps", type=int, default=1000000,
        help="total timesteps of the experiments")
    parser.add_argument("--learning-rate", type=float, default=2.5e-4,
        help="the learning rate of the optimizer")
    parser.add_argument("--num-envs", type=int, default=2,
        help="the number of parallel game environments")
    parser.add_argument("--num-steps", type=int, default=128,
        help="the number of steps to run in each environment per policy rollout")
    parser.add_argument("--anneal-lr", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
        help="Toggle learning rate annealing for policy and value networks")
    parser.add_argument("--gamma", type=float, default=0.9,
        help="the discount factor gamma")
    parser.add_argument("--gae-lambda", type=float, default=0.95,
        help="the lambda for the general advantage estimation")
    parser.add_argument("--num-minibatches", type=int, default=4,
        help="the number of mini-batches")
    parser.add_argument("--update-epochs", type=int, default=4,
        help="the K epochs to update the policy")
    parser.add_argument("--norm-adv", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
        help="Toggles advantages normalization")
    parser.add_argument("--clip-coef", type=float, default=0.2,
        help="the surrogate clipping coefficient")
    parser.add_argument("--clip-vloss", type=lambda x: bool(strtobool(x)), default=True, nargs="?", const=True,
        help="Toggles whether or not to use a clipped loss for the value function, as per the paper.")
    parser.add_argument("--ent-coef", type=float, default=0.01,
        help="coefficient of the entropy")
    parser.add_argument("--vf-coef", type=float, default=0.5,
        help="coefficient of the value function")
    parser.add_argument("--max-grad-norm", type=float, default=0.5,
        help="the maximum norm for the gradient clipping")
    parser.add_argument("--target-kl", type=float, default=None,
        help="the target KL divergence threshold")
    # game related args
    parser.add_argument('--opponent', type=str, default='random', choices=["random", "osla", "mcts"])
    parser.add_argument("--n-players", type=int, default=2,
        help="the number of players in the env (note some games only support certain number of players)")
    parser.add_argument("--framestack", type=int, default=1)

    # self-play args
    parser.add_argument("--sp-checkpoint-freq", type=int, default=int(5e4))
    parser.add_argument("--sp-window", type=int, default=10, help="Number of checkpoints to store in memory")
    parser.add_argument("--sp-replace-freq", type=int, default=int(1e4), help="How often to replace the opponent")
    parser.add_argument("--sp-recent-prob", type=float, default=0.7, help="Probability of playing against most recent checkpoint")
    parser.add_argument("--sp-save-checkpoints", type=lambda x: bool(strtobool(x)), default=False, nargs="?", const=True, help="Whether to save checkpoints to disk instead of keeping them in memory")

    args = parser.parse_args()
    args.batch_size = int(args.num_envs * args.num_steps)
    args.minibatch_size = int(args.batch_size // args.num_minibatches)
    # fmt: on
    return args


if __name__ == "__main__":
    args = parse_args()
    run_name = f"{args.env_id}__{args.exp_name}__{args.seed}__{int(time.time())}"
    if args.track:
        import wandb

        wandb.init(
            project=args.wandb_project_name,
            entity=args.wandb_entity,
            sync_tensorboard=True,
            config=vars(args),
            name=run_name,
            monitor_gym=True,
            save_code=True,
        )
    args.logdir = os.path.expanduser(args.logdir)
    results_dir = os.path.join(args.logdir, run_name)
    if not os.path.exists(results_dir):
        os.makedirs(results_dir)
    writer = SummaryWriter(f"{results_dir}")
    writer.add_text(
        "hyperparameters",
        "|param|value|\n|-|-|\n%s" % ("\n".join([f"|{key}|{value}|" for key, value in vars(args).items()])),
    )

    # TRY NOT TO MODIFY: seeding
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.backends.cudnn.deterministic = args.torch_deterministic

    if torch.cuda.is_available() and args.gpu_id != -1:
        device = torch.device(f'cuda:{args.gpu_id}')
        torch.cuda.manual_seed(seed=args.seed)
    else:
        device = torch.device('cpu')

    # env setup
    obs_type = "vector"
    if "Sushi" in args.env_id:
        obs_type = "json"
    envs = gym.vector.SyncVectorEnv(
        [make_sp_env(args.env_id, args.seed + i, args.n_players, framestack=args.framestack, randomise_order=True, obs_type=obs_type) for i in range(args.num_envs)]
    )
    # For environments in which the action-masks align (aka same amount of actions)
    # This wrapper will merge them all into one numpy array, instead of having an array of arrays
    envs = MergeActionMaskWrapper(envs)
    envs = RecordSelfPlayEpStats(envs)

    agent = PPONet(args, envs).to(device)
    optimizer = optim.Adam(agent.parameters(), lr=args.learning_rate, eps=1e-5)

    training_manager = SelfPlayAssistant(window=args.sp_window, checkpoint_freq=args.sp_checkpoint_freq,
                                         replace_freq=args.sp_replace_freq, self_play_prob=args.sp_recent_prob,
                                         save_checkpoints=args.sp_save_checkpoints, seed=args.seed)
    # add and "sample" the first checkpoint
    training_manager.add_checkpoint(args, agent, 0)
    opponent = training_manager.sample_opponent()

    # ALGO Logic: Storage setup
    if args.framestack > 1:
        obs = torch.zeros((args.num_steps, args.num_envs) + (np.array(envs.single_observation_space.shape).prod(),)).to(device)
    else:
        obs = torch.zeros((args.num_steps, args.num_envs) + envs.single_observation_space.shape).to(device)
    masks = torch.zeros((args.num_steps, args.num_envs, envs.single_action_space.n), dtype=torch.bool).to(device)
    actions = torch.zeros((args.num_steps, args.num_envs) + envs.single_action_space.shape).to(device)
    logprobs = torch.zeros((args.num_steps, args.num_envs)).to(device)
    rewards = torch.zeros((args.num_steps, args.num_envs)).to(device)
    dones = torch.zeros((args.num_steps, args.num_envs)).to(device)
    values = torch.zeros((args.num_steps, args.num_envs)).to(device)

    # TRY NOT TO MODIFY: start the game
    global_step = 0
    start_time = time.time()
    next_obs, next_info = envs.reset()
    next_obs = torch.tensor(next_obs).to(device)
    if args.framestack > 1:
        next_obs = next_obs.view(next_obs.shape[0], -1)
    next_masks = torch.from_numpy(next_info["action_mask"]).to(device)
    learning_id = torch.from_numpy(next_info["learning_player"]).to(device)
    player_id = torch.from_numpy(next_info["player_id"]).to(device)
    train_ids = (learning_id == player_id).int()
    prev_train_idx = learning_id

    step = 0
    steps = torch.zeros(args.num_envs, dtype=torch.int32).to(device)
    while global_step < args.total_timesteps:

        if args.anneal_lr:
            # Annealing the rate if instructed to do so.
            frac = 1.0 - (global_step - 1.0) / args.total_timesteps
            lrnow = frac * args.learning_rate
            optimizer.param_groups[0]["lr"] = lrnow

        while step < args.num_steps:
            # note that step is max(steps) so if any of the envs reach step we stop! - we don't wait to fill up all the transitions
            # step is not a scalar value - but rather trajectory length for each training agent
            # approach: split and merge observations depending on who needs to act
            # update step and the trajectories where training_id is not zero

            # ALGO LOGIC: action logic
            action = opp_action = torch.zeros(0) # just a placeholder
            logprob = opp_logprob = torch.zeros(0)
            with torch.no_grad():
                (next_obs, opp_obs), (next_mask, opp_mask) = split_obs(next_obs, next_masks, filter=(learning_id == player_id))
                if len(next_obs > 0):
                    # global step only counts where our training agent is acting
                    global_step += sum(train_ids).item()

                    # self-play agent acting
                    insert_at_indices(obs, steps, train_ids, next_obs)
                    action, logprob, _, value = agent.get_action_and_value(next_obs, mask=next_mask)
                    insert_at_indices(values, steps, train_ids, value.flatten())
                if (len(opp_obs > 0)):
                    opp_action, opp_logprob, _, value = opponent.get_action_and_value(opp_obs, mask=opp_mask)

                # self-play admin
                if global_step % training_manager.checkpoint_freq < sum(train_ids):
                    training_manager.add_checkpoint(args, agent, global_step)
                if global_step % training_manager.replace_freq < sum(train_ids):
                    opponent = training_manager.sample_opponent()

                # modified
                if len(next_obs > 0):
                    # merge back actions and logprobs
                    action_ = merge_actions(train_ids, action, opp_action)
                    insert_at_indices(actions, steps, train_ids, action)
                    insert_at_indices(logprobs, steps, train_ids, logprob)
                    insert_at_indices(masks, steps, train_ids, next_mask)
                else:
                    action_ = opp_action

            # merge the actions back together
            next_obs, reward, done, truncated, info = envs.step(action_.cpu().numpy())

            next_masks = torch.from_numpy(info["action_mask"]).to(device)
            reward = torch.tensor(reward).to(device)
            done = torch.Tensor(done).to(device)

            # due to the vec env the player ids are resampled at the next episode - so we need the previous player id
            # when we win - we get reward instantly
            insert_at_indices(rewards, steps, train_ids, reward)
            insert_at_indices(dones, steps, train_ids, done)

            # if we lose during opponent's turn we need to take a step back to allocate the reward correctly
            # note that only the learning player gets reward from the SP env
            insert_at_indices(rewards, steps-1, torch.logical_and(done, ~train_ids.bool()).int(), reward)
            insert_at_indices(dones, steps-1, torch.logical_and(done, ~train_ids.bool()).int(), done)

            next_obs = torch.Tensor(next_obs).to(device)
            if args.framestack > 1:
                next_obs = next_obs.view(next_obs.shape[0], -1)

            # keep track of the steps
            steps += train_ids
            step = steps.max()
            prev_train_idx = learning_id

            learning_id = torch.from_numpy(info["learning_player"]).to(device)
            player_id = torch.from_numpy(info["player_id"]).to(device)
            train_ids = (learning_id == player_id).int()

            if "episode" in info:
                for i in range(args.num_envs):
                    if info["_episode"][i]:
                        writer.add_scalar("charts/episodic_wins", info["episode"]["wins"][i], global_step)
                        writer.add_scalar("charts/episodic_ties", info["episode"]["ties"][i], global_step)
                        writer.add_scalar("charts/episodic_losses", info["episode"]["losses"][i], global_step)

                        writer.add_scalar("charts/episodic_return", info["episode"]["r"][i], global_step)
                        writer.add_scalar("charts/episodic_length", info["episode"]["l"][i], global_step)
                        writer.add_scalar("charts/total_ep_length", info["episode"]["total_l"][i], global_step)
                        writer.add_scalar("charts/episodic_outcomes", info["episode"]["w"][i], global_step) # [-1, 1]

            # if we have just passed this point then we evaluate
            with torch.no_grad():
                if global_step % args.eval_freq <= sum(train_ids).item():
                    evaluate(args, agent, global_step, opponents=["random", "osla", "mcts"])

        # bootstrap value if not done
        # update starts here: we want to take the final observation where the training agent was used for acting
        with torch.no_grad():
            advantages = torch.zeros_like(rewards).to(device)
            lastgaelam = 0
            # last step is not observed (128) and for bootstrapping we need 127
            # the last step is used with t+1, so we are not losing anything
            for t in reversed(range(args.num_steps - 2)):
                # with (steps > t).int() we filter out the incorrect values
                nextnonterminal = 1.0 - dones[t + 1]
                nextvalues = values[t + 1] * (steps > t).int()
                delta = rewards[t] + args.gamma * nextvalues * nextnonterminal - values[t]
                advantages[t] = lastgaelam = delta + args.gamma * args.gae_lambda * nextnonterminal * lastgaelam
            returns = advantages + values

        # todo add back support for framestacking
        # if args.framestack > 1:
        #     obs = torch.zeros(
        #         (args.num_steps, args.num_envs) + (np.array(envs.single_observation_space.shape).prod(),)).to(device)
        # else:
        n_transitions = torch.sum(steps)
        b_obs = torch.zeros((n_transitions,) + envs.single_observation_space.shape).to(device)
        b_logprobs = torch.zeros((n_transitions)).to(device)
        b_actions = torch.zeros((n_transitions,) + envs.single_action_space.shape).to(device)
        b_masks = torch.zeros((n_transitions, envs.single_action_space.n), dtype=torch.bool).to(device)
        b_advantages = torch.zeros((n_transitions)).to(device)
        b_returns = torch.zeros((n_transitions)).to(device)
        b_values = torch.zeros((n_transitions)).to(device)

        # flatten the batch and cut-off the trajectories
        j = 0
        for i in range(len(steps)):
            j += 0 if i == 0 else steps[i - 1]
            b_obs[j:j+steps[i]] = obs[:steps[i], i]
            b_logprobs[j:j+steps[i]] = logprobs[:steps[i], i]
            b_actions[j:j+steps[i]] = actions[:steps[i], i]
            b_masks[j:j+steps[i]] = masks[:steps[i], i]
            b_advantages[j:j+steps[i]] = advantages[:steps[i], i]
            b_returns[j:j+steps[i]] = returns[:steps[i], i]
            b_values[j:j+steps[i]] = values[:steps[i], i]

        # Optimizing the policy and value network
        b_inds = np.arange(n_transitions)
        clipfracs = []
        for epoch in range(args.update_epochs):
            np.random.shuffle(b_inds)
            for start in range(0, n_transitions, args.minibatch_size):
                end = start + args.minibatch_size
                mb_inds = b_inds[start:end]

                _, newlogprob, entropy, newvalue = agent.get_action_and_value(b_obs[mb_inds], b_actions.long()[mb_inds], mask=b_masks[mb_inds])
                logratio = newlogprob - b_logprobs[mb_inds]
                ratio = logratio.exp()

                with torch.no_grad():
                    # calculate approx_kl http://joschu.net/blog/kl-approx.html
                    old_approx_kl = (-logratio).mean()
                    approx_kl = ((ratio - 1) - logratio).mean()
                    clipfracs += [((ratio - 1.0).abs() > args.clip_coef).float().mean().item()]

                mb_advantages = b_advantages[mb_inds]
                if args.norm_adv:
                    mb_advantages = (mb_advantages - mb_advantages.mean()) / (mb_advantages.std() + 1e-8)

                # Policy loss
                pg_loss1 = -mb_advantages * ratio
                pg_loss2 = -mb_advantages * torch.clamp(ratio, 1 - args.clip_coef, 1 + args.clip_coef)
                pg_loss = torch.max(pg_loss1, pg_loss2).mean()

                # Value loss
                newvalue = newvalue.view(-1)
                if args.clip_vloss:
                    v_loss_unclipped = (newvalue - b_returns[mb_inds]) ** 2
                    v_clipped = b_values[mb_inds] + torch.clamp(
                        newvalue - b_values[mb_inds],
                        -args.clip_coef,
                        args.clip_coef,
                    )
                    v_loss_clipped = (v_clipped - b_returns[mb_inds]) ** 2
                    v_loss_max = torch.max(v_loss_unclipped, v_loss_clipped)
                    v_loss = 0.5 * v_loss_max.mean()
                else:
                    v_loss = 0.5 * ((newvalue - b_returns[mb_inds]) ** 2).mean()

                entropy_loss = entropy.mean()
                loss = pg_loss - args.ent_coef * entropy_loss + v_loss * args.vf_coef

                optimizer.zero_grad()
                loss.backward()
                nn.utils.clip_grad_norm_(agent.parameters(), args.max_grad_norm)
                optimizer.step()

            if args.target_kl is not None:
                if approx_kl > args.target_kl:
                    break

        y_pred, y_true = b_values.cpu().numpy(), b_returns.cpu().numpy()
        var_y = np.var(y_true)
        explained_var = np.nan if var_y == 0 else 1 - np.var(y_true - y_pred) / var_y

        # TRY NOT TO MODIFY: record rewards for plotting purposes

        writer.add_scalar("charts/learning_rate", optimizer.param_groups[0]["lr"], global_step)
        writer.add_scalar("losses/value_loss", v_loss.item(), global_step)
        writer.add_scalar("losses/policy_loss", pg_loss.item(), global_step)
        writer.add_scalar("losses/entropy", entropy_loss.item(), global_step)
        writer.add_scalar("losses/old_approx_kl", old_approx_kl.item(), global_step)
        writer.add_scalar("losses/approx_kl", approx_kl.item(), global_step)
        writer.add_scalar("losses/clipfrac", np.mean(clipfracs), global_step)
        writer.add_scalar("losses/explained_variance", explained_var, global_step)
        # print("SPS:", int(global_step / (time.time() - start_time)))
        writer.add_scalar("charts/SPS", int(global_step / (time.time() - start_time)), global_step)



        # reset counters
        step = 0
        steps = torch.zeros(args.num_envs, dtype=torch.int32).to(device)

    # create checkpoint
    torch.save(agent.state_dict(), f"{results_dir}/agent.pt")
    if args.track:
        wandb.save(f"{results_dir}/agent.pt", policy="end")
    envs.close()
    writer.close()