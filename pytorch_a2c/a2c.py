#!/usr/bin/env python3

import copy
import glob
import os
import time
import operator
from functools import reduce

import gym
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.autograd import Variable

from .arguments import get_args
from .vec_env.dummy_vec_env import DummyVecEnv
from .vec_env.subproc_vec_env import SubprocVecEnv
from .envs import make_env
from .model import Policy
from .storage import RolloutStorage

os.environ['OMP_NUM_THREADS'] = '1'

def train(
    args, # TODO: get rid of this argument
    envs,
    actor_critic,
    num_updates,
    num_processes=32
):


    obs_shape = envs.observation_space.shape
    obs_numel = reduce(operator.mul, obs_shape, 1)

    assert envs.action_space.__class__.__name__ == "Discrete"
    action_shape = 1




    if args.cuda:
        actor_critic.cuda()

    optimizer = optim.RMSprop(actor_critic.parameters(), args.lr, eps=args.eps, alpha=args.alpha)

    rollouts = RolloutStorage(args.num_steps, args.num_processes, obs_shape, envs.action_space, actor_critic.state_size)
    current_obs = torch.zeros(args.num_processes, *obs_shape)

    def update_current_obs(obs):
        shape_dim0 = envs.observation_space.shape[0]
        obs = torch.from_numpy(obs).float()
        current_obs[:, -shape_dim0:] = obs

    obs = envs.reset()
    update_current_obs(obs)
    rollouts.observations[0].copy_(current_obs)

    # These variables are used to compute average rewards for all processes.
    episode_rewards = torch.zeros([args.num_processes, 1])
    final_rewards = torch.zeros([args.num_processes, 1])

    if args.cuda:
        current_obs = current_obs.cuda()
        rollouts.cuda()

    start = time.time()
    for j in range(num_updates):
        for step in range(args.num_steps):
            # Sample actions
            value, action, action_log_prob, states = actor_critic.act(
                Variable(rollouts.observations[step], volatile=True),
                Variable(rollouts.states[step], volatile=True),
                Variable(rollouts.masks[step], volatile=True)
            )
            cpu_actions = action.data.squeeze(1).cpu().numpy()

            # Obser reward and next obs
            obs, reward, done, info = envs.step(cpu_actions)
            reward = torch.from_numpy(np.expand_dims(np.stack(reward), 1)).float()
            episode_rewards += reward

            # If done then clean the history of observations.
            masks = torch.FloatTensor([[0.0] if done_ else [1.0] for done_ in done])
            final_rewards *= masks
            final_rewards += (1 - masks) * episode_rewards
            episode_rewards *= masks

            if args.cuda:
                masks = masks.cuda()

            if current_obs.dim() == 4:
                current_obs *= masks.unsqueeze(2).unsqueeze(2)
            elif current_obs.dim() == 3:
                current_obs *= masks.unsqueeze(2)
            else:
                current_obs *= masks

            update_current_obs(obs)
            rollouts.insert(step, current_obs, states.data, action.data, action_log_prob.data, value.data, reward, masks)

        next_value = actor_critic(
            Variable(rollouts.observations[-1], volatile=True),
            Variable(rollouts.states[-1], volatile=True),
            Variable(rollouts.masks[-1], volatile=True)
        )[0].data

        rollouts.compute_returns(next_value, args.use_gae, args.gamma, args.tau)

        values, action_log_probs, dist_entropy, states = actor_critic.evaluate_actions(
            Variable(rollouts.observations[:-1].view(-1, *obs_shape)),
            Variable(rollouts.states[:-1].view(-1, actor_critic.state_size)),
            Variable(rollouts.masks[:-1].view(-1, 1)),
            Variable(rollouts.actions.view(-1, action_shape))
        )

        values = values.view(args.num_steps, num_processes, 1)
        action_log_probs = action_log_probs.view(args.num_steps, num_processes, 1)

        advantages = Variable(rollouts.returns[:-1]) - values
        value_loss = advantages.pow(2).mean()

        action_loss = -(Variable(advantages.data) * action_log_probs).mean()

        optimizer.zero_grad()
        (value_loss * args.value_loss_coef + action_loss - dist_entropy * args.entropy_coef).backward()

        nn.utils.clip_grad_norm(actor_critic.parameters(), args.max_grad_norm)

        optimizer.step()

        rollouts.after_update()

        if j % args.save_interval == 0 and args.save_dir != "":
            save_path = os.path.join(args.save_dir)
            try:
                os.makedirs(save_path)
            except OSError:
                pass

            # A really ugly way to save a model to CPU
            save_model = actor_critic
            if args.cuda:
                save_model = copy.deepcopy(actor_critic).cpu()

            save_model = [save_model, hasattr(envs, 'ob_rms') and envs.ob_rms or None]

            torch.save(save_model, os.path.join(save_path, args.env_name + ".pt"))

        if j % args.log_interval == 0:
            end = time.time()
            total_num_steps = (j + 1) * args.num_processes * args.num_steps
            print("Updates {}, num timesteps {}, FPS {}, mean/median reward {:.1f}/{:.1f}, min/max reward {:.1f}/{:.1f}, entropy {:.5f}, value loss {:.5f}, policy loss {:.5f}".
                format(j, total_num_steps,
                       int(total_num_steps / (end - start)),
                       final_rewards.mean(),
                       final_rewards.median(),
                       final_rewards.min(),
                       final_rewards.max(), dist_entropy.data[0],
                       value_loss.data[0], action_loss.data[0]))
