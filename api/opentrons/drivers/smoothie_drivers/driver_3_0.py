from os import environ
import logging
from time import sleep
from threading import Event
from typing import Dict

from serial.serialutil import SerialException

from opentrons.drivers import serial_communication
from opentrons.drivers.rpi_drivers import gpio
'''
- Driver is responsible for providing an interface for motion control
- Driver is the only system component that knows about GCODES or how smoothie
  communications

- Driver is NOT responsible interpreting the motions in any way
  or knowing anything about what the axes are used for
'''

log = logging.getLogger(__name__)

ERROR_KEYWORD = 'error'
ALARM_KEYWORD = 'alarm'

# TODO (artyom, ben 20171026): move to config
HOMED_POSITION = {
    'X': 418,
    'Y': 353,
    'Z': 218,
    'A': 218,
    'B': 19,
    'C': 19
}

PLUNGER_BACKLASH_MM = 0.3
LOW_CURRENT_Z_SPEED = 30
CURRENT_CHANGE_DELAY = 0.005

Y_SWITCH_BACK_OFF_MM = 28
Y_SWITCH_REVERSE_BACK_OFF_MM = 10
Y_BACKOFF_LOW_CURRENT = 0.8
Y_BACKOFF_SLOW_SPEED = 50
Y_RETRACT_SPEED = 8
Y_RETRACT_DISTANCE = 3

DEFAULT_AXES_SPEED = 400

HOME_SEQUENCE = ['ZABC', 'X', 'Y']
AXES = ''.join(HOME_SEQUENCE)
# Ignore these axis when sending move or home command
DISABLE_AXES = ''

MOVEMENT_ERROR_MARGIN = 1/160  # Largest movement in mm for any step
SEC_PER_MIN = 60

DEFAULT_SMOOTHIE_TIMEOUT = 1
DEFAULT_MOVEMENT_TIMEOUT = 30
SMOOTHIE_BOOT_TIMEOUT = 3
DEFAULT_STABILIZE_DELAY = 0.1

DEFAULT_COMMAND_RETRIES = 3

GCODES = {'HOME': 'G28.2',
          'MOVE': 'G0',
          'DWELL': 'G4',
          'CURRENT_POSITION': 'M114.2',
          'LIMIT_SWITCH_STATUS': 'M119',
          'PROBE': 'G38.2',
          'ABSOLUTE_COORDS': 'G90',
          'RELATIVE_COORDS': 'G91',
          'RESET_FROM_ERROR': 'M999',
          'PUSH_SPEED': 'M120',
          'POP_SPEED': 'M121',
          'SET_SPEED': 'G0F',
          'READ_INSTRUMENT_ID': 'M369',
          'WRITE_INSTRUMENT_ID': 'M370',
          'READ_INSTRUMENT_MODEL': 'M371',
          'WRITE_INSTRUMENT_MODEL': 'M372',
          'SET_MAX_SPEED': 'M203.1',
          'SET_CURRENT': 'M907',
          'DISENGAGE_MOTOR': 'M18',
          'HOMING_STATUS': 'G28.6'}

# Number of digits after the decimal point for coordinates being sent
# to Smoothie
GCODE_ROUNDING_PRECISION = 3

SMOOTHIE_COMMAND_TERMINATOR = 'M400\r\n\r\n'
SMOOTHIE_ACK = 'ok\r\nok\r\n'


class SmoothieError(Exception):
    pass


class ParseError(Exception):
    pass


def _parse_number_from_substring(smoothie_substring):
    '''
    Returns the number in the expected string "N:12.3", where "N" is the
    axis, and "12.3" is a floating point value for the axis' position
    '''
    try:
        return round(
            float(smoothie_substring.split(':')[1]),
            GCODE_ROUNDING_PRECISION
        )
    except (ValueError, IndexError, TypeError, AttributeError) as e:
        log.exception('Unexpected argument to _parse_number_from_substring:')
        raise ParseError(
            'Unexpected argument to _parse_number_from_substring: {}'.format(
                smoothie_substring))


def _parse_axis_from_substring(smoothie_substring):
    '''
    Returns the axis in the expected string "N:12.3", where "N" is the
    axis, and "12.3" is a floating point value for the axis' position
    '''
    try:
        return smoothie_substring.split(':')[0].title()  # upper 1st letter
    except (ValueError, IndexError, TypeError, AttributeError) as e:
        log.exception('Unexpected argument to _parse_axis_from_substring:')
        raise ParseError(
            'Unexpected argument to _parse_axis_from_substring: {}'.format(
                smoothie_substring))


def _parse_position_response(raw_axis_values):
    parsed_values = raw_axis_values.strip().split(' ')
    if len(parsed_values) < 8:
        msg = 'Unexpected response in _parse_position_response: {}'.format(
            raw_axis_values)
        log.error(msg)
        raise ParseError(msg)

    data = {
        _parse_axis_from_substring(s): _parse_number_from_substring(s)
        for s in parsed_values[2:]  # remove first two items ('ok', 'MCS:')
    }
    return data


def _parse_instrument_data(smoothie_response):
    try:
        items = smoothie_response.split('\n')[0].strip().split(':')
        mount = items[0]
        # data received from Smoothieware is stringified HEX values
        # because of how Smoothieware handles GCODE messages
        data = bytearray.fromhex(items[1])
    except (ValueError, IndexError, TypeError, AttributeError) as e:
        log.exception('Unexpected argument to _parse_instrument_data:')
        raise ParseError(
            'Unexpected argument to _parse_instrument_data: {}'.format(
                smoothie_response))
    return {mount: data}


