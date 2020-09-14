from simulators.warehouse.item import Item
from simulators.warehouse.robot import Robot
from simulators.warehouse.utils import *
import numpy as np
import copy
import random
from gym import spaces
import time
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import networkx as nx
import csv
sys.path.append("..") 
from influence.influence_model import InfluenceModel
from influence.influence import Influence
from influence.data_collector import DataCollector
import torch

class PartialWarehouse(object):
    """
    warehouse environment
    """

    ACTIONS = {0: 'UP',
               1: 'DOWN',
               2: 'LEFT',
               3: 'RIGHT'}

    def __init__(self, influence=None):
        self.parameters = read_parameters('partial_warehouse.yaml')
        # parameters = parse_arguments()
        self.n_columns = self.parameters['n_columns']
        self.n_rows = self.parameters['n_rows']
        self.n_robots_row = self.parameters['n_robots_row']
        self.n_robots_column = self.parameters['n_robots_column']
        self.distance_between_shelves = self.parameters['distance_between_shelves']
        self.robot_domain_size = self.parameters['robot_domain_size']
        self.prob_item_appears = self.parameters['prob_item_appears']
        # The learning robot
        self.learning_robot_id = self.parameters['learning_robot_id']
        self.max_episode_length = self.parameters['n_steps_episode']
        self.obs_type = self.parameters['obs_type']
        self.items = []
        self.img = None
        self.influence = influence
        self.reset()
        self.total_steps = 0

    def reset(self):
        """
        Resets the environment's state
        """
        self.robot_id = 0
        self._place_robots()
        self.item_id = 0
        self.items = []
        self._add_items()
        self.obs = self._get_observation()
        obs_tensor = torch.reshape(torch.FloatTensor(self.obs), (1,1,-1))
        _, probs = self.influence.model(obs_tensor)
        self.episode_length = 0
        self.influence.model.reset()
        # Influence-augmented observations
        if self.parameters['influence_aug_obs']:
            ia_obs = np.append(self.obs, np.concatenate([prob[:-1] for prob in probs]))
            return ia_obs
        else:
            return self.obs

    def step(self, action):
        """
        Performs a single step in the environment.
        """
        self._robots_act(action)
        obs_tensor = torch.reshape(torch.FloatTensor(self.obs), (1,1,-1))
        _, probs = self.influence.model(obs_tensor)
        ext_robot_locs = self._sample_ext_robot_locs(probs)
        reward = self._compute_reward(self.robots[self.learning_robot_id])
        self._remove_items(ext_robot_locs)
        self._add_items()
        self.obs = self._get_observation()
        # INFLUENCE-AUGMENTED OBSERVATIONS
        # ia_obs = np.append(self.obs, np.concatenate([prob[:-1] for prob in probs]))
        # Check whether learning robot is done
        # done = self.robots[self.learning_robot_id].done
        self.episode_length += 1
        self.total_steps += 1
        done = (self.max_episode_length <= self.episode_length)
        if done:
            self.reset()
        if self.parameters['render']:
            self.render(self.parameters['render_delay'])
        # if self.parameters['inf_update_freq'] % self.total_steps == 0 and self.simulator_id == 0:
            # self._update_influence()
        # Experiment.py resets the environment when done
        # if done is True:
        #     # Reset the environment to start a new episode.
        #     self.reset()
        # Influence-augmented observations
        if self.parameters['influence_aug_obs']:
            ia_obs = np.append(self.obs, np.concatenate([prob[:-1] for prob in probs]))
            return ia_obs, reward, done, []
        else:
            return self.obs, reward, done, []
        

    @property
    def observation_space(self):
        return None

    @property
    def action_space(self):
        """
        Returns A gym dict containing the number of action choices for all the
        agents in the environment
        """
        n_actions = spaces.Discrete(len(self.ACTIONS))
        action_dict = {robot.get_id:n_actions for robot in self.robots}
        action_space = spaces.Dict(action_dict)
        action_space.n = 4
        return action_space

    def render(self, delay=0.0):
        """
        Renders the environment
        """
        bitmap = self._get_state()
        position = self.robots[self.learning_robot_id].get_position
        bitmap[position[0], position[1], 1] += 1
        im = bitmap[:, :, 0] - 2*bitmap[:, :, 1]
        if self.img is None:
            fig,ax = plt.subplots(1)
            self.img = ax.imshow(im)
            for robot_id, robot in enumerate(self.robots):
                domain = robot.get_domain
                y = domain[0]
                x = domain[1]
                if robot_id == self.learning_robot_id:
                    color = 'r'
                    linestyle='-'
                    linewidth=2
                else:
                    color = 'k'
                    linestyle=':'
                    linewidth=1
                rect = patches.Rectangle((x-0.5, y-0.5), self.robot_domain_size[0],
                                         self.robot_domain_size[1], linewidth=linewidth,
                                         edgecolor=color, linestyle=linestyle,
                                         facecolor='none')
                ax.add_patch(rect)
        else:
            self.img.set_data(im)
        plt.pause(delay)
        plt.draw()

    def close(self):
        pass

    def seed(self, seed=None):
        if seed is not None:
            random.seed(seed)

    def create_graph(self, robot):
        """
        Creates a graph of robot's domain in the warehouse. Nodes are cells in
        the robot's domain and edges represent the possible transitions.
        """
        graph = nx.Graph()
        for i in range(robot.get_domain[0], robot.get_domain[2]+1):
            for j in range(robot.get_domain[1], robot.get_domain[3]+1):
                cell = np.array([i, j])
                graph.add_node(tuple(cell))
                for neighbor in self._neighbors(cell):
                    graph.add_edge(tuple(cell), tuple(neighbor))
        return graph

    ######################### Private Functions ###########################

    def _place_robots(self):
        """
        Sets robots initial position at the begining of every episode
        """
        self.robots = []
        domain_rows = np.arange(0, self.n_rows, self.robot_domain_size[0]-1)
        domain_columns = np.arange(0, self.n_columns, self.robot_domain_size[1]-1)
        for i in range(self.n_robots_row):
            for j in range(self.n_robots_column):
                robot_domain = [domain_rows[i], domain_columns[j],
                                domain_rows[i+1], domain_columns[j+1]]
                robot_position = [robot_domain[0] + self.robot_domain_size[0]//2,
                                  robot_domain[1] + self.robot_domain_size[1]//2]
                self.robots.append(Robot(self.robot_id, robot_position,
                                                  robot_domain))
                self.robot_id += 1

    def _add_items(self):
        """
        Add new items to the designated locations in the environment.
        """
        item_locs = None
        if len(self.items) > 0:
            item_locs = [item.get_position for item in self.items]
        for row in range(self.n_rows):
            if row % (self.distance_between_shelves) == 0:
                for column in range(self.n_columns):
                    loc = [row, column]
                    loc_free = True
                    if item_locs is not None:
                        loc_free = loc not in item_locs
                    if random.random() < self.prob_item_appears and loc_free:
                        self.items.append(Item(self.item_id, loc))
                        self.item_id += 1
            else:
                for column in range(0, self.n_rows, self.distance_between_shelves):
                    loc = [row, column]
                    loc_free = True
                    if item_locs is not None:
                        loc_free = loc not in item_locs
                    if random.random() < self.prob_item_appears and loc_free:
                        self.items.append(Item(self.item_id, loc))
                        self.item_id += 1

    def _get_state(self):
        """
        Generates a 3D bitmap: First layer shows the location of every item.
        Second layer shows the location of the robots.
        """
        state_bitmap = np.zeros([self.n_rows, self.n_columns, 2], dtype=np.int)
        for item in self.items:
            item_pos = item.get_position
            state_bitmap[item_pos[0], item_pos[1], 0] = 1 #item.get_waiting_time
        for robot in self.robots:
            robot_pos = robot.get_position
            state_bitmap[robot_pos[0], robot_pos[1], 1] = 1
        return state_bitmap

    def _get_observation(self):
        """
        Generates the individual observation for every robot given the current
        state and the robot's designated domain.
        """
        state = self._get_state()
        observation = self.robots[self.learning_robot_id].observe(state, self.obs_type)
        return observation

    def _robots_act(self, action):
        """
        Robot takes an action in the environment.
        """
        self.robots[self.learning_robot_id].act(action)

    def _compute_reward(self, robot):
        """
        Computes reward for the learning robot.
        """
        reward = 0
        robot_pos = robot.get_position
        robot_domain = robot.get_domain
        for item in self.items:
            item_pos = item.get_position
            if robot_domain[0] <= item_pos[0] <= robot_domain[2] and \
               robot_domain[1] <= item_pos[1] <= robot_domain[3]:
                reward += -0.1 #*item.get_waiting_time
            if robot_pos[0] == item_pos[0] and robot_pos[1] == item_pos[1]:
                reward += 1
        return reward

    def _remove_items(self, ext_robot_pos):
        """
        Removes items collected by robots. Robots collect items by steping on
        them
        """
        for robot in self.robots:
            robot_pos = robot.get_position
            for item in self.items:
                item_pos = item.get_position
                if robot_pos[0] == item_pos[0] and robot_pos[1] == item_pos[1]:
                    self.items.remove(item)
        for pos in ext_robot_pos:
            if pos is not None:
                for item in self.items:
                    item_pos = item.get_position
                    if pos[0] == item_pos[0] and pos[1] == item_pos[1]:
                        self.items.remove(item)

    def _increase_item_waiting_time(self):
        """
        Increases items waiting time
        """
        for item in self.items:
            item.increase_waiting_time()

    def _neighbors(self, cell):
        return [cell + [0, 1], cell + [0, -1], cell + [1, 0], cell + [-1, 0]]
    
    def _sample_ext_robot_locs(self, probs):
        locations = []
        for i, prob in enumerate(probs):
            # loc = np.zeros(self.robot_domain_size[0]*self.robot_domain_size[1])
            sample = np.random.choice(np.arange(len(prob)), p=prob)
            # loc[sample] = 1
            # loc = np.reshape(loc, self.robot_domain_size)
            if sample < len(prob) - 1:
                location = self._find_loc(i, sample)
            else:
                location = None
            locations.append(location)
        return locations
    
    def _find_loc(self, ext_robot_id, loc):
        locations = {0: [loc, 4], 1: [-1, 4], 2: [4, loc], 3: [4, 0],
                     4: [loc, 0], 5: [0, 0], 6: [0, loc], 7: [0, 4]}
        return locations[ext_robot_id]