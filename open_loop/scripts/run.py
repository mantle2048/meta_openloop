# +
import numpy as np
import torch
import time
import gym
import random

import open_loop

from typing import Dict
from pyvirtualdisplay import Display
from matplotlib import pyplot as plt

from reRLs.infrastructure.utils import pytorch_util as ptu
from reRLs.infrastructure.utils.utils import Path, get_pathlength, write_gif
from reRLs.infrastructure.loggers import setup_logger

from es import CMAES, OpenES

from open_loop.meta_openloop import CpgRbfNet
import open_loop.user_config as conf
from open_loop.envs.wrappers.trajectory_generator_wrapper_env import TrajectoryGeneratorWrapperEnv

# %matplotlib notebook
# %reload_ext autoreload
# %autoreload 2
# -
def make_env(env_name, seed):
    env = gym.make(env_name)
    env.reset(seed=seed)
    env.action_space.seed(seed)
    return env

def rollout(env, render=False):
    obs = env.reset()
    obss, acts, rews, next_obss, terminals, image_obss = [], [], [], [], [], []

    for step in range(200):
        if render:
            if hasattr(env, 'sim'):
                image_obss.append(env.sim.render(camera_name='track', height=500, width=500)[::-1])
            else:
                image_obss.append(env.render(mode='rbg_array'))
        obss.append(obs)

        # act is dummy for traj generator env
        act = env.action_space.sample()
        acts.append(act)

        next_obs, rew, done, _ = env.step(act)

        rews.append(rew)
        next_obss.append(next_obs)

        rollout_done = done
        terminals.append(rollout_done)

        if rollout_done:
            break

    return Path(obss, image_obss, acts, rews, next_obss, terminals)



