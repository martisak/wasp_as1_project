from __future__ import print_function

import sys
import time
import termios
import logging
import threading
import ujson as json
import csv
import numpy as np
import transformations as trans
from cflib import crazyflie, crtp
from cflib.crazyflie.log import LogConfig

from util import *

# Set a channel - if set to None, the first available crazyflie is used
URI = 'radio://0/91/2M'
# URI = None


def read_input(file=sys.stdin):
    """Registers keystrokes and yield these every time one of the
    *valid_characters* are pressed."""

    old_attrs = termios.tcgetattr(file.fileno())
    new_attrs = old_attrs[:]
    new_attrs[3] = new_attrs[3] & ~(termios.ECHO | termios.ICANON)
    try:
        termios.tcsetattr(file.fileno(), termios.TCSADRAIN, new_attrs)
        while True:
            try:
                yield sys.stdin.read(1)
            except (KeyboardInterrupt, EOFError):
                break
    finally:
        termios.tcsetattr(file.fileno(), termios.TCSADRAIN, old_attrs)


class ControllerThread(threading.Thread):
    """This is the controller thread"""

    # Move these to config file.
    period_in_ms = 20       # Control period. [ms]
    thrust_step = 5e3       # Thrust step with W/S. [of 2**16]
    thrust_initial = 0      # Start with zero thrust.
    thrust_limit = (0, .8 * 2**16 - 1)
    roll_limit = (-30.0, 30.0)
    pitch_limit = (-30.0, 30.0)
    yaw_limit = (-200.0, 200.0)
    enabled = False
    cumerr_limit = (-200, 200)

    def __init__(self, cf):
        super(ControllerThread, self).__init__()
        self.cf = cf

        self.read_config()
        self.logger = get_logger("controller")
        # Reset state
        self.disable(stop=False)

        # Keeps track of when we last printed
        self.last_time_print = 0.0

        # Connect some callbacks from the Crazyflie API
        self.cf.connected.add_callback(self._connected)
        self.cf.disconnected.add_callback(self._disconnected)
        self.cf.connection_failed.add_callback(self._connection_failed)
        self.cf.connection_lost.add_callback(self._connection_lost)
        self.send_setpoint = self.cf.commander.send_setpoint

        # Pose estimate from the Kalman filter
        self.pos = np.r_[0.0, 0.0, 0.0]
        self.vel = np.r_[0.0, 0.0, 0.0]
        self.attq = np.r_[0.0, 0.0, 0.0, 1.0]
        self.yawrate = 0  # derivative term

        self.cum_height_err = 0
        self.err_mag = 0
        self.roll_r, self.pitch_r, self.thrust_r, self.yawrate_r = 0, 0, 0, 0
        self.R = np.eye(3)

        # Attitide (roll, pitch, yaw) from stabilizer
        self.stab_att = np.r_[0.0, 0.0, 0.0]

        self.pos_ref = np.r_[self.pos[:2], 1.0]
        self.yaw_ref = 0.0
        self.pos_ref_initial = self.pos_ref

        # This makes Python exit when this is the only thread alive.
        self.daemon = True

        self.t0 = time.time()  # To be updated at run()

    def read_config(self):
        """Read config file and store values in class"""

        with open('config.json') as config_file:
            self.config = json.load(config_file)

    def _connected(self, link_uri):
        self.logger.info('Connected to {}'.format(link_uri))

        log_stab_att = LogConfig(
            name='Stabilizer', period_in_ms=self.period_in_ms)
        log_stab_att.add_variable('stabilizer.roll', 'float')
        log_stab_att.add_variable('stabilizer.pitch', 'float')
        log_stab_att.add_variable('stabilizer.yaw', 'float')
        self.cf.log.add_config(log_stab_att)

        log_pos = LogConfig(name='Kalman Position',
                            period_in_ms=self.period_in_ms)
        log_pos.add_variable('kalman.stateX', 'float')
        log_pos.add_variable('kalman.stateY', 'float')
        log_pos.add_variable('kalman.stateZ', 'float')
        self.cf.log.add_config(log_pos)

        log_vel = LogConfig(name='Kalman Velocity',
                            period_in_ms=self.period_in_ms)
        log_vel.add_variable('kalman.statePX', 'float')
        log_vel.add_variable('kalman.statePY', 'float')
        log_vel.add_variable('kalman.statePZ', 'float')
        log_vel.add_variable('gyro.z', 'float')

        self.cf.log.add_config(log_vel)

        log_att = LogConfig(name='Kalman Attitude',
                            period_in_ms=self.period_in_ms)
        log_att.add_variable('kalman.q0', 'float')
        log_att.add_variable('kalman.q1', 'float')
        log_att.add_variable('kalman.q2', 'float')
        log_att.add_variable('kalman.q3', 'float')

        self.cf.log.add_config(log_att)

        # If all log configs are valid, add callbacks
        if log_stab_att.valid and log_pos.valid and \
                log_vel.valid and log_att.valid:

            log_stab_att.data_received_cb.add_callback(self._log_data_stab_att)
            log_stab_att.error_cb.add_callback(self._log_error)
            log_stab_att.start()

            log_pos.data_received_cb.add_callback(self._log_data_pos)
            log_pos.error_cb.add_callback(self._log_error)
            log_pos.start()

            log_vel.error_cb.add_callback(self._log_error)
            log_vel.data_received_cb.add_callback(self._log_data_vel)
            log_vel.start()

            log_att.error_cb.add_callback(self._log_error)
            log_att.data_received_cb.add_callback(self._log_data_att)
            log_att.start()
        else:
            raise RuntimeError('One or more of the variables in the'
                               'configuration was not'
                               'found in log TOC. Will not'
                               'get any position data.')

    def _connection_failed(self, link_uri, msg):
        self.logger.error('Connection to %s failed: %s' % (link_uri, msg))

    def _connection_lost(self, link_uri, msg):
        self.logger.error('Connection to %s lost: %s' % (link_uri, msg))

    def _disconnected(self, link_uri):
        self.logger.info('Disconnected from %s' % link_uri)
        self.logger.info('Flight log: {}'.format(self.log_file_name))

    def _log_data_stab_att(self, timestamp, data, logconf):
        """Log function for stabilizer data"""
        self.stab_att = np.r_[data['stabilizer.roll'],
                              data['stabilizer.pitch'],
                              data['stabilizer.yaw']]

    def _log_data_pos(self, timestamp, data, logconf):
        """Log function for Kalman filter data"""
        self.pos = np.r_[data['kalman.stateX'],
                         data['kalman.stateY'],
                         data['kalman.stateZ']]

    def _log_data_vel(self, timestamp, data, logconf):
        """Log function for Kalman data"""
        vel_bf = np.r_[data['kalman.statePX'],
                       data['kalman.statePY'],
                       data['kalman.statePZ']]
        self.vel = np.dot(self.R, vel_bf)
        self.yawrate = data['gyro.z']

    def _log_data_att(self, timestamp, data, logconf):
        # NOTE q0 is real part of Kalman state's quaternion, but
        # transformations.py wants it as last dimension.

        self.attq = np.r_[data['kalman.q1'], data['kalman.q2'],
                          data['kalman.q3'], data['kalman.q0']]

        # Extract 3x3 rotation matrix from 4x4 transformation matrix
        self.R = trans.quaternion_matrix(self.attq)[:3, :3]
        # r, p, y = trans.euler_from_quaternion(self.attq)

    def _log_error(self, logconf, msg):
        self.logger.error('Error when logging %s: %s' % (logconf.name, msg))

    def make_position_sanity_check(self):
        # We assume that the position from the LPS should be
        # [-20m, +20m] in xy and [0m, 5m] in z
        if np.max(np.abs(self.pos[:2])) > 20 or self.pos[2] < 0 or self.pos[2] > 5:
            raise RuntimeError('Position estimate out of bounds', self.pos)

    def run(self):
        """Control loop definition"""

        # Keep the file pointer here.
        self.log_file_name = 'flightlog_' + \
            time.strftime("%Y%m%d_%H%M%S") + '.csv'
        self.fh = open(self.log_file_name, 'w')
        self.logger.info("Flight log: {}".format(self.log_file_name))
        while not self.cf.is_connected():
            time.sleep(0.2)

        self.logger.debug('Waiting for position estimate to be good enough...')
        self.reset_estimator()

        self.make_position_sanity_check()

        # Set the current reference to the current positional estimate, at a
        # slight elevation
        self.pos_ref = np.r_[self.pos[:2], 1.0]
        self.yaw_ref = 0.0

        self.logger.debug('Initial positional reference: {}'.format(
            self.pos_ref))
        self.pos_ref_initial = self.pos_ref
        self.logger.debug('Initial thrust reference: {}'.format(self.thrust_r))
        self.logger.info('Ready! Press e to enable motors,' +
                         'h for help and Q to quit')

        self.t0 = time.time()

        # Main loop
        while True:
            time_start = time.time()
            self.calc_control_signals()
            if self.enabled:
                sp = (self.roll_r, self.pitch_r,
                      self.yawrate_r, int(self.thrust_r))
                self.send_setpoint(*sp)
                self.log_data(sp)

            self.loop_sleep(time_start)

    def log_data(self, sp):
        """Log data to CSV"""

        ld = np.r_[time.time() - self.t0]
        ld = np.append(ld, np.asarray(sp))
        ld = np.append(ld, self.pos_ref)
        ld = np.append(ld, self.yaw_ref)
        ld = np.append(ld, self.pos)
        ld = np.append(ld, self.vel)
        ld = np.append(ld, self.attq)
        ld = np.append(ld, (np.reshape(self.R, -1)))
        ld = np.append(ld, trans.euler_from_quaternion(self.attq))
        ld = np.append(ld, self.stab_att)

        self.fh.write(','.join(map(str, ld)) + '\n')
        self.fh.flush()

    def calc_control_signals(self):
        """ This is the control code that outputs reference values
        for roll, pitch, yawrate and thrust."""

        roll, pitch, yaw = trans.euler_from_quaternion(self.attq)

        # Compute control errors in position
        ex, ey, ez = self.pos_ref - self.pos

        # Calculate the magnitude of the error for
        # coordinate navigation
        self.err_mag = np.linalg.norm([ex, ey, ez])

        eyaw = np.degrees(self.yaw_ref) - self.stab_att[2]  # In degrees

        # Thrust - height PD controller
        # TODO Read from config file

        if self.enabled:
            self.cum_height_err += ez
            self.cum_height_err = np.clip(
                self.cum_height_err, *self.cumerr_limit)

        kP = self.config['thrust']['kP']
        kD = self.config['thrust']['kD']
        kI = self.config['thrust']['kI']
        C = self.config['thrust']['C']

        mg = self.config['m'] * self.config['g']

        self.thrust_r = 4 * C * \
            (kP * ez - kD * self.vel[2] + mg + kI * self.cum_height_err) *  \
            2**16 / \
            (np.cos(np.radians(self.stab_att[0])) *
             np.cos(np.radians(self.stab_att[1])))

        # Pitch and roll - PD controller.

        kP = self.config['pitchroll']['kP']
        kD = self.config['pitchroll']['kD']
        kI = self.config['pitchroll']['kI']

        self.pitch_r = 20 * (kP * ex - kD * self.vel[0])
        self.roll_r = -20 * (kP * ey - kD * self.vel[1])

        # Rotational matrix.How's this different from R?
        A = np.array([[np.cos(yaw), -np.sin(yaw)], [np.sin(yaw), np.cos(yaw)]])

        self.pitch_r, self.roll_r = np.matmul(
            A,
            np.array([self.pitch_r, self.roll_r])
        )

        # Yaw - P-controller

        kP = self.config['yaw']['kP']
        kD = self.config['yaw']['kD']
        kI = self.config['yaw']['kI']

        self.yawrate_r = - (kP * eyaw - kD * self.yawrate)

        # Clip control signals to be within the specified range.
        self.roll_r = np.clip(self.roll_r, *self.roll_limit)
        self.pitch_r = np.clip(self.pitch_r, *self.pitch_limit)
        self.yawrate_r = np.clip(self.yawrate_r, *self.yaw_limit)
        self.thrust_r = np.clip(self.thrust_r, *self.thrust_limit)

        # This message is constructed too often,
        # but seldom printed.
        message = ('ref: ({}, {}, {}, {})\n'.format(
            self.pos_ref[0],
            self.pos_ref[1],
            self.pos_ref[2],
            np.degrees(self.yaw_ref)) +
            'pos: ({}, {}, {}, {})\n'.format(
            self.pos[0],
            self.pos[1],
            self.pos[2],
            self.stab_att[2]) +
            'vel: ({}, {}, {}, {})\n'.format(
            self.vel[0],
            self.vel[1],
            self.vel[2],
            self.yawrate) +
            'error: ({}, {}, {}, {}, {})\n'.format(
                ex, ey, ez, eyaw, self.cum_height_err) +
            'control: ({}, {}, {}, {})\n'.format(
            self.roll_r,
            self.pitch_r,
            self.thrust_r,
            self.yawrate_r,))

        self.print_at_period(.5, message)

    def print_at_period(self, period, message):
        """ Prints the message at a given period """

        if (time.time() - period) > self.last_time_print:
            self.read_config()  # Also read configuration file again
            self.last_time_print = time.time()
            self.logger.debug(message)

    def reset_estimator(self):
        self.cf.param.set_value('kalman.resetEstimation', '1')
        time.sleep(0.1)
        self.cf.param.set_value('kalman.resetEstimation', '0')

        # Sleep a bit, hoping that the estimator will have converged
        # Should be replaced by something that actually checks...
        # (see `wait_for_position_estimator)

        time.sleep(1.5)

    def disable(self, stop=True):
        if stop:
            self.send_setpoint(0.0, 0.0, 0.0, 0)
        if self.enabled:
            self.logger.info('Disabling controller')
        self.enabled = False
        self.roll_r = 0.0
        self.pitch_r = 0.0
        self.yawrate_r = 0.0
        self.thrust_r = self.thrust_initial

    def enable(self):
        if not self.enabled:
            self.logger.info('Enabling controller')

        # Need to send a zero setpoint to unlock the controller.
        self.send_setpoint(0.0, 0.0, 0.0, 0)
        self.cum_height_err = 0
        self.enabled = True

    def loop_sleep(self, time_start):
        """ Sleeps the control loop to make it run at a specified rate """
        delta_time = 1e-3 * self.period_in_ms - (time.time() - time_start)
        if delta_time > 0:
            time.sleep(delta_time)
        else:
            self.logger.warning('Deadline missed by', -delta_time, 'seconds. '
                                'Too slow control loop!')

    def increase_thrust(self):
        self.thrust_r += self.thrust_step
        self.thrust_r = min(self.thrust_r, 0xffff)

    def decrease_thrust(self):
        self.thrust_r -= self.thrust_step
        self.thrust_r = max(0, self.thrust_r)