def _byte_array_to_ascii_string(byte_array):
    # remove trailing null characters
    try:
        for c in [b'\x00', b'\xFF']:
            if c in byte_array:
                byte_array = byte_array[:byte_array.index(c)]
        res = byte_array.decode()
    except (ValueError, TypeError, AttributeError) as e:
        log.exception('Unexpected argument to _byte_array_to_ascii_string:')
        raise ParseError(
            'Unexpected argument to _byte_array_to_ascii_string: {}'.format(
                byte_array))
    return res


def _byte_array_to_hex_string(byte_array):
    # data must be sent as stringified HEX values
    # because of how Smoothieware parses GCODE messages
    try:
        res = ''.join('%02x' % b for b in byte_array)
    except TypeError as e:
        log.exception('Unexpected argument to _byte_array_to_hex_string:')
        raise ParseError(
            'Unexpected argument to _byte_array_to_hex_string: {}'.format(
                byte_array))
    return res


def _parse_switch_values(raw_switch_values):
    if not raw_switch_values or not isinstance(raw_switch_values, str):
        raise ParseError(
            'Unexpected argument to _parse_switch_values: {}'.format(
                raw_switch_values))

    # probe has a space after it's ":" for some reason
    if 'Probe: ' in raw_switch_values:
        raw_switch_values = raw_switch_values.replace('Probe: ', 'Probe:')

    parsed_values = raw_switch_values.strip().split(' ')
    res = {
        _parse_axis_from_substring(s): bool(_parse_number_from_substring(s))
        for s in parsed_values
        if any([n in s for n in ['max', 'Probe']])
    }
    # remove the extra "_max" character from each axis key in the dict
    res = {
        key.split('_')[0]: val
        for key, val in res.items()
    }
    if len((list(AXES) + ['Probe']) & res.keys()) != 7:
        raise ParseError(
            'Unexpected argument to _parse_switch_values: {}'.format(
                raw_switch_values))
    return res


def _parse_homing_status_values(raw_homing_status_values):
    '''
        Parse the Smoothieware response to a G28.6 command (homing-status)
        A "1" means it has been homed, and "0" means it has not been homed

        Example response after homing just X axis:
        "X:1 Y:0 Z:0 A:0 B:0 C:0"

        returns: dict
            Key is axis, value is True if the axis needs to be homed
    '''
    if not raw_homing_status_values or \
            not isinstance(raw_homing_status_values, str):
        raise ParseError(
            'Unexpected argument to _parse_homing_status_values: {}'.format(
                raw_homing_status_values))
    parsed_values = raw_homing_status_values.strip().split(' ')
    res = {
        _parse_axis_from_substring(s): bool(_parse_number_from_substring(s))
        for s in parsed_values
    }
    if len(list(AXES) & res.keys()) != 6:
        raise ParseError(
            'Unexpected argument to _parse_homing_status_values: {}'.format(
                raw_homing_status_values))
    return res


