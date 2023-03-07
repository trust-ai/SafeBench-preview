''' 
Date: 2023-01-31 22:23:17
LastEditTime: 2023-03-06 00:19:51
Description: 
    Copyright (c) 2022-2023 Safebench Team

    This work is licensed under the terms of the MIT license.
    For a copy, see <https://opensource.org/licenses/MIT>
'''

import copy

import numpy as np
import carla
import pygame
from tqdm import tqdm

from safebench.gym_carla.env_wrapper import VectorWrapper
from safebench.gym_carla.envs.render import BirdeyeRender
from safebench.gym_carla.replay_buffer import RouteReplayBuffer, PerceptionReplayBuffer

from safebench.agent import AGENT_POLICY_LIST
from safebench.scenario import SCENARIO_POLICY_LIST

from safebench.scenario.scenario_manager.carla_data_provider import CarlaDataProvider
from safebench.scenario.scenario_data_loader import ScenarioDataLoader, ScenicDataLoader
from safebench.scenario.tools.scenario_utils import scenario_parse, scenic_parse

from safebench.util.logger import Logger, setup_logger_kwargs
from safebench.util.run_util import VideoRecorder
from safebench.util.metric_util import get_route_scores, get_perception_scores
from safebench.util.scenic_utils import ScenicSimulator