class Traj_Trainer():

    def __init__(self, config: Dict):
        self.config = config

        self.env = make_env(config['env_name'], config['seed'])
        self.config['num_act'] = self.env.action_space.shape[0]
        self.config['timestep'] = self.env.dt

        sin_config = {
            'amplitude': config['amplitude'],
            'theta': config['theta'],
            'frequency': config['frequency'],
        }
        self.trajectory_generator = CpgRbfNet(
            sin_config, config['timestep'], config['num_rbf'], config['num_act']
        )

        self.env = TrajectoryGeneratorWrapperEnv(
            self.env, self.trajectory_generator
        )

        self.logger = setup_logger(
            exp_prefix=config['exp_prefix'],
            seed=config['seed'],
            exp_id=config['exp_id'],
            snapshot_mode=config['snapshot_mode'],
            base_log_dir=config['base_log_dir']
        )

        self.virtual_disp = Display(visible=False, size=(1400,900))
        self.virtual_disp.start()

        self.es_solver = CMAES(
            num_params=self.trajectory_generator.num_params,
            popsize=self.config['popsize'],
            sigma_init=0.01,
            weight_decay=0.01
        )

        # self.es_solver = OpenES(
        #     num_params=self.trajectory_generator.num_params,
        #     popsize=self.config['popsize'],
        #     sigma_init=0.005,
        #     sigma_decay=0.99,
        #     sigma_limit=0.001,
        #     learning_rate = 0.003,
        #     learning_rate_decay = 0.999,
        #     learning_rate_limit = 0.001,
        #     weight_decay = 0.01,
        #     rank_fitness = False,
        #     forget_best = True,
        # )

        # Set random seed (must be set after es_sovler)
        seed = self.config['seed']
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)

        # simulation timestep, will be used for video saving
        self.fps=30
        self.config['fps'] = self.fps

    def run_trainning_loop(self, n_itr):

        self.start_time = time.time()
        self.total_steps = 0

        for itr in range(n_itr):

            ## decide if videos should be rendered/logged at this iteration
            if self.config['video_log_freq'] != -1 \
                    and itr % self.config['video_log_freq'] == 0:
                self.logvideo = True
            else:
                self.logvideo = False

            ## decide if tabular should be logged
            if self.config['tabular_log_freq'] != -1 \
                    and itr % self.config['tabular_log_freq'] == 0:
                self.logtabular = True
            else:
                self.logtabular = False

            solutions = self.es_solver.ask()
            fitness_list = []
            paths = []
            for i in range(self.es_solver.popsize):
                self.trajectory_generator.set_flat_weight(solutions[i])
                path = rollout(self.env)
                paths.append(path)
                fitness_list.append(path['rew'].sum())
                self.total_steps += get_pathlength(path)
            self.es_solver.tell(fitness_list)

            # first element is the best solution, second element is the best fitness
            best_param, best_fitness, _, _ = self.es_solver.result()

            if self.logtabular:
                self.perform_logging(itr, best_param, best_fitness, paths)

                if self.config['save_params']:
                    self.logger.save_itr_params(itr, self.trajectory_generator.get_state())

        self.env.close()
        self.logger.close()

    def perform_logging(self, itr, best_param, best_fitness, paths):

        if itr == 0:
            self.logger.log_variant('config.json', self.config)

        self.trajectory_generator.set_flat_weight(best_param)

        eval_paths = []
        for _ in range(10):
            eval_paths.append(rollout(self.env))

        if self.logvideo:
            video_paths = [rollout(self.env, render=True) for _ in range(2)]

            self.logger.log_paths_as_videos(
                video_paths, itr, fps=self.fps, video_title='rollout'
            )

            fig = plt.figure(figsize=(6,4))
            ax_1 = self.trajectory_generator.cpg.plot_curve(fig.add_subplot(121))
            ax_2 = self.trajectory_generator.plot_curve(fig.add_subplot(122))
            self.logger.log_figure(fig, 'trajectory_curve', itr)

        train_ep_lens = [get_pathlength(path) for path in paths]
        eval_ep_lens = [get_pathlength(path) for path in eval_paths]
        train_returns = [path["rew"].sum() for path in paths]
        eval_returns = [path["rew"].sum() for path in eval_paths]

        self.logger.record_tabular("Itr", itr)

        self.logger.record_tabular_misc_stat("TrainReward", train_returns)
        self.logger.record_tabular_misc_stat("EvalReward", eval_returns)
        self.logger.record_tabular("TotalEnvInteracts", self.total_steps)
        self.logger.record_tabular("BestReturn", best_fitness)
        self.logger.record_tabular("TrainEpLen", np.mean(train_ep_lens))
        self.logger.record_tabular("EvalEpLen", np.mean(eval_ep_lens))
        self.logger.record_tabular("Time", (time.time() - self.start_time) / 60)

        self.logger.dump_tabular(with_prefix=True, with_timestamp=False)

def get_parser():

    import argparse
    parser = argparse.ArgumentParser()

    # exp args
    parser.add_argument('--env_name', type=str, default='HalfCheetah-v3')

    # logger args
    parser.add_argument('--exp_prefix', type=str, default='Traj_HalfCheetah-v3')
    parser.add_argument('--exp_id', type=int, default=0)
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--snapshot_mode', type=str, default="last")
    parser.add_argument('--base_log_dir', type=str, default=f"{conf.LOCAL_LOG_DIR}")

    # train args
    parser.add_argument('--n_itr', '-n', type=int, default=10)
    parser.add_argument('--video_log_freq', type=int, default=-1)
    parser.add_argument('--tabular_log_freq', type=int, default=1)
    parser.add_argument('--save_params', action='store_true')

    # cpg_rbf args
    parser.add_argument('--amplitude', '-A', type=float, default=0.2)
    parser.add_argument('--theta', type=float, default=-0.5*np.pi)
    parser.add_argument('--frequency', type=float, default=1.0)
    parser.add_argument('--num_rbf', type=int, default=20)

    # es args
    parser.add_argument('--popsize', type=int, default=10)

    return parser

def main():

    parser = get_parser()
    args = parser.parse_args()

    config = vars(args)

    trainer = Traj_Trainer(config)
    trainer.run_trainning_loop(config['n_itr'])

if __name__ == '__main__':
    main()