class SmoothieDriver_3_0_0:
    def __init__(self, config):
        self.run_flag = Event()
        self.run_flag.set()

        self._position = HOMED_POSITION.copy()
        self.log = []

        # why do we do this after copying the HOMED_POSITION?
        self._update_position({axis: 0 for axis in AXES})

        self.simulating = True
        self._connection = None
        self._config = config

        # Current settings:
        # The amperage of each axis, has been organized into three states:
        # Current-Settings is the amperage each axis was last set to
        # Active-Current-Settings is set when an axis is moving/homing
        # Dwelling-Current-Settings is set when an axis is NOT moving/homing
        self._current_settings = {
            'now': config.low_current.copy(),
            'saved': config.low_current.copy()  # used in push/pop methods
        }
        self._active_current_settings = {
            'now': config.high_current.copy(),
            'saved': config.high_current.copy()  # used in push/pop methods
        }
        self._dwelling_current_settings = {
            'now': config.low_current.copy(),
            'saved': config.low_current.copy()  # used in push/pop methods
        }

        # Active axes are axes that are in use. An axis might be disabled if
        # a motor has had a failure and the robot is operating without that
        # axis until it can be repaired. This will be an unusual circumstance.
        self._active_axes = {ax: False for ax in AXES}

        # Engaged axes are axes that have not been disengaged (GCode M18) since
        # their last "move" or "home" operations. Disengaging an axis stops the
        # power output to the associated motor, primarily for the purpose of
        # reducing heat. When a "disengage" command is sent for an axis, this
        # dict should be updated to False for that axis, and when a "move" or
        # "home" command is sent for an axis, that axis should be updated to
        # True.
        self.engaged_axes = {ax: True for ax in AXES}

        # motor speed settings
        self._max_speed_settings = config.default_max_speed.copy()
        self._saved_max_speed_settings = self._max_speed_settings.copy()
        self._combined_speed = float(DEFAULT_AXES_SPEED)
        self._saved_axes_speed = float(self._combined_speed)

        # position after homing
        self._homed_position = HOMED_POSITION.copy()
        self.homed_flags = {}
        self.update_homed_flags(flags={
            'X': False,
            'Y': False,
            'Z': False,
            'A': False,
            'B': False,
            'C': False
        })

    @property
    def homed_position(self):
        return self._homed_position.copy()

    def _update_position(self, target):
        self._position.update({
            axis: value
            for axis, value in target.items() if value is not None
        })

        self.log += [self._position.copy()]

    def update_position(self, default=None):
        if default is None:
            default = self._position

        if self.simulating:
            updated_position = self._position.copy()
            updated_position.update(**default)
        else:
            def _recursive_update_position(retries):
                try:
                    position_response = self._send_command(
                        GCODES['CURRENT_POSITION'])
                    return _parse_position_response(position_response)
                except ParseError as e:
                    retries -= 1
                    if retries <= 0:
                        raise e
                    if not self.simulating:
                        sleep(DEFAULT_STABILIZE_DELAY)
                    return _recursive_update_position(retries)

            updated_position = _recursive_update_position(
                DEFAULT_COMMAND_RETRIES)

        self._update_position(updated_position)

    def read_pipette_id(self, mount):
        '''
        Reads in an attached pipette's UUID
        The UUID is unique to this pipette, and is a string of unknown length

        mount:
            String (str) with value 'left' or 'right'
        '''
        if self.simulating:
            res = '1234567890'
        else:
            res = self._read_from_pipette(GCODES['READ_INSTRUMENT_ID'], mount)
        if res:
            ret = {'pipette_id': res}
        else:
            ret = {'message': 'Error: Pipette ID read failed'}
        return ret

    def read_pipette_model(self, mount):
        '''
        Reads an attached pipette's MODEL
        The MODEL is a unique string for this model of pipette

        :return model string, or None
        '''
        if self.simulating:
            res = None
        else:
            res = self._read_from_pipette(
                GCODES['READ_INSTRUMENT_MODEL'], mount)
            if res and '_v' not in res:
                # Backward compatibility for pipettes programmed with model
                # strings that did not include the _v# designation
                res = res + '_v1'
            elif res and '_v13' in res:
                # Backward compatibility for pipettes programmed with model
                # strings that did not include the "." to seperate version
                # major and minor values
                res = res.replace('_v13', 'v1.3')

        return res

    def write_pipette_id(self, mount, data_string):
        '''
        Writes to an attached pipette's UUID memory location
        The UUID is unique to this pipette, and is a string of unknown length

        NOTE: To enable write-access to the pipette, it's button must be held

        mount:
            String (str) with value 'left' or 'right'
        data_string:
            String (str) that is of unknown length, and should be unique to
            this one pipette
        '''
        self._write_to_pipette(
            GCODES['WRITE_INSTRUMENT_ID'], mount, data_string)

    def write_pipette_model(self, mount, data_string):
        '''
        Writes to an attached pipette's MODEL memory location
        The MODEL is a unique string for this model of pipette

        NOTE: To enable write-access to the pipette, it's button must be held

        mount:
            String (str) with value 'left' or 'right'
        data_string:
            String (str) that is unique to this model of pipette
        '''
        self._write_to_pipette(
            GCODES['WRITE_INSTRUMENT_MODEL'], mount, data_string)

    # FIXME (JG 9/28/17): Should have a more thought out
    # way of simulating vs really running
    def connect(self, port=None):
        if environ.get('ENABLE_VIRTUAL_SMOOTHIE', '').lower() == 'true':
            self.simulating = True
            return
        self.disconnect()
        self._connect_to_port(port)
        self._setup()

    def disconnect(self):
        if self.is_connected():
            self._connection.close()
        self._connection = None
        self.simulating = True

    def is_connected(self):
        if not self._connection:
            return False
        return self._connection.is_open

    def _connect_to_port(self, port=None):
        try:
            smoothie_id = environ.get('OT_SMOOTHIE_ID', 'FT232R')
            self._connection = serial_communication.connect(
                device_name=smoothie_id,
                port=port,
                baudrate=self._config.serial_speed
            )
            self.simulating = False
        except SerialException:
            # if another process is using the port, pyserial raises an
            # exception that describes a "readiness to read" which is confusing
            error_msg = 'Unable to access UART port to Smoothie. This is '
            error_msg += 'because another process is currently using it, or '
            error_msg += 'the UART port is disabled on this device (OS)'
            raise SerialException(error_msg)

    @property
    def port(self):
        if not self._connection:
            return None
        return self._connection.port

    def get_fw_version(self):
        '''
        Queries Smoothieware for it's build version, and returns
        the parsed response.

        returns: str
            Current version of attached Smoothi-driver. Versions are derived
            from git branch-hash (eg: edge-66ec883NOMSD)

        Example Smoothieware response:

        Build version: edge-66ec883NOMSD, Build date: Jan 28 2018 15:26:57, MCU: LPC1769, System Clock: 120MHz  # NOQA
          CNC Build   NOMSD Build
        6 axis
        '''
        version = 'Virtual Smoothie'
        if not self.simulating:
            version = self._send_command('version')
            version = version.split(',')[0].split(':')[-1].strip()
            version = version.replace('NOMSD', '')
        return version

    @property
    def position(self):
        """
        Instead of sending M114.2 we are storing target values in
        self._position since movement and home commands are blocking and
        assumed to go the correct place.

        Cases where Smoothie would not be in the correct place (such as if a
        belt slips) would not be corrected by getting position with M114.2
        because Smoothie would also not be aware of slippage.
        """
        return {k.upper(): v for k, v in self._position.items()}

    @property
    def switch_state(self):
        '''Returns the state of all SmoothieBoard limit switches'''
        res = self._send_command(GCODES['LIMIT_SWITCH_STATUS'])
        return _parse_switch_values(res)

    def update_homed_flags(self, flags=None):
        '''
        Returns Smoothieware's current homing-status, which is a dictionary
        of boolean values for each axis (XYZABC). If an axis is False, then it
        still needs to be homed, and it's coordinate cannot be trusted.
        Smoothieware sets it's internal homing flags for all axes to False when
        it has yet to home since booting/restarting, or an endstop/homing error

        returns: dict
            {
                'X': False,
                'Y': True,
                'Z': False,
                'A': True,
                'B': False,
                'C': True
            }
        '''
        if flags and isinstance(flags, dict):
            self.homed_flags.update(flags)

        elif self.simulating:
            self.homed_flags.update({ax: False for ax in AXES})

        elif self.is_connected():

            def _recursive_update_homed_flags(retries):
                try:
                    res = self._send_command(GCODES['HOMING_STATUS'])
                    flags = _parse_homing_status_values(res)
                    self.homed_flags.update(flags)
                except ParseError as e:
                    retries -= 1
                    if retries <= 0:
                        raise e
                    if not self.simulating:
                        sleep(DEFAULT_STABILIZE_DELAY)
                    return _recursive_update_homed_flags(retries)

            _recursive_update_homed_flags(DEFAULT_COMMAND_RETRIES)

    @property
    def current(self):
        return self._current_settings['now']

    @property
    def speed(self):
        pass

    def set_speed(self, value):
        ''' set total axes movement speed in mm/second'''
        self._combined_speed = float(value)
        speed_per_min = int(self._combined_speed * SEC_PER_MIN)
        command = GCODES['SET_SPEED'] + str(speed_per_min)
        log.debug("set_speed: {}".format(command))
        self._send_command(command)

    def push_speed(self):
        self._saved_axes_speed = float(self._combined_speed)

    def pop_speed(self):
        self.set_speed(self._saved_axes_speed)

    def set_axis_max_speed(self, settings):
        '''
        Sets the maximum speed (mm/sec) that a given axis will move

        settings
            Dict with axes as valies (e.g.: 'X', 'Y', 'Z', 'A', 'B', or 'C')
            and floating point number for millimeters per second (mm/sec)
        '''
        self._max_speed_settings.update(settings)
        values = ['{}{}'.format(axis.upper(), value)
                  for axis, value in sorted(settings.items())]
        command = '{} {}'.format(
            GCODES['SET_MAX_SPEED'],
            ' '.join(values)
        )
        log.debug("set_axis_max_speed: {}".format(command))
        self._send_command(command)

    def push_axis_max_speed(self):
        self._saved_max_speed_settings = self._max_speed_settings.copy()

    def pop_axis_max_speed(self):
        self.set_axis_max_speed(self._saved_max_speed_settings)

    def set_active_current(self, settings):
        '''
        Sets the amperage of each motor for when it is activated by driver.
        Values are initialized from the `robot_config.high_current` values,
        and can then be changed through this method by other parts of the API.

        For example, `Pipette` setting the active-current of it's pipette,
        depending on what model pipette it is, and what action it is performing

        settings
            Dict with axes as valies (e.g.: 'X', 'Y', 'Z', 'A', 'B', or 'C')
            and floating point number for current (generally between 0.1 and 2)
        '''
        self._active_current_settings['now'].update(settings)

        # if an axis specified in the `settings` is currently active,
        # reset it's current to the new active-current value
        active_axes_to_update = {
            axis: amperage
            for axis, amperage in self._active_current_settings['now'].items()
            if self._active_axes.get(axis) is True
            if self.current[axis] != amperage
        }
        if active_axes_to_update:
            self._save_current(active_axes_to_update, axes_active=True)

    def push_active_current(self):
        self._active_current_settings['saved'].update(
            self._active_current_settings['now'])

    def pop_active_current(self):
        self.set_active_current(self._active_current_settings['saved'])

    def set_dwelling_current(self, settings):
        '''
        Sets the amperage of each motor for when it is dwelling.
        Values are initialized from the `robot_config.log_current` values,
        and can then be changed through this method by other parts of the API.

        For example, `Pipette` setting the dwelling-current of it's pipette,
        depending on what model pipette it is.

        settings
            Dict with axes as valies (e.g.: 'X', 'Y', 'Z', 'A', 'B', or 'C')
            and floating point number for current (generally between 0.1 and 2)
        '''
        self._dwelling_current_settings['now'].update(settings)

        # if an axis specified in the `settings` is currently dwelling,
        # reset it's current to the new dwelling-current value
        dwelling_axes_to_update = {
            axis: amps
            for axis, amps in self._dwelling_current_settings['now'].items()
            if self._active_axes.get(axis) is False
            if self.current[axis] != amps
        }
        if dwelling_axes_to_update:
            self._save_current(dwelling_axes_to_update, axes_active=False)

    def push_dwelling_current(self):
        self._dwelling_current_settings['saved'].update(
            self._dwelling_current_settings['now'])

    def pop_dwelling_current(self):
        self.set_dwelling_current(self._dwelling_current_settings['saved'])

    def _save_current(self, settings, axes_active=True):
        '''
        Sets the current in Amperes (A) by axis. Currents are limited to be
        between 0.0-2.0 amps per axis motor.

        Note: this method does not send gcode commands, but instead stores the
        desired current setting. A seperate call to _generate_current_command()
        will return a gcode command that can be used to set Smoothie's current

        settings
            Dict with axes as valies (e.g.: 'X', 'Y', 'Z', 'A', 'B', or 'C')
            and floating point number for current (generally between 0.1 and 2)
        '''
        self._active_axes.update({
            ax: axes_active
            for ax in settings.keys()
        })
        self._current_settings['now'].update(settings)
        log.debug("_save_current: {}".format(self.current))

    def _set_saved_current(self):
        '''
        Sends the driver's current settings to the serial port as gcode. Call
        this method to set the axis-current state on the actual Smoothie
        motor-driver.
        '''
        self._send_command(self._generate_current_command())

    def _generate_current_command(self):
        '''
        Returns a constructed GCode string that contains this driver's
        axis-current settings, plus a small delay to wait for those settings
        to take effect.
        '''
        values = ['{}{}'.format(axis, value)
                  for axis, value in sorted(self.current.items())]
        current_cmd = '{} {}'.format(
            GCODES['SET_CURRENT'],
            ' '.join(values)
        )
        command = '{currents} {code}P{seconds}'.format(
            currents=current_cmd,
            code=GCODES['DWELL'],
            seconds=CURRENT_CHANGE_DELAY
        )
        log.debug("_generate_current_command: {}".format(command))
        return command

    def disengage_axis(self, axes):
        '''
        Disable the stepper-motor-driver's 36v output to motor
        This is a safe GCODE to send to Smoothieware, as it will automatically
        re-engage the motor if it receives a home or move command

        axes
            String containing the axes to be disengaged
            (e.g.: 'XY' or 'ZA' or 'XYZABC')
        '''
        axes = ''.join(set(axes.upper()) & set(AXES))
        if axes:
            log.debug("disengage_axis: {}".format(axes))
            self._send_command(GCODES['DISENGAGE_MOTOR'] + axes)
            for axis in axes:
                self.engaged_axes[axis] = False

    def dwell_axes(self, axes):
        '''
        Sets motors to low current, for when they are not moving.

        Dwell for XYZA axes is only called after HOMING
        Dwell for BC axes is called after both HOMING and MOVING

        axes:
            String containing the axes to set to low current (eg: 'XYZABC')
        '''
        axes = ''.join(set(axes) & set(AXES) - set(DISABLE_AXES))
        dwelling_currents = {
            ax: self._dwelling_current_settings['now'][ax]
            for ax in axes
            if self._active_axes[ax] is True
        }
        if dwelling_currents:
            self._save_current(dwelling_currents, axes_active=False)

    def activate_axes(self, axes):
        '''
        Sets motors to a high current, for when they are moving
        and/or must hold position

        Activating XYZABC axes before both HOMING and MOVING

        axes:
            String containing the axes to set to high current (eg: 'XYZABC')
        '''
        axes = ''.join(set(axes) & set(AXES) - set(DISABLE_AXES))
        active_currents = {
            ax: self._active_current_settings['now'][ax]
            for ax in axes
            if self._active_axes[ax] is False
        }
        if active_currents:
            self._save_current(active_currents, axes_active=True)

    # ----------- Private functions --------------- #

    def _wait_for_ack(self):
        '''
        In the case where smoothieware has just been reset, we want to
        ignore all the garbage it spits out

        This methods writes a sequence of newline characters, which will
        guarantee Smoothieware responds with 'ok\r\nok\r\n' within 3 seconds
        '''
        self._send_command('\r\n', timeout=SMOOTHIE_BOOT_TIMEOUT)

    def _reset_from_error(self):
        # smoothieware will ignore new messages for a short time
        # after it has entered an error state, so sleep for some milliseconds
        if not self.simulating:
            sleep(DEFAULT_STABILIZE_DELAY)
        log.debug("reset_from_error")
        self._send_command(GCODES['RESET_FROM_ERROR'])
        self.update_homed_flags()

    # Potential place for command optimization (buffering, flushing, etc)
    def _send_command(self, command, timeout=DEFAULT_SMOOTHIE_TIMEOUT):
        """
        Submit a GCODE command to the robot, followed by M400 to block until
        done. This method also ensures that any command on the B or C axis
        (the axis for plunger control) do current ramp-up and ramp-down, so
        that plunger motors rest at a low current to prevent burn-out.

        In the case of a limit-switch alarm during any command other than home,
        the robot should home the axis from the alarm and then raise a
        SmoothieError. The robot should *not* recover and continue to run the
        protocol, as this could result in unpredicable handling of liquids.
        When a SmoothieError is raised, the user should inspect the physical
        configuration of the robot and the protocol and determine why the limit
        switch was hit unexpectedly. This is usually due to an undetected
        collision in a previous move command.

        :param command: the GCODE to submit to the robot
        :param timeout: the time to wait before returning (indefinite wait if
            this is set to none
        """
        if self.simulating:
            return

        command_line = command + ' ' + SMOOTHIE_COMMAND_TERMINATOR
        ret_code = self._recursive_write_and_return(
            command_line, timeout, DEFAULT_COMMAND_RETRIES)

        ret_code = self._remove_unwanted_characters(command_line, ret_code)

        # Smoothieware returns error state if a switch was hit while moving
        if (ERROR_KEYWORD in ret_code.lower()) or \
                (ALARM_KEYWORD in ret_code.lower()):
            self._reset_from_error()
            error_axis = ret_code.strip()[-1]
            if GCODES['HOME'] not in command and error_axis in 'XYZABC':
                self.home(error_axis)
            raise SmoothieError(ret_code)

        return ret_code.strip()

    def _remove_unwanted_characters(self, command, response):
        # smoothieware can enter a weird state, where it repeats back
        # the sent command at the beginning of its response.
        # Check for this echo, and strips the command from the response
        remove_from_response = [
            c.strip() for c in command.strip().split(' ') if c.strip()]

        # also removing any inadvertant newline/return characters
        # this is ok because all data we need from Smoothie is returned on
        # the first line in the response
        remove_from_response += ['\r', '\n']
        modified_response = str(response)

        for cmd in remove_from_response:
            modified_response = modified_response.replace(cmd, '')

        if modified_response != response:
            log.debug('Removed characters from response: {}'.format(
                response))
            log.debug('Newly formatted response: {}'.format(modified_response))

        return modified_response

    def _recursive_write_and_return(self, cmd, timeout, retries):
        try:
            return serial_communication.write_and_return(
                cmd,
                SMOOTHIE_ACK,
                self._connection,
                timeout=timeout)
        except serial_communication.SerialNoResponse as e:
            retries -= 1
            if retries <= 0:
                raise e
            if not self.simulating:
                sleep(DEFAULT_STABILIZE_DELAY)
            if self._connection:
                self._connection.close()
                self._connection.open()
            return self._recursive_write_and_return(
                cmd, timeout, retries)

    def _home_x(self):
        log.debug("_home_x")
        # move the gantry forward on Y axis with low power
        self._save_current({'Y': Y_BACKOFF_LOW_CURRENT})
        self.push_speed()
        self.set_speed(Y_BACKOFF_SLOW_SPEED)

        # move away from the Y endstop switch, then backward half that distance
        relative_retract_command = '{0} {1}Y{2} {3}Y{4} {5}'.format(
            GCODES['RELATIVE_COORDS'],  # set to relative coordinate system
            GCODES['MOVE'],             # move towards front of machine
            str(int(-Y_SWITCH_BACK_OFF_MM)),
            GCODES['MOVE'],             # move towards back of machine
            str(int(Y_SWITCH_REVERSE_BACK_OFF_MM)),
            GCODES['ABSOLUTE_COORDS']   # set back to abs coordinate system
        )

        command = '{0} {1}'.format(
            self._generate_current_command(), relative_retract_command)
        self._send_command(command, timeout=DEFAULT_MOVEMENT_TIMEOUT)
        self.pop_speed()
        self.dwell_axes('Y')

        # now it is safe to home the X axis
        try:
            self.activate_axes('X')
            command = '{0} {1}'.format(
                self._generate_current_command(),
                GCODES['HOME'] + 'X'
            )
            self._send_command(command, timeout=DEFAULT_MOVEMENT_TIMEOUT)
            self.update_homed_flags(flags={'X': True})
        finally:
            self.dwell_axes('X')
            self._set_saved_current()

    def _home_y(self):
        log.debug("_home_y")
        self.activate_axes('Y')
        # home the Y at normal speed (fast)
        command = '{0} {1}'.format(
            self._generate_current_command(),
            GCODES['HOME'] + 'Y'
        )
        self._send_command(command, timeout=DEFAULT_MOVEMENT_TIMEOUT)

        # slow the maximum allowed speed on Y axis
        self.push_axis_max_speed()
        self.set_axis_max_speed({'Y': Y_RETRACT_SPEED})

        # retract, then home, then retract again
        relative_retract_command = '{0} {1}Y{2} {3}'.format(
            GCODES['RELATIVE_COORDS'],  # set to relative coordinate system
            GCODES['MOVE'],             # move 3 millimeters away from switch
            str(-Y_RETRACT_DISTANCE),
            GCODES['ABSOLUTE_COORDS']   # set back to abs coordinate system
        )
        try:
            self._send_command(
                relative_retract_command, timeout=DEFAULT_MOVEMENT_TIMEOUT)
            self._send_command(
                GCODES['HOME'] + 'Y', timeout=DEFAULT_MOVEMENT_TIMEOUT)
            self.update_homed_flags(flags={'Y': True})
            self._send_command(
                relative_retract_command, timeout=DEFAULT_MOVEMENT_TIMEOUT)
            self.pop_axis_max_speed()  # bring max speeds back to normal
        finally:
            self.dwell_axes('Y')
            self._set_saved_current()

    def _setup(self):
        log.debug("_setup")
        try:
            self._wait_for_ack()
        except serial_communication.SerialNoResponse:
            # incase motor-driver is stuck in bootloader and unresponsive,
            # use gpio to reset into a known state
            self._smoothie_reset()
        self._reset_from_error()
        self._send_command(self._config.acceleration)
        self._send_command(self._config.steps_per_mm)
        self._send_command(GCODES['ABSOLUTE_COORDS'])
        self._save_current(self.current, axes_active=False)
        self.update_position(default=self.homed_position)
        self.pop_axis_max_speed()
        self.pop_speed()

    def _read_from_pipette(self, gcode, mount):
        '''
        Read from an attached pipette's internal memory. The gcode used
        determines which portion of memory is read and returned.

        All motors must be disengaged to consistently read over I2C lines

        gcode:
            String (str) containing a GCode
            either 'READ_INSTRUMENT_ID' or 'READ_INSTRUMENT_MODEL'
        mount:
            String (str) with value 'left' or 'right'
        '''
        allowed_mounts = {'left': 'L', 'right': 'R'}
        mount = allowed_mounts.get(mount)
        if not mount:
            raise ValueError('Unexpected mount: {}'.format(mount))
        try:
            # EMI interference from both plunger motors has been found to
            # prevent the I2C lines from communicating between Smoothieware and
            # pipette's onboard EEPROM. To avoid, turn off both plunger motors
            self.disengage_axis('BC')
            self.delay(CURRENT_CHANGE_DELAY)
            # request from Smoothieware the information from that pipette
            res = self._send_command(gcode + mount)
            if res:
                res = _parse_instrument_data(res)
                assert mount in res
                # data is read/written as strings of HEX characters
                # to avoid firmware weirdness in how it parses GCode arguments
                return _byte_array_to_ascii_string(res[mount])
        except (ParseError, AssertionError, SmoothieError):
            pass

    def _write_to_pipette(self, gcode, mount, data_string):
        '''
        Write to an attached pipette's internal memory. The gcode used
        determines which portion of memory is written to.

        NOTE: To enable write-access to the pipette, it's button must be held

        gcode:
            String (str) containing a GCode
            either 'WRITE_INSTRUMENT_ID' or 'WRITE_INSTRUMENT_MODEL'
        mount:
            String (str) with value 'left' or 'right'
        data_string:
            String (str) that is of unkown length
        '''
        allowed_mounts = {'left': 'L', 'right': 'R'}
        mount = allowed_mounts.get(mount)
        if not mount:
            raise ValueError('Unexpected mount: {}'.format(mount))
        if not isinstance(data_string, str):
            raise ValueError(
                'Expected {0}, not {1}'.format(str, type(data_string)))
        # EMI interference from both plunger motors has been found to
        # prevent the I2C lines from communicating between Smoothieware and
        # pipette's onboard EEPROM. To avoid, turn off both plunger motors
        self.disengage_axis('BC')
        self.delay(CURRENT_CHANGE_DELAY)
        # data is read/written as strings of HEX characters
        # to avoid firmware weirdness in how it parses GCode arguments
        byte_string = _byte_array_to_hex_string(
            bytearray(data_string.encode()))
        command = gcode + mount + byte_string
        log.debug("_write_to_pipette: {}".format(command))
        self._send_command(command)

    # ----------- END Private functions ----------- #

    # ----------- Public interface ---------------- #
    def move(self, target, home_flagged_axes=False):
        '''
        Move to the `target` Smoothieware coordinate, along any of the size
        axes, XYZABC.

        target: dict
            dict setting the coordinate that Smoothieware will be at when
            `move()` returns. `target` keys are the axis in upper-case, and the
            values are the coordinate in millimeters (float)

        home_flagged_axes: boolean (default=False)
            If set to `True`, each axis included within the target coordinate
            may be homed before moving, determined by Smoothieware's internal
            homing-status flags (`True` means it has already homed). All axes'
            flags are set to `False` by Smoothieware under three conditions:
            1) Smoothieware boots or resets, 2) if a HALT gcode or signal
            is sent, or 3) a homing/limitswitch error occured.
        '''
        from numpy import isclose

        self.run_flag.wait()

        def valid_movement(coords, axis):
            return not (
                (axis in DISABLE_AXES) or
                (coords is None) or
                isclose(coords, self.position[axis])
            )

        def create_coords_list(coords_dict):
            return [
                axis + str(round(coords, GCODE_ROUNDING_PRECISION))
                for axis, coords in sorted(coords_dict.items())
                if valid_movement(coords, axis)
            ]

        backlash_target = target.copy()
        backlash_target.update({
            axis: value + PLUNGER_BACKLASH_MM
            for axis, value in sorted(target.items())
            if axis in 'BC' and self.position[axis] < value
        })

        target_coords = create_coords_list(target)
        backlash_coords = create_coords_list(backlash_target)

        if target_coords:
            non_moving_axes = ''.join([
                ax
                for ax in AXES
                if ax not in target.keys()
            ])
            self.dwell_axes(non_moving_axes)
            self.activate_axes(target.keys())

            # include the current-setting gcodes within the moving gcode string
            # to reduce latency, since we're setting current so much
            command = self._generate_current_command()

            if backlash_coords != target_coords:
                command += ' ' + GCODES['MOVE'] + ''.join(backlash_coords)
            command += ' ' + GCODES['MOVE'] + ''.join(target_coords)

            try:
                for axis in target.keys():
                    self.engaged_axes[axis] = True
                if home_flagged_axes:
                    self.home_flagged_axes(''.join(list(target.keys())))
                log.debug("move: {}".format(command))
                # TODO (andy) a movement's timeout should be calculated by
                # how long the movement is expected to take. A default timeout
                # of 30 seconds prevents any movements that take longer
                self._send_command(command, timeout=DEFAULT_MOVEMENT_TIMEOUT)
            finally:
                # dwell pipette motors because they get hot
                plunger_axis_moved = ''.join(set('BC') & set(target.keys()))
                if plunger_axis_moved:
                    self.dwell_axes(plunger_axis_moved)
                    self._set_saved_current()

            self._update_position(target)

    def home(self, axis=AXES, disabled=DISABLE_AXES):

        self.run_flag.wait()

        axis = axis.upper()

        # If Y is requested make sure we home X first
        if 'Y' in axis:
            axis += 'X'
        # If horizontal movement is requested, ensure we raise the instruments
        if 'X' in axis:
            axis += 'ZA'
        # These two additions are safe even if they duplicate requested axes
        # because of the use of set operations below, which will de-duplicate
        # characters from the resulting string

        # HOME_SEQUENCE defines a pattern for homing, specifically that the
        # ZABC axes should be homed first so that horizontal movement doesn't
        # happen with the pipette down (which could bump into things). Then
        # the X axis is homed, which has to happen before Y. Finally Y can be
        # homed. This variable will contain the sequence just explained, but
        # filters out unrequested axes using set intersection (&) and then
        # filters out disabled axes using set difference (-)
        home_sequence = list(filter(
            None,
            [
                ''.join(set(group) & set(axis) - set(disabled))
                for group in HOME_SEQUENCE
            ]))

        non_moving_axes = ''.join([
            ax
            for ax in AXES
            if ax not in home_sequence
        ])
        self.dwell_axes(non_moving_axes)

        for axes in home_sequence:
            if 'X' in axes:
                self._home_x()
            elif 'Y' in axes:
                self._home_y()
            else:
                # if we are homing neither the X nor Y axes, simple home
                self.activate_axes(axes)

                # include the current-setting gcodes within the moving gcode
                # string to reduce latency, since we're setting current so much
                command = self._generate_current_command()
                command += ' ' + GCODES['HOME'] + ''.join(sorted(axes))
                try:
                    log.debug("home: {}".format(command))
                    self._send_command(
                        command, timeout=DEFAULT_MOVEMENT_TIMEOUT)
                    self.update_homed_flags(flags={ax: True for ax in axes})
                finally:
                    # always dwell an axis after it has been homed
                    self.dwell_axes(axes)
                    self._set_saved_current()

        # Only update axes that have been selected for homing
        homed = {
            ax: self.homed_position.get(ax)
            for ax in ''.join(home_sequence)
        }
        self.update_position(default=homed)
        for axis in ''.join(home_sequence):
            self.engaged_axes[axis] = True

        # coordinate after homing might not synce with default in API
        # so update this driver's homed position using current coordinates
        new = {
            ax: self.position[ax]
            for ax in self.homed_position.keys()
            if ax in axis
        }
        self._homed_position.update(new)

        return self.position

    def fast_home(self, axis, safety_margin):
        ''' home after a controlled motor stall

        Given a known distance we have just stalled along an axis, move
        that distance away from the homing switch. Then finish with home.
        '''
        # move some mm distance away from the target axes endstop switch(es)
        destination = {
            ax: self.homed_position.get(ax) - abs(safety_margin)
            for ax in axis.upper()
        }

        # there is a chance the axis will hit it's home switch too soon
        # if this happens, catch the error and continue with homing afterwards
        try:
            self.move(destination)
        except SmoothieError:
            pass

        # then home once we're closer to the endstop(s)
        disabled = ''.join([ax for ax in AXES if ax not in axis.upper()])
        return self.home(axis=axis, disabled=disabled)

    def pause(self):
        if not self.simulating:
            self.run_flag.clear()

    def resume(self):
        if not self.simulating:
            self.run_flag.set()

    def delay(self, seconds):
        # per http://smoothieware.org/supported-g-codes:
        # In grbl mode P is float seconds to comply with gcode standards
        command = '{code}P{seconds}'.format(
            code=GCODES['DWELL'],
            seconds=seconds
        )
        log.debug("delay: {}".format(command))
        self._send_command(command, timeout=int(seconds) + 1)

    def probe_axis(self, axis, probing_distance) -> Dict[str, float]:
        if axis.upper() in AXES:
            self.engaged_axes[axis] = True
            command = GCODES['PROBE'] + axis.upper() + str(probing_distance)
            log.debug("probe_axis: {}".format(command))
            self._send_command(
                command=command, timeout=DEFAULT_MOVEMENT_TIMEOUT)
            self.update_position(self.position)
            return self.position
        else:
            raise RuntimeError("Cant probe axis {}".format(axis))

    def kill(self):
        """
        In order to terminate Smoothie motion immediately (including
        interrupting a command in progress, we set the reset pin low and then
        back to high, then call `_setup` method to send the RESET_FROM_ERROR
        Smoothie code to return Smoothie to a normal waiting state and reset
        any other state needed for the driver.
        """
        log.debug("kill")
        self._smoothie_hard_halt()
        self._reset_from_error()
        self._setup()

    def home_flagged_axes(self, axes_string):
        '''
        Given a list of axes to check, this method will home each axis if
        Smoothieware's internal flag sets it as needing to be homed
        '''
        axes_that_need_to_home = [
            axis
            for axis, already_homed in self.homed_flags.items()
            if (not already_homed) and (axis in axes_string)
        ]
        if axes_that_need_to_home:
            axes_string = ''.join(axes_that_need_to_home)
            self.home(axes_string)

    def _smoothie_reset(self):
        log.debug('Resetting Smoothie (simulating: {})'.format(
            self.simulating))
        if self.simulating:
            pass
        else:
            gpio.set_low(gpio.OUTPUT_PINS['RESET'])
            gpio.set_high(gpio.OUTPUT_PINS['ISP'])
            sleep(0.25)
            gpio.set_high(gpio.OUTPUT_PINS['RESET'])
            sleep(0.25)
            self._wait_for_ack()
            self._reset_from_error()

    def _smoothie_programming_mode(self):
        log.debug('Setting Smoothie to ISP mode (simulating: {})'.format(
            self.simulating))
        if self.simulating:
            pass
        else:
            gpio.set_low(gpio.OUTPUT_PINS['RESET'])
            gpio.set_low(gpio.OUTPUT_PINS['ISP'])
            sleep(0.25)
            gpio.set_high(gpio.OUTPUT_PINS['RESET'])
            sleep(0.25)
            gpio.set_high(gpio.OUTPUT_PINS['ISP'])
            sleep(0.25)

    def _smoothie_hard_halt(self):
        log.debug('Halting Smoothie (simulating: {})'.format(
            self.simulating))
        if self.simulating:
            pass
        else:
            gpio.set_low(gpio.OUTPUT_PINS['HALT'])
            sleep(0.25)
            gpio.set_high(gpio.OUTPUT_PINS['HALT'])
            sleep(0.25)

    # ----------- END Public interface ------------ #