class ScenicRunner:
    def __init__(self, agent_config, scenario_config):
        self.scenario_config = scenario_config
        self.agent_config = agent_config

        self.seed = scenario_config['seed']
        self.exp_name = scenario_config['exp_name']
        self.output_dir = scenario_config['output_dir']
        self.mode = scenario_config['mode']

        self.render = scenario_config['render']
        self.num_scenario = scenario_config['num_scenario']
        self.fixed_delta_seconds = scenario_config['fixed_delta_seconds']
        self.scenario_category = scenario_config['type_category']
        self.scenario_policy_type = scenario_config['type_name'].split('.')[0]

        # continue training flag
        self.continue_agent_training = scenario_config['continue_agent_training']
        self.continue_scenario_training = scenario_config['continue_scenario_training']

        # apply settings to carla
        self.client = carla.Client('localhost', scenario_config['port'])
        self.client.set_timeout(10.0)
        self.world = None
        self.env = None

        self.env_params = {
            'auto_ego': scenario_config['auto_ego'],
            'obs_type': agent_config['obs_type'],
            'scenario_category': self.scenario_category,
            'ROOT_DIR': scenario_config['ROOT_DIR'],
            'disable_lidar': True,                                     # show bird-eye view lidar or not
            'display_size': 128,                                       # screen size of one bird-eye view windowd=
            'obs_range': 32,                                           # observation range (meter)
            'd_behind': 12,                                            # distance behind the ego vehicle (meter)
            'max_past_step': 1,                                        # the number of past steps to draw
            'discrete': False,                                         # whether to use discrete control space
            'discrete_acc': [-3.0, 0.0, 3.0],                          # discrete value of accelerations
            'discrete_steer': [-0.2, 0.0, 0.2],                        # discrete value of steering angles
            'continuous_accel_range': [-3.0, 3.0],                     # continuous acceleration range
            'continuous_steer_range': [-0.3, 0.3],                     # continuous steering angle range
            'max_episode_step': scenario_config['max_episode_step'],   # maximum timesteps per episode
            'max_waypt': 12,                                           # maximum number of waypoints
            'lidar_bin': 0.125,                                        # bin size of lidar sensor (meter)
            'out_lane_thres': 4,                                       # threshold for out of lane (meter)
            'desired_speed': 8,                                        # desired speed (m/s)
            'image_sz': 1024,                                          # TODO: move to config of od scenario
        }

        # pass config from scenario to agent
        agent_config['mode'] = scenario_config['mode']
        agent_config['ego_action_dim'] = scenario_config['ego_action_dim']
        agent_config['ego_state_dim'] = scenario_config['ego_state_dim']
        agent_config['ego_action_limit'] = scenario_config['ego_action_limit']
 
        # define logger
        logger_kwargs = setup_logger_kwargs(self.exp_name, self.output_dir, self.seed)
        self.logger = Logger(**logger_kwargs)
        
        # prepare parameters
        if self.mode == 'train_agent':
            self.buffer_capacity = agent_config['buffer_capacity']
            self.eval_in_train_freq = agent_config['eval_in_train_freq']
            self.save_freq = agent_config['save_freq']
            self.train_episode = agent_config['train_episode']
            self.logger.save_config(agent_config)
        elif self.mode == 'train_scenario':
            self.buffer_capacity = scenario_config['buffer_capacity']
            self.eval_in_train_freq = scenario_config['eval_in_train_freq']
            self.save_freq = scenario_config['save_freq']
            self.train_episode = scenario_config['train_episode']
            self.logger.save_config(scenario_config)
        elif self.mode == 'eval':
            self.logger.log('>> Evaluation Mode, skip config saving', 'yellow')
            self.logger.create_eval_dir(load_existing_results=True)
        else:
            raise NotImplementedError(f"Unsupported mode: {self.mode}.")

        # define agent and scenario
        self.logger.log('>> Agent Policy: ' + agent_config['policy_type'])
        self.logger.log('>> Scenario Policy: ' + self.scenario_policy_type)

        if self.scenario_config['auto_ego']:
            self.logger.log('>> Using auto-polit for ego vehicle, the action of agent policy will be ignored', 'yellow')
        if self.scenario_policy_type == 'odrinary' and self.mode != 'train_agent':
            self.logger.log('>> Ordinary scenario can only be used in agent training', 'red')
            raise Exception()
        self.logger.log('>> ' + '-' * 40)

        # define agent and scenario policy
        self.agent_policy = AGENT_POLICY_LIST[agent_config['policy_type']](agent_config, logger=self.logger)
        self.scenario_policy = SCENARIO_POLICY_LIST[self.scenario_policy_type](scenario_config, logger=self.logger)
        self.video_recorder = VideoRecorder(scenario_config, logger=self.logger)

    def _init_world(self):
        self.logger.log(">> Initializing carla world")
        self.update_scene()
        self.world = self.client.get_world()
        self.world.scenic = self.scenic
        CarlaDataProvider.set_client(self.client)
        CarlaDataProvider.set_world(self.world)
        CarlaDataProvider.set_traffic_manager_port(self.scenario_config['tm_port'])
        
    def _init_scenic(self, config):
        self.logger.log(f">> Initializing scenic simulator: {config.scenic_file}")
        self.scenic = ScenicSimulator(config.scenic_file)
        
    def update_scene(self):
        self.logger.log(f">> Updating the scene...")
        while(True):
            scene, _ = self.scenic.generateScene()
            if self.set_scene(scene):
                break
                
    def set_scene(self, scene):
        if self.scenic.setSimulation(scene):
            self.scene = scene
            return True
        return False
                
    def run_scene(self):
        self.logger.log(f">> Begin to run the scene...")
        self.scenic.update_behavior = self.scenic.runSimulation()
        next(self.scenic.update_behavior)
        
    def _init_renderer(self):
        self.logger.log(">> Initializing pygame birdeye renderer")
        pygame.init()
        flag = pygame.HWSURFACE | pygame.DOUBLEBUF
        if not self.render:
            flag = flag | pygame.HIDDEN
        if self.scenario_category in ['planning', 'scenic']: 
            # [bird-eye view, Lidar, front view] or [bird-eye view, front view]
            if self.env_params['disable_lidar']:
                window_size = (self.env_params['display_size'] * 2, self.env_params['display_size'] * self.num_scenario)
            else:
                window_size = (self.env_params['display_size'] * 3, self.env_params['display_size'] * self.num_scenario)
        else:
            window_size = (self.env_params['display_size'], self.env_params['display_size'] * self.num_scenario)
        self.display = pygame.display.set_mode(window_size, flag)

        # initialize the render for generating observation and visualization
        pixels_per_meter = self.env_params['display_size'] / self.env_params['obs_range']
        pixels_ahead_vehicle = (self.env_params['obs_range'] / 2 - self.env_params['d_behind']) * pixels_per_meter
        self.birdeye_params = {
            'screen_size': [self.env_params['display_size'], self.env_params['display_size']],
            'pixels_per_meter': pixels_per_meter,
            'pixels_ahead_vehicle': pixels_ahead_vehicle,
        }
        self.birdeye_render = BirdeyeRender(self.world, self.birdeye_params, logger=self.logger)

    def train(self, data_loader, start_episode=0):
        # general buffer for both agent and scenario
        Buffer = RouteReplayBuffer if self.scenario_category in ['planning', 'scenic'] else PerceptionReplayBuffer
        replay_buffer = Buffer(self.num_scenario, self.mode, self.buffer_capacity)

        for e_i in tqdm(range(start_episode, self.train_episode)):
            # sample scenarios
            sampled_scenario_configs, _ = data_loader.sampler()
            # TODO: to restart the data loader, reset the index counter every time
            data_loader.reset_idx_counter()

            # get static obs and then reset with init action 
            static_obs = self.env.get_static_obs(sampled_scenario_configs)
            scenario_init_action, additional_dict = self.scenario_policy.get_init_action(static_obs)
            obs, infos = self.env.reset(sampled_scenario_configs, scenario_init_action)
            replay_buffer.store_init([static_obs, scenario_init_action], additional_dict=additional_dict)

            # get ego vehicle from scenario
            self.agent_policy.set_ego_and_route(self.env.get_ego_vehicles(), infos)

            # start loop
            while not self.env.all_scenario_done():
                # get action from agent policy and scenario policy (assume using one batch)
                ego_actions = self.agent_policy.get_action(obs, infos, deterministic=False)
                scenario_actions = self.scenario_policy.get_action(obs, infos, deterministic=False)

                # apply action to env and get obs
                next_obs, rewards, dones, infos = self.env.step(ego_actions=ego_actions, scenario_actions=scenario_actions)
                replay_buffer.store([ego_actions, scenario_actions, obs, next_obs, rewards, dones], additional_dict=infos)
                obs = copy.deepcopy(next_obs)

                # train on-policy agent or scenario
                if self.mode == 'train_agent' and self.agent_policy.type == 'offpolicy':
                    self.agent_policy.train(replay_buffer)
                elif self.mode == 'train_scenario' and self.scenario_policy.type == 'offpolicy':
                    self.scenario_policy.train(replay_buffer)

            # end up environment
            self.env.clean_up()
            replay_buffer.finish_one_episode()

            # train off-policy agent or scenario
            if self.mode == 'train_agent' and self.agent_policy.type == 'onpolicy':
                self.agent_policy.train(replay_buffer)
            elif self.mode == 'train_scenario' and self.scenario_policy.type in ['init_state', 'onpolicy']:
                self.scenario_policy.train(replay_buffer)

            # eval during training
            if (e_i+1) % self.eval_in_train_freq == 0:
                #self.eval(env, data_loader)
                self.logger.log('>> ' + '-' * 40)

            # save checkpoints
            if (e_i+1) % self.save_freq == 0:
                if self.mode == 'train_agent':
                    self.agent_policy.save_model(e_i)
                if self.mode == 'train_scenario':
                    self.scenario_policy.save_model(e_i)

    def eval(self, data_loader):
        num_finished_scenario = 0
        video_count = 0
        data_loader.reset_idx_counter()
        while len(data_loader) > 0:
            # sample scenarios
            sampled_scenario_configs, num_sampled_scenario = data_loader.sampler()
            num_finished_scenario += num_sampled_scenario
            
            # begin to run the scene
            self.run_scene()
            
            sampled_scenario_configs[0].trajectory = [self.world.scenic.simulation.ego.carlaActor.get_transform()]
            # reset envs with new config, get init action from scenario policy, and run scenario
            static_obs = self.env.get_static_obs(sampled_scenario_configs)
            scenario_init_action, _ = self.scenario_policy.get_init_action(static_obs)
            obs, infos = self.env.reset(sampled_scenario_configs, scenario_init_action)
            
            # get ego vehicle from scenario
            self.agent_policy.set_ego_and_route(self.env.get_ego_vehicles(), infos)

            score_list = {s_i: [] for s_i in range(num_sampled_scenario)}
            while not self.env.all_scenario_done():
                # get action from agent policy and scenario policy (assume using one batch)
                ego_actions = self.agent_policy.get_action(obs, infos, deterministic=True)
                scenario_actions = self.scenario_policy.get_action(obs, infos, deterministic=True)

                # apply action to env and get obs
                obs, rewards, _, infos = self.env.step(ego_actions=ego_actions, scenario_actions=scenario_actions)

                # save video
                self.video_recorder.add_frame(pygame.surfarray.array3d(self.display).transpose(1, 0, 2))

                # accumulate scores of corresponding scenario
                reward_idx = 0
                for s_i in infos:
                    score = rewards[reward_idx] if self.scenario_category in ['planning', 'scenic'] else 1-infos[reward_idx]['iou_loss']
                    score_list[s_i['scenario_id']].append(score)
                    reward_idx += 1

            # clean up all things
            self.logger.log(">> All scenarios are completed. Clearning up all actors")
            self.env.clean_up()

            # save video
            self.video_recorder.save(video_name=f'video_{video_count}.gif')
            video_count += 1

            # print score for ranking
            self.logger.log(f'[{num_finished_scenario}/{data_loader.num_total_scenario}] Ranking scores for batch scenario:', color='yellow')
            for s_i in score_list.keys():
                self.logger.log('\t Env id ' + str(s_i) + ': ' + str(np.mean(score_list[s_i])), color='yellow')

            # calculate evaluation results
            score_function = get_route_scores if self.scenario_category in ['planning', 'scenic'] else get_perception_scores
            all_scores = score_function(self.env.running_results)
            self.logger.add_eval_results(all_scores)
            self.logger.print_eval_results()
            self.logger.save_eval_results()
            
            # update the next scene 
            if len(data_loader):
                self.update_scene()

    def run(self):
        # get scenario data of different maps
        config_list = scenic_parse(self.scenario_config, self.logger)
        
        for config in config_list:
            
            # initialize scenic
            self._init_scenic(config)
            # initialize map and render
            self._init_world()
            self._init_renderer()

            # create scenarios within the vectorized wrapper
            self.env = VectorWrapper(self.env_params, self.scenario_config, self.world, self.birdeye_render, self.display, self.logger)

            # prepare data loader and buffer
            data_loader = ScenicDataLoader(config, self.num_scenario)

            # run with different modes
            if self.mode == 'eval':
                self.agent_policy.load_model()
                self.scenario_policy.load_model()
                self.agent_policy.set_mode('eval')
                self.scenario_policy.set_mode('eval')
                self.eval(data_loader)
            elif self.mode == 'train_agent':
                start_episode = self.check_continue_training(self.agent_policy)
                self.scenario_policy.load_model()
                self.agent_policy.set_mode('train')
                self.scenario_policy.set_mode('eval')
                self.train(data_loader, start_episode)
            elif self.mode == 'train_scenario':
                start_episode = self.check_continue_training(self.scenario_policy)
                self.agent_policy.load_model()
                self.agent_policy.set_mode('eval')
                self.scenario_policy.set_mode('train')
                self.train(data_loader, start_episode)
            else:
                raise NotImplementedError(f"Unsupported mode: {self.mode}.")

    def check_continue_training(self, policy):
        # load previous checkpoint
        if policy.continue_episode == 0:
            start_episode = 0
            self.logger.log('>> Previous checkpoint not found. Training from scratch.')
        else:
            start_episode = policy.continue_episode
            self.logger.log('>> Continue training from previous checkpoint.')
        return start_episode

    def close(self):
        pygame.quit() # close pygame renderer
        if self.env:
            self.env.clean_up()