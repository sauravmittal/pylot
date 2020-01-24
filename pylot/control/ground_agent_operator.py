"""Implements an agent operator that uses info from the simulator."""

from collections import deque
import erdos
import math
from pid_controller.pid import PID

from pylot.control.messages import ControlMessage
import pylot.control.utils
from pylot.map.hd_map import HDMap
from pylot.simulation.utils import get_map


class GroundAgentOperator(erdos.Operator):
    """ Agent that uses data from CARLA to drive around.

    The agent stops whenever it encounters an obstacle (i.e., a vehicle or
    a person), and waits for the obstacle to move. It does not implement
    any obstacle avoidance policy.

    The agent waits for all input streams to receive a watermark message for
    timestamp t, and then it computes and sends a control command.

    Args:
        can_bus_stream (:py:class:`erdos.ReadStream`): Stream on which can bus
            info is received.
        ground_obstacles_stream (:py:class:`erdos.ReadStream`): Stream on which
            :py:class:`~pylot.simulation.GroundObstaclesMessage` messages are
            received.
        ground_traffic_lights_stream (:py:class:`erdos.ReadStream`): Stream on
            which :py:class:`~pylot.simulation.GroundTrafficLightsMessage`
            messages are received.
        waypoints_stream (:py:class:`erdos.ReadStream`): Stream on which
            :py:class:`~pylot.planning.messages.WaypointMessage` messages are
            received. The agent must follow these waypoints.
        control_stream (:py:class:`erdos.WriteStream`): Stream on which the
            operator sends :py:class:`~pylot.control.messages.ControlMessage`
            messages.
        name (:obj:`str`): The name of the operator.
        flags (absl.flags): Object to be used to access absl flags.
        log_file_name (:obj:`str`, optional): Name of file where log messages
            are written to. If None, then messages are written to stdout.
        csv_file_name (:obj:`str`, optional): Name of file where stats logs are
            written to. If None, then messages are written to stdout.
    """
    def __init__(self,
                 can_bus_stream,
                 ground_obstacles_stream,
                 ground_traffic_lights_stream,
                 waypoints_stream,
                 control_stream,
                 name,
                 flags,
                 log_file_name=None,
                 csv_file_name=None):
        can_bus_stream.add_callback(self.on_can_bus_update)
        ground_obstacles_stream.add_callback(self.on_obstacles_update)
        ground_traffic_lights_stream.add_callback(
            self.on_traffic_lights_update)
        waypoints_stream.add_callback(self.on_waypoints_update)
        erdos.add_watermark_callback([
            can_bus_stream, ground_obstacles_stream,
            ground_traffic_lights_stream, waypoints_stream
        ], [control_stream], self.on_watermark)
        self._name = name
        self._logger = erdos.utils.setup_logging(name, log_file_name)
        self._csv_logger = erdos.utils.setup_csv_logging(
            name + '-csv', csv_file_name)
        self._flags = flags
        self._map = HDMap(
            get_map(self._flags.carla_host, self._flags.carla_port,
                    self._flags.carla_timeout), log_file_name)
        self._pid = PID(p=flags.pid_p, i=flags.pid_i, d=flags.pid_d)
        self._can_bus_msgs = deque()
        self._obstacle_msgs = deque()
        self._traffic_light_msgs = deque()
        self._waypoint_msgs = deque()

    @staticmethod
    def connect(can_bus_stream, ground_obstacles_stream,
                ground_traffic_lights_stream, waypoints_stream):
        """Connects the operator to input streams.

        Returns:
            :py:class:`erdos.WriteStream`: Stream on which the control commands
            are sent by the operator.
        """
        control_stream = erdos.WriteStream()
        return [control_stream]

    def on_watermark(self, timestamp, control_stream):
        """Invoked when all input streams have received a watermark.

        The method computes and sends the control command on the control
        stream.

        Args:
            timestamp (:py:class:`erdos.timestamp.Timestamp`): The timestamp of
                the watermark.
        """
        self._logger.debug('@{}: received watermark'.format(timestamp))
        # Get hero vehicle info.
        can_bus_msg = self._can_bus_msgs.popleft()
        vehicle_transform = can_bus_msg.data.transform
        vehicle_speed = can_bus_msg.data.forward_speed
        # Get waypoints.
        waypoint_msg = self._waypoint_msgs.popleft()
        wp_angle = waypoint_msg.wp_angle
        wp_vector = waypoint_msg.wp_vector
        wp_angle_speed = waypoint_msg.wp_angle_speed
        # Get ground obstacle info.
        obstacles = self._obstacle_msgs.popleft().obstacles
        # Get ground traffic lights info.
        traffic_lights = self._traffic_light_msgs.popleft().traffic_lights

        speed_factor, state = pylot.control.utils.stop_for_agents(
            vehicle_transform.location, wp_angle, wp_vector, wp_angle_speed,
            obstacles, traffic_lights, self._map, self._flags)
        control_stream.send(
            self.get_control_message(wp_angle, wp_angle_speed, speed_factor,
                                     vehicle_speed, timestamp))

    def on_waypoints_update(self, msg):
        self._logger.debug('@{}: received waypoints message'.format(
            msg.timestamp))
        self._waypoint_msgs.append(msg)

    def on_can_bus_update(self, msg):
        self._logger.debug('@{}: received can bus message'.format(
            msg.timestamp))
        self._can_bus_msgs.append(msg)

    def on_obstacles_update(self, msg):
        self._logger.debug('@{}: received obstacles message'.format(
            msg.timestamp))
        self._obstacle_msgs.append(msg)

    def on_traffic_lights_update(self, msg):
        self._logger.debug('@{}: received traffic lights message'.format(
            msg.timestamp))
        self._traffic_light_msgs.append(msg)

    def get_control_message(self, wp_angle, wp_angle_speed, speed_factor,
                            current_speed, timestamp):
        assert current_speed >= 0, 'Current speed is negative'
        steer = pylot.control.utils.radians_to_steer(wp_angle,
                                                     self._flags.steer_gain)

        # Don't go to fast around corners
        if math.fabs(wp_angle_speed) < 0.1:
            target_speed_adjusted = self._flags.target_speed * speed_factor / 2
        else:
            target_speed_adjusted = self._flags.target_speed * speed_factor

        throttle, brake = pylot.control.utils.compute_throttle_and_brake(
            self._pid, current_speed, target_speed_adjusted, self._flags)

        return ControlMessage(steer, throttle, brake, False, False, timestamp)