def coordinates(control):
    """ Follow waypoints set in config.json"""

    logger = get_logger("coordinates")

    for ch in read_input():
        if ch == 'e':
            control.enable()
            break

    for n in range(len(control.config['coordinates']['x'])):


        # x and y can be relative
        x, y = control.config['coordinates']['x'][n], \
            control.config['coordinates']['y'][n]

        if control.config['coordinates']['relative']:
            x += control.pos_ref_initial[0]
            y += control.pos_ref_initial[1]

        # z and yaw can not be relative
        z, yaw = control.config['coordinates']['z'][n], \
            control.config['coordinates']['yaw'][n]

        logger.info("Setting reference: ({}, {}, {}, {})".format(
            x, y, z, yaw
        ))

        control.pos_ref = [x, y, z]
        control.yaw_ref = np.radians(yaw)

        # If magniture iof error is small enough
        while (control.err_mag > control.config["waypoint_margin"]):
            time.sleep(0.1)

        time.sleep(.5)

    control.disable()


def handle_keyboard_input(control):
    pos_step = 0.1  # [m]
    yaw_step = 5   # [deg]

    for ch in read_input():
        if ch == 'h':
            print('Key map:')
            print('>: Increase thrust (non-control mode)')
            print('<: Decrease thrust (non-control mode)')
            print('Q: quit program')
            print('e: Enable motors')
            print('q: Disable motors')
            print('w: Increase x-reference by ', pos_step, 'm.')
            print('s: Decrease x-reference by ', pos_step, 'm.')
            print('a: Increase y-reference by ', pos_step, 'm.')
            print('d: Decrease y-reference by ', pos_step, 'm.')
            print('i: Increase z-reference by ', pos_step, 'm.')
            print('k: Decrease z-reference by ', pos_step, 'm.')
            print('j: Increase yaw-reference by ', yaw_step, 'm.')
            print('l: Decrease yaw-reference by ', yaw_step, 'deg.')
        elif ch == '>':
            control.increase_thrust()
            print('Increased thrust to', control.thrust_r)
        elif ch == '<':
            control.decrease_thrust()
            print('Decreased thrust to', control.thrust_r)
        elif ch == 'w':
            control.pos_ref[0] += pos_step
            print('Reference position changed to :', control.pos_ref)
        elif ch == 's':
            control.pos_ref[0] -= pos_step
            print('Reference position changed to :', control.pos_ref)
        elif ch == 'a':
            control.pos_ref[1] += pos_step
            print('Reference position changed to :', control.pos_ref)
        elif ch == 'd':
            control.pos_ref[1] -= pos_step
            print('Reference position changed to :', control.pos_ref)
        elif ch == 'i':
            control.pos_ref[2] += pos_step
            print('Reference position changed to :', control.pos_ref)
        elif ch == 'k':
            control.pos_ref[2] -= pos_step
            print('Reference position changed to :', control.pos_ref)
        elif ch == 'j':
            control.yaw_ref += np.radians(yaw_step)
            print('Yaw reference changed to :',
                  np.degrees(control.yaw_ref), 'deg.')
        elif ch == 'l':
            control.yaw_ref -= np.radians(yaw_step)
            print('Yaw reference changed to :',
                  np.degrees(control.yaw_ref), 'deg.')
        elif ch == ' ':
            control.pos_ref[2] = 0.0
            print('Reference position changed to :', control.pos_ref)
        elif ch == 'e':
            control.enable()
        elif ch == 'q':
            if not control.enabled:
                print('Uppercase Q quits the program')
            control.disable()
        elif ch == 'Q':
            control.disable()
            print('Bye!')
            break
        else:
            print('Unhandled key', ch, 'was pressed')


if __name__ == "__main__":

    # logging.basicConfig()

    crtp.init_drivers(enable_debug_driver=False)
    cf = crazyflie.Crazyflie(rw_cache='./cache')
    control = ControllerThread(cf)
    control.start()
    logger = get_logger("main")
    if URI is None:
        logger.info('Scanning for Crazyflies...')
        available = crtp.scan_interfaces()
        if available:
            logger.info('Found Crazyflies:')
            for i in available:
                logger.info('- {}'.format(i[0]))
            URI = available[0][0]
        else:
            logger.error('No Crazyflies found!')
            sys.exit(1)

    logger.info('Connecting to {}'.format(URI))
    cf.open_link(URI)

    handle_keyboard_input(control)
    # coordinates(control)

    cf.close_link()
