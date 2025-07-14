# Support for a manual controlled stepper
#
# Copyright (C) 2019-2021  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import stepper, chelper
from . import force_move
from kinematics import extruder

class ManualStepper:
    def __init__(self, config):
        self.printer = config.get_printer()
        # Setup iterative solver
        ffi_main, ffi_lib = chelper.get_ffi()
        if config.get('endstop_pin', None) is not None:
            self.can_home = True
            self.rail = stepper.PrinterRail(
                config, need_position_minmax=False,
                default_position_endstop=0.)
            self.steppers = self.rail.get_steppers()
            self.homed_flag = False
        else:
            self.can_home = False
            self.rail = stepper.PrinterStepper(config)
            self.steppers = [self.rail]
            #----------------------------------------------
            self.config_pa = config.getfloat(
                'pressure_advance', 0., minval=0.)
            self.config_smooth_time = config.getfloat(
                'pressure_advance_smooth_time', 0.040, above=0., maxval=.200)
            self.sk_extruder = ffi_main.gc(
                ffi_lib.extruder_stepper_alloc(),
                ffi_lib.extruder_stepper_free)
            self.rail.set_stepper_kinematics(self.sk_extruder)
            # Register commands
            self.printer.register_event_handler("klippy:connect",
                                                self._handle_connect)
            #----------------------------------------------
        self.velocity = config.getfloat('velocity', 5., above=0.)
        self.accel = self.homing_accel = config.getfloat(
            'accel', 0., minval=0.)
        self.next_cmd_time = 0.
        #----------------------------------------------
        self.pressure_advance = self.pressure_advance_smooth_time = 0.
        self.motion_queue = None
        self.sync_advance_distance = config.getfloat(
            'sync_advance_distance', 0., minval=0.)
        self.sync_advance_velocity = config.getfloat(
            'sync_advance_velocity', self.velocity, above=0.)
        self.sync_advance_accel = config.getfloat(
            'sync_advance_accel', self.accel, minval=0.)
        self.my_self = self
        self.show_info = 1
        # funtion button
        self.funtion_button = config.get('function_button', None)
        if self.funtion_button == 'set_rotation_distance':
            # button1, must be set
            self.button_list = {}
            buttons = self.printer.load_object(config, "buttons")
            button1_pin = config.get('button1_pin')
            press_set_data = config.getfloat(
                'button1_press_set_data', None)
            release_set_data = config.getfloat(
                'button1_release_set_data', None)
            self.button_list.update(
                {'button1': {
                    'pin': button1_pin,
                    'press': press_set_data,
                    'release': release_set_data,
                    'state': None,}})
            if config.get('button1_analog_range', None) is None:
                buttons.register_debounce_button(
                    button1_pin, self.button1_callback, config)
            else:
                amin, amax = config.getfloatlist(
                    'button1_analog_range', count=2)
                pullup = config.getfloat(
                    'button1_analog_pullup_resistor', 4700., above=0.)
                buttons.register_debounce_adc_button(
                    button1_pin, amin, amax, pullup,
                    self.button1_callback, config)
            # button2, if not None:
            button2_pin = config.get('button2_pin', None)
            if button2_pin is not None:
                press_set_data = config.getfloat(
                    'button2_press_set_data', None)
                release_set_data = config.getfloat(
                    'button2_release_set_data', None)
                self.button_list.update(
                    {'button2': {
                        'pin': button2_pin,
                        'press': press_set_data,
                        'release': release_set_data,
                        'state': None,}})
                if config.get('button2_analog_range', None) is None:
                    buttons.register_debounce_button(
                        button2_pin, self.button2_callback, config)
                else:
                    amin, amax = config.getfloatlist(
                        'button2_analog_range', count=2)
                    pullup = config.getfloat(
                        'button2_analog_pullup_resistor', 4700., above=0.)
                    buttons.register_debounce_adc_button(
                        button2_pin, amin, amax, pullup,
                        self.button2_callback, config)
        #----------------------------------------------

        self.trapq = ffi_main.gc(ffi_lib.trapq_alloc(), ffi_lib.trapq_free)
        self.trapq_append = ffi_lib.trapq_append
        self.trapq_finalize_moves = ffi_lib.trapq_finalize_moves
        self.rail.setup_itersolve('cartesian_stepper_alloc', b'x')
        self.rail.set_trapq(self.trapq)

        # Register commands
        self.stepper_name = config.get_name().split()[1]
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command(
            'MANUAL_STEPPER', "STEPPER", self.stepper_name,
            self.cmd_MANUAL_STEPPER, desc=self.cmd_MANUAL_STEPPER_help)

        gcode.register_mux_command(
            'MANUAL_STEPPER_SHOW', "STEPPER", self.stepper_name,
            self.cmd_MANUAL_STEPPER_SHOW, desc=self.cmd_MANUAL_STEPPER_SHOW_help)
        gcode.register_mux_command(
            'SYNC_MANUAL_STEPPER', 'STEPPER', self.stepper_name,
            self.cmd_SYNC_MANUAL_MOTION, desc=self.cmd_SYNC_MANUAL_MOTION_help)
        gcode.register_mux_command(
            'MANUAL_STEPPER_ROTATION_DISTANCE', "STEPPER",
            self.stepper_name, self.cmd_MANUAL_STEPPER_ROTATION_DISTANCE,
            desc=self.cmd_MANUAL_STEPPER_ROTATION_DISTANCE_help)
        self.printer.add_object('manual_stepper '+self.stepper_name, self)

    #----------------------------------------------
    def _handle_connect(self):
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.register_step_generator(self.rail.generate_steps)
        self._set_pressure_advance(self.config_pa, self.config_smooth_time)
    def get_status(self, eventtime):
        state = {'pressure_advance': self.pressure_advance,
                'smooth_time': self.pressure_advance_smooth_time,
                'motion_queue': self.motion_queue,
                'rotation_distance': self.steppers[0].get_rotation_distance(),
                'show_info': self.show_info,}
        if self.can_home:
            state['homed'] = self.homed_flag
        return state
    def _set_pressure_advance(self, pressure_advance, smooth_time):
        old_smooth_time = self.pressure_advance_smooth_time
        if not self.pressure_advance:
            old_smooth_time = 0.
        new_smooth_time = smooth_time
        if not pressure_advance:
            new_smooth_time = 0.
        toolhead = self.printer.lookup_object("toolhead")
        if new_smooth_time != old_smooth_time:
            toolhead.note_step_generation_scan_time(
                    new_smooth_time * .5, old_delay=old_smooth_time * .5)
        ffi_main, ffi_lib = chelper.get_ffi()
        espa = ffi_lib.extruder_set_pressure_advance
        toolhead.register_lookahead_callback(
            lambda print_time: espa(self.sk_extruder, print_time,
                                    pressure_advance, new_smooth_time))
        self.pressure_advance = pressure_advance
        self.pressure_advance_smooth_time = smooth_time
    def sync_to_extruder(self, extruder_name):
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.flush_step_generation()
        if not extruder_name:
            if self.motion_queue is not None:
                extruder = self.printer.lookup_object(self.motion_queue, None)
                if extruder is None:
                    raise self.printer.command_error(
                            "'%s' is not a valid extruder." %
                            (self.motion_queue,))
                extruder.del_sync_stepper(self.stepper_name)
            old_motion_queue = self.motion_queue
            self.rail.set_trapq(self.trapq)
            self.motion_queue = None
            return old_motion_queue
        extruder = self.printer.lookup_object(extruder_name, None)
        if extruder is None:
            raise self.printer.command_error(
                    "'%s' is not a valid extruder." % (extruder_name,))
        if not extruder.has_multi_sync():
            # The extruder only supports the synchronization of a single motor.
            # So we need to unsynchronize the other motors.
            sync_steppers = extruder.get_sync_steppers().copy()
            for name, obj_name in sync_steppers.items():
                obj = self.printer.lookup_object(obj_name)
                obj.sync_to_extruder(None)
        extruder.add_sync_stepper(
            self.stepper_name, 'manual_stepper '+self.stepper_name)
        #self.rail.set_stepper_kinematics(self.sk_extruder)
        self._set_pressure_advance(extruder.extruder_stepper.pressure_advance,
            extruder.extruder_stepper.pressure_advance_smooth_time)
        self.rail.set_position([extruder.last_position, 0., 0.])
        self.rail.set_trapq(extruder.get_trapq())
        self.motion_queue = extruder_name
        self.do_enable(1)
        # logging.info("name:%s motion:%s" %
        #     (self.stepper_name, self.motion_queue,))
        return None
    def get_sync_extruder_name(self):
        return motion_queue
    def do_sync_advance_move(self, dir):
        if (self.sync_advance_distance > 0.000001):
            old_sync = self.sync_to_extruder(None)
            self.do_set_position(0.)
            self.do_move((self.sync_advance_distance * dir),
                self.sync_advance_velocity, self.sync_advance_accel)
            self.sync_to_extruder(old_sync)
            return True
        return False
    
    # button callback function
    def button1_callback(self, eventtime, state):
        if self.funtion_button == 'set_rotation_distance':
            if state:
                rotation_dist = self.button_list['button1']['press']
                state_name = 'PRESSED'
            else:
                rotation_dist = self.button_list['button1']['release']
                state_name = 'RELEASED'
            # set the rotation distance
            if rotation_dist is not None:
                invert_dir, orig_invert_dir = (
                    self.steppers[0].get_dir_inverted())
                next_invert_dir = orig_invert_dir
                if rotation_dist < 0.:
                    next_invert_dir = not orig_invert_dir
                    rotation_dist = -rotation_dist
                # toolhead = self.printer.lookup_object('toolhead')
                # toolhead.flush_step_generation()
                for s in self.steppers:
                    s.set_rotation_distance(rotation_dist)
                    s.set_dir_inverted(next_invert_dir)
            self.button_list['button1'].update({'state': state_name})
            # if self.show_info:
            #     gcmd = self.printer.lookup_object('gcode')
            #     gcmd.respond_info(
            #         "manual_stepper '%s' rotation_distance is %0.6f"
            #         % (self.stepper_name, rotation_dist))
    def button2_callback(self, eventtime, state):
        if self.funtion_button == 'set_rotation_distance':
            if state:
                rotation_dist = self.button_list['button2']['press']
                state_name = 'PRESSED'
            else:
                rotation_dist = self.button_list['button2']['release']
                state_name = 'RELEASED'
            # set the rotation distance
            if rotation_dist is not None:
                invert_dir, orig_invert_dir = (
                    self.steppers[0].get_dir_inverted())
                next_invert_dir = orig_invert_dir
                if rotation_dist < 0.:
                    next_invert_dir = not orig_invert_dir
                    rotation_dist = -rotation_dist
                # toolhead = self.printer.lookup_object('toolhead')
                # toolhead.flush_step_generation()
                for s in self.steppers:
                    s.set_rotation_distance(rotation_dist)
                    s.set_dir_inverted(next_invert_dir)
            self.button_list['button2'].update({'state': state_name})
            # if self.show_info:
            #     gcmd = self.printer.lookup_object('gcode')
            #     gcmd.respond_info(
            #         "manual_stepper '%s' rotation_distance is %0.6f"
            #         % (self.stepper_name, rotation_dist))
                
    #----------------------------------------------
    def sync_print_time(self):
        toolhead = self.printer.lookup_object('toolhead')
        print_time = toolhead.get_last_move_time()
        if self.next_cmd_time > print_time:
            toolhead.dwell(self.next_cmd_time - print_time)
        else:
            self.next_cmd_time = print_time
    def do_enable(self, enable):
        self.sync_print_time()
        stepper_enable = self.printer.lookup_object('stepper_enable')
        if enable:
            for s in self.steppers:
                se = stepper_enable.lookup_enable(s.get_name())
                se.motor_enable(self.next_cmd_time)
        else:
            for s in self.steppers:
                se = stepper_enable.lookup_enable(s.get_name())
                se.motor_disable(self.next_cmd_time)
            if self.can_home:
                self.homed_flag = False
        self.sync_print_time()
    def do_set_position(self, setpos):
        self.rail.set_position([setpos, 0., 0.])
    def do_move(self, movepos, speed, accel, sync=True):
        self.sync_print_time()
        cp = self.rail.get_commanded_position()
        dist = movepos - cp
        axis_r, accel_t, cruise_t, cruise_v = force_move.calc_move_time(
            dist, speed, accel)
        self.trapq_append(self.trapq, self.next_cmd_time,
                          accel_t, cruise_t, accel_t,
                          cp, 0., 0., axis_r, 0., 0.,
                          0., cruise_v, accel)
        self.next_cmd_time = self.next_cmd_time + accel_t + cruise_t + accel_t
        self.rail.generate_steps(self.next_cmd_time)
        self.trapq_finalize_moves(self.trapq, self.next_cmd_time + 99999.9,
                                  self.next_cmd_time + 99999.9)
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.note_mcu_movequeue_activity(self.next_cmd_time)
        if sync:
            self.sync_print_time()
    def do_homing_move(self, movepos, speed, accel, triggered, check_trigger):
        if not self.can_home:
            raise self.printer.command_error(
                "No endstop for this manual stepper")
        self.homing_accel = accel
        pos = [movepos, 0., 0., 0.]
        endstops = self.rail.get_endstops()
        phoming = self.printer.lookup_object('homing')
        phoming.manual_home(self, endstops, pos, speed,
                            triggered, check_trigger)
        if self.can_home:
            self.homed_flag = True
    cmd_MANUAL_STEPPER_SHOW_help = "Enable and disable the manual_stepper "\
        "information display."
    def cmd_MANUAL_STEPPER_SHOW(self, gcmd):
        show_info = gcmd.get_int('SHOW', None)
        if show_info is not None:
            self.show_info = show_info
    cmd_MANUAL_STEPPER_help = "Command a manually configured stepper"
    def cmd_MANUAL_STEPPER(self, gcmd):
        if self.motion_queue is not None:
            gcmd.respond_info("MANUAL_STEPPER error: "
                            "Stepper '%s' now syncing with '%s'"
                            % (self.stepper_name, self.motion_queue))
            return
        enable = gcmd.get_int('ENABLE', None)
        if enable is not None:
            self.do_enable(enable)
        setpos = gcmd.get_float('SET_POSITION', None)
        if setpos is not None:
            self.do_set_position(setpos)
        speed = gcmd.get_float('SPEED', self.velocity, above=0.)
        accel = gcmd.get_float('ACCEL', self.accel, minval=0.)
        homing_move = gcmd.get_int('STOP_ON_ENDSTOP', 0)
        if homing_move:
            movepos = gcmd.get_float('MOVE')
            self.do_homing_move(movepos, speed, accel,
                                homing_move > 0, abs(homing_move) == 1)
        elif gcmd.get_float('MOVE', None) is not None:
            movepos = gcmd.get_float('MOVE')
            sync = gcmd.get_int('SYNC', 1)
            self.do_move(movepos, speed, accel, sync)
        elif gcmd.get_int('SYNC', 0):
            self.sync_print_time()
    # Toolhead wrappers to support homing
    def flush_step_generation(self):
        self.sync_print_time()
    def get_position(self):
        return [self.rail.get_commanded_position(), 0., 0., 0.]
    def set_position(self, newpos, homing_axes=()):
        self.do_set_position(newpos[0])
    def get_last_move_time(self):
        self.sync_print_time()
        return self.next_cmd_time
    def dwell(self, delay):
        self.next_cmd_time += max(0., delay)
    def drip_move(self, newpos, speed, drip_completion):
        self.do_move(newpos[0], speed, self.homing_accel)
    def get_kinematics(self):
        return self
    def get_steppers(self):
        return self.steppers
    def calc_position(self, stepper_positions):
        return [stepper_positions[self.rail.get_name()], 0., 0.]
    cmd_SYNC_MANUAL_MOTION_help = "Set manual stepper motion queue"
    def cmd_SYNC_MANUAL_MOTION(self, gcmd):
        ename = gcmd.get('MOTION_QUEUE', None)
        self.sync_to_extruder(ename)
        if self.show_info:
            gcmd.respond_info("Stepper '%s' now syncing with '%s'"
                              % (self.stepper_name, ename))
    cmd_MANUAL_STEPPER_ROTATION_DISTANCE_help = \
        "Set or get manual_stepper rotation distance."
    def cmd_MANUAL_STEPPER_ROTATION_DISTANCE(self, gcmd):
        rotation_dist = gcmd.get_float('DISTANCE', None)
        if rotation_dist is not None:
            if not rotation_dist:
                raise gcmd.error("rotation_distance can not be zero")
            invert_dir, orig_invert_dir = self.steppers[0].get_dir_inverted()
            next_invert_dir = orig_invert_dir
            if rotation_dist < 0.:
                next_invert_dir = not orig_invert_dir
                rotation_dist = -rotation_dist
            toolhead = self.printer.lookup_object('toolhead')
            toolhead.flush_step_generation()
            for s in self.steppers:
                s.set_rotation_distance(rotation_dist)
                s.set_dir_inverted(next_invert_dir)
        else:
            rotation_dist, spr = self.steppers[0].get_rotation_distance()
        invert_dir, orig_invert_dir = self.steppers[0].get_dir_inverted()
        if invert_dir != orig_invert_dir:
            rotation_dist = -rotation_dist
        if self.show_info:
            gcmd.respond_info("manual_stepper '%s' rotation_distance is %0.6f"
                              % (self.stepper_name, rotation_dist))

def load_config_prefix(config):
    return ManualStepper(config)
