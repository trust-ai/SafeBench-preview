import numpy as np
import carla
import pygame

from safebench.gym_carla.env_wrapper import VectorWrapper
from safebench.gym_carla.envs.render import BirdeyeRender

from safebench.scenario.srunner.scenario_manager.carla_data_provider import CarlaDataProvider
from safebench.scenario.srunner.scenario_manager.scenario_trainer import ScenarioTrainer
from safebench.scenario.srunner.tools.scenario_utils import scenario_parse

from safebench.agent import AGENT_LIST
from safebench.agent.safe_rl.agent_trainer import AgentTrainer
from safebench.util.logger import EpochLogger, setup_logger_kwargs


class CarlaRunner:
    def __init__(self, agent_config, scenario_config):
        self.scenario_config = scenario_config
        self.agent_config = agent_config

        self.mode = scenario_config['mode']
        self.render = scenario_config['render']
        self.num_scenario = scenario_config['num_scenario']
        self.num_episode = scenario_config['num_episode']
        self.fixed_delta_seconds = scenario_config['fixed_delta_seconds']
        self.scenario_type = scenario_config['type_name'].split('.')[0]

        # continue training flag
        self.continue_agent_training = scenario_config['continue_agent_training']
        self.continue_scenario_training = scenario_config['continue_scenario_training']

        # apply settings to carla
        self.client = carla.Client('localhost', scenario_config['port'])
        self.client.set_timeout(10.0)
        self.world = None

        # for obtaining rendering results
        self.display_size = 256
        self.obs_range = 32
        self.d_behind = 12

        # pass info from scenario to agent
        agent_config['mode'] = scenario_config['mode']
        agent_config['ego_action_dim'] = scenario_config['ego_action_dim']
        agent_config['ego_state_dim'] = scenario_config['ego_state_dim']
        agent_config['ego_action_limit'] = scenario_config['ego_action_limit']

        # prepare ego agent
        if self.mode == 'eval':
            self.logger = EpochLogger(eval_mode=True)
        elif self.mode == 'train_scenario':
            self.logger = EpochLogger(eval_mode=True)
            self.trainer = ScenarioTrainer()
        elif self.mode == 'train_agent':
            logger_kwargs = setup_logger_kwargs(scenario_config['exp_name'], scenario_config['seed'], data_dir=scenario_config['data_dir'])
            self.logger = EpochLogger(**logger_kwargs)
            self.logger.save_config(agent_config)
            self.trainer = AgentTrainer(agent_config, self.logger)
        else:
            raise NotImplementedError(f"Unsupported mode: {self.mode}.")
        self.agent = AGENT_LIST[agent_config['agent_type']](agent_config, logger=self.logger)
        self.env = None

    def _eval_sampler(self, config_lists):
        # sometimes the length of list is smaller than num_scenario
        sample_num = np.min([self.num_scenario, len(config_lists)])

        # TODO: sampled scenario should not have overlap

        selected_scenario = []
        for _ in range(sample_num):
            s_i = np.random.randint(0, len(config_lists))
            selected_scenario.append(config_lists.pop(s_i))
        
        assert len(selected_scenario) <= self.num_scenario, "number of sampled scenarios is larger than {}".format(self.num_scenario)
        return selected_scenario

    def _train_sampler(self, config_lists):
        # TODO: during training, we should provide a looped sampler
        return self._eval_sampler(config_lists)

    def _init_world(self, town):
        self.logger.log(">> Initializing carla world")
        self.world = self.client.load_world(town)
        settings = self.world.get_settings()
        settings.synchronous_mode = True
        settings.fixed_delta_seconds = self.fixed_delta_seconds
        self.world.apply_settings(settings)
        CarlaDataProvider.set_client(self.client)
        CarlaDataProvider.set_world(self.world)
        self.world.set_weather(carla.WeatherParameters.ClearNoon)

    def _init_renderer(self, num_envs):
        self.logger.log(">> Initializing pygame birdeye renderer")
        pygame.init()
        flag = pygame.HWSURFACE | pygame.DOUBLEBUF
        if not self.render:
            flag = flag | pygame.HIDDEN
        self.display = pygame.display.set_mode((self.display_size * 3, self.display_size * num_envs), flag)

        pixels_per_meter = self.display_size / self.obs_range
        pixels_ahead_vehicle = (self.obs_range / 2 - self.d_behind) * pixels_per_meter
        self.birdeye_params = {
            'screen_size': [self.display_size, self.display_size],
            'pixels_per_meter': pixels_per_meter,
            'pixels_ahead_vehicle': pixels_ahead_vehicle,
        }

        # initialize the render for genrating observation and visualization
        self.birdeye_render = BirdeyeRender(self.world, self.birdeye_params, logger=self.logger)

    def eval(self, config_lists):
        num_total_scenario = len(config_lists)
        num_finished_scenario = 0
        while len(config_lists) > 0:
            # sample scenarios
            scenario_configs = self._eval_sampler(config_lists)
            num_batch_scenario = len(scenario_configs)
            num_finished_scenario += num_batch_scenario
            # reset envs
            obss = self.env.reset(scenario_configs, self.scenario_type)
            rewards_list = {s_i: [] for s_i in range(num_batch_scenario)}
            while True:
                if self.env.all_scenario_done():
                    self.logger.log(">> All scenarios are completed. Prepare for exiting")
                    break

                # get action from ego agent (assume using one batch)
                ego_actions = self.agent.get_action(obss)

                # apply action to env and get obs
                obss, rewards, _, infos = self.env.step(ego_actions=ego_actions)

                # accumulate reward to corresponding scenario
                reward_idx = 0
                for s_i in infos:
                    rewards_list[s_i['scenario_id']].append(rewards[reward_idx])
                    reward_idx += 1

            self.logger.log('>> Clearning up all actors')
            self.env.clean_up()

            # calculate episode reward and print
            self.logger.log('[{}/{}] Episode reward for batch scenario:'.format(num_finished_scenario, num_total_scenario), color='yellow')
            for s_i in rewards_list.keys():
                self.logger.log('\t Scenario' + str(s_i) + ': ' + str(np.sum(rewards_list[s_i])), color='yellow')

    def run(self):
        # get config of map and twon
        map_town_config = scenario_parse(self.scenario_config, self.logger)
        for town in map_town_config.keys():
            # initialize town
            self._init_world(town)
            # initialize the renderer
            self._init_renderer(self.num_scenario)
            config_lists = map_town_config[town]

            # create scenarios within the vectorized wrapper
            self.env = VectorWrapper(self.agent_config, self.scenario_config, self.world, self.birdeye_render, self.display, self.logger)

            if self.mode == 'eval':
                self.eval(config_lists)
            elif self.mode in ['train_scenario', 'train_agent']:
                self.trainer.set_environment(self.env, self.agent)
                self.trainer.train()
            else:
                raise NotImplementedError(f"Unsupported mode: {self.mode}.")

    def close(self):
        # check if all actors are cleaned
        actor_filters = [
            'vehicle.*',
            'walker.*',
            'controller.ai.walker',
            'sensor.other.collision', 
            'sensor.lidar.ray_cast',
            'sensor.camera.rgb', 
        ]
        for actor_filter in actor_filters:
            for actor in self.world.get_actors().filter(actor_filter):
                self.logger.log('>> Removing', actor.type_id, actor.id, actor.is_alive)
                if actor.is_alive:
                    if actor.type_id == 'controller.ai.walker':
                        actor.stop()
                    actor.destroy()
