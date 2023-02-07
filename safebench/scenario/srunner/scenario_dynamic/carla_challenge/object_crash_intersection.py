"""
@author: Shuai Wang
@e-mail: ws199807@outlook.com
Object crash with prior vehicle action scenario:
The scenario realizes the user controlled ego vehicle
moving along the road and encounters a cyclist ahead after taking a right or left turn.
"""

from __future__ import print_function

import math
import carla

from safebench.scenario.srunner.scenario_manager.carla_data_provider import CarlaDataProvider
from safebench.scenario.srunner.scenario_dynamic.basic_scenario_dynamic import BasicScenarioDynamic, SpawnOtherActorError
from safebench.scenario.srunner.tools.scenario_helper import generate_target_waypoint, generate_target_waypoint_in_route

from safebench.scenario.srunner.tools.scenario_operation import ScenarioOperation
from safebench.scenario.srunner.tools.scenario_utils import calculate_distance_transforms
from safebench.scenario.srunner.tools.scenario_utils import calculate_distance_locations


def get_opponent_transform(added_dist, waypoint, trigger_location):
    """
    Calculate the transform of the adversary
    """
    lane_width = waypoint.lane_width

    offset = {"orientation": 270, "position": 90, "k": 1.0}
    _wp = waypoint.next(added_dist)
    if _wp:
        _wp = _wp[-1]
    else:
        raise RuntimeError("Cannot get next waypoint !")

    location = _wp.transform.location
    orientation_yaw = _wp.transform.rotation.yaw + offset["orientation"]
    position_yaw = _wp.transform.rotation.yaw + offset["position"]

    offset_location = carla.Location(
        offset['k'] * lane_width * math.cos(math.radians(position_yaw)),
        offset['k'] * lane_width * math.sin(math.radians(position_yaw)))
    location += offset_location
    location.z = trigger_location.z
    transform = carla.Transform(location, carla.Rotation(yaw=orientation_yaw))

    return transform


def get_right_driving_lane(waypoint):
    """
    Gets the driving / parking lane that is most to the right of the waypoint
    as well as the number of lane changes done
    """
    lane_changes = 0

    while True:
        wp_next = waypoint.get_right_lane()
        lane_changes += 1

        if wp_next is None or wp_next.lane_type == carla.LaneType.Sidewalk:
            break
        elif wp_next.lane_type == carla.LaneType.Shoulder:
            # Filter Parkings considered as Shoulders
            if is_lane_a_parking(wp_next):
                lane_changes += 1
                waypoint = wp_next
            break
        else:
            waypoint = wp_next

    return waypoint, lane_changes


def is_lane_a_parking(waypoint):
    """
    This function filters false negative Shoulder which are in reality Parking lanes.
    These are differentiated from the others because, similar to the driving lanes,
    they have, on the right, a small Shoulder followed by a Sidewalk.
    """

    # Parking are wide lanes
    if waypoint.lane_width > 2:
        wp_next = waypoint.get_right_lane()

        # That are next to a mini-Shoulder
        if wp_next is not None and wp_next.lane_type == carla.LaneType.Shoulder:
            wp_next_next = wp_next.get_right_lane()

            # Followed by a Sidewalk
            if wp_next_next is not None and wp_next_next.lane_type == carla.LaneType.Sidewalk:
                return True

    return False


class VehicleTurningRouteDynamic(BasicScenarioDynamic):

    """
    This class holds everything required for a simple object crash
    with prior vehicle action involving a vehicle and a cyclist.
    The ego vehicle is passing through a road and encounters
    a cyclist after taking a turn. This is the version used when the ego vehicle
    is following a given route. (Traffic Scenario 4)
    This is a single ego vehicle scenario
    """

    def __init__(self, world, ego_vehicles, config, randomize=False, debug_mode=False, criteria_enable=True,
                 timeout=60):
        """
        Setup all relevant parameters and create scenario
        """
        # parameters = [self._other_actor_target_velocity, self.trigger_distance_threshold, start_distance]
        # parameters = [10, 17, 8]
        self.parameters = config.parameters
        self._wmap = CarlaDataProvider.get_map()
        self.timeout = timeout
        self._other_actor_target_velocity = self.parameters[0]
        self._reference_waypoint = self._wmap.get_waypoint(config.trigger_points[0].location)
        self._trigger_location = config.trigger_points[0].location
        self._ego_route = CarlaDataProvider.get_ego_vehicle_route()

        self._num_lane_changes = 0

        self._ego_route = CarlaDataProvider.get_ego_vehicle_route()

        super(VehicleTurningRouteDynamic, self).__init__("VehicleTurningRouteDynamic",
                                                  ego_vehicles,
                                                  config,
                                                  world,
                                                  debug_mode,
                                                  criteria_enable=criteria_enable,
                                                  terminate_on_failure=True)

        self.scenario_operation = ScenarioOperation(self.ego_vehicles, self.other_actors)

        self.actor_type_list.append('vehicle.diamondback.century')

        self.reference_actor = None
        self.trigger_distance_threshold = self.parameters[1]
        self.ego_max_driven_distance = 180

    def initialize_actors(self):
        """
        Custom initialization
        """
        waypoint = generate_target_waypoint_in_route(self._reference_waypoint, self._ego_route)

        # Move a certain distance to the front
        start_distance = self.parameters[2]
        waypoint = waypoint.next(start_distance)[0]

        # Get the last driving lane to the right
        waypoint, self._num_lane_changes = get_right_driving_lane(waypoint)
        # And for synchrony purposes, move to the front a bit
        added_dist = self._num_lane_changes

        _other_actor_transform = get_opponent_transform(added_dist, waypoint, self._trigger_location)

        self.other_actor_transform.append(_other_actor_transform)

        try:
            self.scenario_operation.initialize_vehicle_actors(self.other_actor_transform, self.other_actors,
                                                              self.actor_type_list)
        except:
            raise SpawnOtherActorError

        """Also need to specify reference actor"""
        self.reference_actor = self.other_actors[0]

    def update_behavior(self):
        for i in range(len(self.other_actors)):
            self.scenario_operation.go_straight(self._other_actor_target_velocity, i)

    def check_stop_condition(self):
        """
        This condition is just for small scenarios
        """

        return False


    def _create_behavior(self):
        pass