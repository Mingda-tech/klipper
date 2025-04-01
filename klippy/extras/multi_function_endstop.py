import stepper, chelper, logging, configparser, statistics
from . import homing, probe

# class error(Exception):
#     pass

class MultiFunctionEndstop:
    error = configparser.Error
    def __init__(self, config):
        self.printer = config.get_printer()
        self.logger = logging.getLogger(name="multi_function_endstop")
        self.logger.setLevel(logging.DEBUG)

        # get gcode
        self.gcode = self.printer.lookup_object('gcode')
        gcode_macro = self.printer.load_object(config, 'gcode_macro')
        self.activate_gcode = gcode_macro.load_template(
            config, 'activate_gcode', '')
        self.deactivate_gcode = gcode_macro.load_template(
            config, 'deactivate_gcode', '')
        self.probe_activate_gcode = gcode_macro.load_template(
            config, 'probe_activate_gcode', '')
        self.probe_deactivate_gcode = gcode_macro.load_template(
            config, 'probe_deactivate_gcode', '')

        # get endstop_pin
        # Create an "endstop" object to handle the probe pin
        endstop_pin = config.get('endstop_pin')
        ppins = self.printer.lookup_object('pins')
        pin_params = ppins.parse_pin(endstop_pin, True, True)
        # Normalize pin name
        pin_name = "%s:%s" % (pin_params['chip_name'], pin_params['pin'])

        self.endstops = []
        # New endstop, register it
        self.mcu_endstop = ppins.setup_pin('endstop', endstop_pin)
        # name = stepper.get_name(short=True)
        name = "multi_function_endstop"
        self.endstops.append((self.mcu_endstop, name))
        query_endstops = self.printer.load_object(config, 'query_endstops')
        query_endstops.register_endstop(self.mcu_endstop, name)
        self.manual_probe = self.printer.load_object(config, 'manual_probe')

        # get samples
        self.samples = config.getint('samples', 1, minval=1)
        self.sample_retract_dist = config.getfloat(
            'sample_retract_dist', 0., above=0.)
        self.sample_extend_compensation = config.getfloat(
            'sample_extend_compensation', 1.)
        # logging.info("samples:%d" % (self.samples,))

        # get park_pos
        # self.park_pos = config.getlists('park_pos', seps=(',', '\n'),
        #                                 parser=float, count=3)
        # < 0:  Move in the negative direction, and the position information
        #       is reset after the end
        # > 0:  Move in the positive direction and record the position
        #       information after finishing
        
        self.axis = None
        self.stepper_map = {}
        self.stepper_objects = {}
        self.move_speed = config.getint('move_speed', 5)
        self.move_distance = config.getfloat('move_distance', 0.)
        self.retract_dir = -1 if self.move_distance>=0. else 1
        self.extend_dir = 1 if self.move_distance>=0. else -1

        # get mode
        self.mode = config.get('mode')
        logging.info("MultiFunctionEndstop mode:%s" % (self.mode,))
        # This mode is used to confirm the offset of
        # the manual stepper to endstop 
        # (if the manual stepper has already returned home).
        if self.mode == 'manual_stepper_offset':
            self.stepper_name = config.getlist('manual_stepper_name')
            self.stepper_len = len(self.stepper_name)
            logging.info("stepper number:%d" % (self.stepper_len,))
            for i in list(range(0, self.stepper_len, 1)):
                # self.stepper_pos[self.stepper_name[i]] = config.getfloatlist(
                #                             'park_pos_%d' % (i,), count=2)
                # self.stepper_offset[self.stepper_name[i]] = None
                self.stepper_objects[self.stepper_name[i]] = (
                    self.printer.lookup_object(
                        'manual_stepper '+self.stepper_name[i]))
                self.stepper_map[self.stepper_name[i]] = {
                    'park_pos': config.getfloatlist('park_pos_%d' % (i,),
                                                    count=2),
                    'move_distance': self.move_distance,
                    'offset': None,
                }
            logging.info("stepper_map: %s" % (self.stepper_map,))
            logging.info("stepper_objects: %s" % (self.stepper_objects,))
        elif self.mode == 'manual_axis_touch':
            self.axis = config.get('axis').lower()
            self.stepper_map[self.axis] = {
                'park_pos': config.getfloatlist('park_pos', count = 3),
                'move_distance': self.move_distance,
                'touch_pos': None,
            }
            self.gcode.register_command('MULTI_FUNCTION_ENDSTOP_AXIS_TOUCH',
                self.cmd_MULTI_FUNCTION_ENDSTOP_AXIS_TOUCH,
                desc=self.cmd_MULTI_FUNCTION_ENDSTOP_AXIS_TOUCH_help)
        elif ((self.mode == 'calibration_z_offset') or 
              (self.mode == 'calibration_zoffset')):
            self.axis = 'z'
            self.speed = config.getfloat('speed', 50, above=0.) # XY move speed
            self.detection_speed = config.getfloat(
                'detection_speed', self.move_speed, above=0.) # Z detection speed
            self.lift_speed = config.getfloat(
                'lift_speed', self.move_speed, above=0.) # Z lift speed
            # self.switch_offset = config.getfloat('switch_offset', 0., above=0.)
            self.switch_offsets = config.getfloatlist('switch_offsets', count = 3)
            self.stepper_map['toolhead_offsets'] = {
                # 'speed': self.speed,
                # 'detection_speed': self.detection_speed,
                # 'lift_speed': self.lift_speed,
                'x':None, 'y':None, 'z':None,
                'move_distance': self.move_distance,
                'touch_pos': None,
            }
            self.stepper_map['probe_offsets'] = {
                'probe_object': config.get('probe_object', "probe"),
                'x':None, 'y':None, 'z':None,
                'move_distance': self.move_distance,
                'touch_pos': None,
                'switch_offsets': self.switch_offsets,
                'homing_pos': config.getfloatlist('homing_pos', [None, None],
                                                  count=2),
            }

            self.gcode.register_command(
                'MULTI_FUNCTION_ENDSTOP_CALIBRATION_TOOLHEAD_Z_OFFSET',
                self.cmd_MFE_CALIBRATION_TOOLHEAD_Z_OFFSET,
                desc=self.cmd_MFE_CALIBRATION_TOOLHEAD_Z_OFFSET_help)
            self.gcode.register_command(
                'MULTI_FUNCTION_ENDSTOP_CALIBRATION_PROBE_OFFSET',
                self.cmd_MFE_CALIBRATION_PROBE_OFFSET,
                desc=self.cmd_MFE_CALIBRATION_PROBE_OFFSET_help)

        logging.info("MultiFunctionEndstop:%s" % (self.mode,))

        # self.gcode.register_command('MULTI_FUNCTION_ENDSTOP_SET_POS',
        #     self.cmd_MULTI_FUNCTION_ENDSTOP_SET_POS,
        #     desc=self.cmd_MULTI_FUNCTION_ENDSTOP_SET_POS_help)

        # self.gcode.register_command('MULTI_FUNCTION_ENDSTOP_START',
        #     self.cmd_START_CALIBRATION,
        #     desc=self.cmd_START_CALIBRATION_help)
        
        # Register event handlers
        # self.printer.register_event_handler("klippy:connect",
        #                                     self._handle_connect)
        self.printer.register_event_handler('klippy:mcu_identify',
                                            self._handle_mcu_identify)
    
    # Register event handlers
    # def _handle_connect(self):
        # flag = 0
        # kin = self.toolhead.get_kinematics()
        # for stepper in kin.get_steppers():
        #     if stepper.is_active_axis(self.axis):
        #         self.stepper_objects[self.axis] = stepper
        #         self.mcu_endstop.add_stepper(stepper)
        #         flag = 1
        #         break
        # if not flag:
        #     # raise error("Can't find the %s axis!" % (self.axis,))
        #     self.stepper_objects[self.axis] = None
        #     raise self.printer.command_error(
        #             "Can't find the %s axis!" % (self.axis,))
    def _handle_mcu_identify(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        if ((self.mode == 'calibration_z_offset') or 
            (self.mode == 'calibration_zoffset')):
            probe_info = self.printer.lookup_object(
                self.stepper_map['probe_offsets']['probe_object'], None)
            if probe_info is None:
                probe_info = self.printer.lookup_object(
                    'probe', None)
            if probe_info is not None:
                x_offset, y_offset, z_offset = probe_info.get_offsets()
                self.stepper_map['probe_offsets'].update(
                    {'probe_object': "probe",
                    'x': x_offset, 'y': y_offset, 'z': z_offset,}
                )
                self.logger.info(
                    ("update probe offsets:%s" % 
                        (self.stepper_map['probe_offsets'],)))
        if self.axis is None:
            return
        flag = 0
        kin = self.printer.lookup_object('toolhead').get_kinematics()
        for stepper in kin.get_steppers():
            if stepper.is_active_axis(self.axis):
                self.stepper_objects[self.axis] = stepper
                self.mcu_endstop.add_stepper(stepper)
                flag = 1
        if not flag:
            self.stepper_objects[self.axis] = None
            raise self.printer.command_error(
                    "Can't find the %s axis!" % (self.axis,))

    # get status
    def get_status(self, eventtime):
        # if ((self.mode == 'manual_stepper_offset') or 
        #     (self.mode == 'manual_axis_touch') or
        #     (self.mode == 'calibration_z_offset') or 
        #     (self.mode == 'calibration_zoffset')):
        #     # return {'mode': self.mode,
        #     #         'position': self.stepper_pos,
        #     #         'offset': self.stepper_offset,}
        #     return self.stepper_map
        return self.stepper_map

    # cmd_MULTI_FUNCTION_ENDSTOP_SET_POS_help = ""
    # def cmd_MULTI_FUNCTION_ENDSTOP_SET_POS(self, gcmd):
    #     if self.mode == 'manual_stepper_offset':
            
    #     elif self.mode == 'manual_axis_touch':
            

    def _move(self, coord, speed):
        self.toolhead.move(coord, speed)

    cmd_START_CALIBRATION_help = "Perform calibration"
    def cmd_START_CALIBRATION(self, gcmd):
        self.activate_gcode.run_gcode_from_command()
        self.multi_function_endstop_begin(gcmd)
        self.deactivate_gcode.run_gcode_from_command()

    def manual_home_to_endstop(self, endstops, pos, speed, toolhead=None,
                               triggered=True, check_triggered=True):
        hmove = homing.HomingMove(self.printer, endstops, toolhead)
        # epos = None
        try:
            epos = hmove.homing_move(pos, speed, probe_pos=True)
        except self.printer.command_error:
            if self.printer.is_shutdown():
                raise self.printer.command_error(
                    "Multi function endstop failed due to printer shutdown")
            raise
        if hmove.check_no_movement() is not None:
            raise self.printer.command_error(
                "Multi function endstop triggered prior to movement")
        return epos

    def multi_function_endstop_begin(self, gcmd):
        if self.mode == 'manual_stepper_offset':
            gcmd.respond_info("multi_function_endstop_begin")
            for i in list(range(0, self.stepper_len, 1)):
                stepper_object = self.stepper_objects[self.stepper_name[i]]
                self.mcu_endstop.add_stepper(stepper_object.get_steppers()[0])
                samples_sum = 0
                offset = 0.0
                retract_pos = 0.0
                pos = [self.move_distance, 0., 0., 0.]
                retract_len = self.retract_dir * self.sample_retract_dist
                extend_len = self.extend_dir * self.sample_extend_compensation
                for num in list(range(0, self.samples, 1)):
                    epos = self.manual_home_to_endstop(list(self.endstops),
                            pos, self.move_speed, stepper_object, True, True)
                    samples_sum += epos[0]
                    logging.info("%s samples:%d, pos:%s" % 
                                 (self.stepper_name[i], num, epos[0],))
                    if self.sample_retract_dist > 0.000001:
                        retract_pos = epos[0] + retract_len
                        stepper_object.do_move(retract_pos, self.move_speed,
                                               stepper_object.accel)
                    # In multi-sample sampling,
                    # for a predetermined displacement,
                    # 'sample_extend_compensation' is employed to
                    # enhance movement efficiency.
                    pos[0] = epos[0] + extend_len
                offset = samples_sum/self.samples
                self.stepper_map[self.stepper_name[i]]['offset'] = offset
                logging.info("%s: %s pos: %s" % (self.stepper_name[i], offset,
                                stepper_object.rail.get_commanded_position()))

    # def touch_move(self, pos, speed):
    #     phoming = self.printer.lookup_object('homing')
    #     return phoming.probing_move(self, pos, speed)
    # def probing_move(self, mcu_probe, pos, speed):
    #     hmove = HomingMove(self.printer, endstops)
    #     try:
    #         epos = hmove.homing_move(pos, speed, probe_pos=True)
    #     except self.printer.command_error:
    #         if self.printer.is_shutdown():
    #             raise self.printer.command_error(
    #                 "Probing failed due to printer shutdown")
    #         raise
    #     if hmove.check_no_movement() is not None:
    #         raise self.printer.command_error(
    #             "Probe triggered prior to movement")
    #     return epos

    cmd_MULTI_FUNCTION_ENDSTOP_AXIS_TOUCH_help = "Specify the axis to "\
                                                 "touch the endstop"
    def cmd_MULTI_FUNCTION_ENDSTOP_AXIS_TOUCH(self, gcmd):
        curtime = self.printer.get_reactor().monotonic()
        # stepper_object = self.stepper_objects[self.axis]
        stepper_object = self.toolhead
        park_pos_x = gcmd.get_float('X', self.stepper_map[self.axis]['park_pos'][0])
        park_pos_y = gcmd.get_float('Y', self.stepper_map[self.axis]['park_pos'][1])
        park_pos_z = gcmd.get_float('Z', self.stepper_map[self.axis]['park_pos'][2])
        park_pos_f = gcmd.get_int('F', self.move_speed * 60)
        axis_minimum = self.toolhead.get_status(curtime)["axis_minimum"]
        axis_maximum = self.toolhead.get_status(curtime)["axis_maximum"]
        if self.stepper_objects[self.axis] is not None:
            # 是否全归位了
            if 'xyz' not in self.toolhead.get_status(curtime)["homed_axes"]:
                raise self.printer.command_error("Must home before probe")
            # 通过G0指令将打印头移动到指定位置
            self.gcode.run_script_from_command("G0 X%.2f Y%.2f Z%.2f F%d" % (
                park_pos_x,
                park_pos_y,
                park_pos_z,
                park_pos_f,
            ))
            self.gcode.run_script_from_command("M400")
            # 确定数据
            axis_to_num = ord(self.axis) - ord('x')
            pos = self.toolhead.get_position()
            logging.info("multi_function: %s" % (pos,))
            retract_pos = list(pos)
            touch_pos = list(pos)
            
            if (self.move_distance >= 0):
                touch_pos[axis_to_num] = min(
                    (touch_pos[axis_to_num] + self.move_distance),
                    axis_maximum[axis_to_num])
            else:
                touch_pos[axis_to_num] = max(
                    (touch_pos[axis_to_num] + self.move_distance),
                    axis_minimum[axis_to_num])
                
            pos_info = 0
            samples_sum = 0
            retract_len = self.retract_dir * self.sample_retract_dist
            extend_len = self.extend_dir * self.sample_extend_compensation
            # 重复获取样本
            for num in list(range(0, self.samples, 1)):
                # 开始去触碰endstop
                try:
                    # epos = self.mcu_endstop.touch_move(
                    #     touch_pos, self.move_speed)
                    epos = self.manual_home_to_endstop(list(self.endstops),
                            touch_pos, self.move_speed)
                except self.printer.command_error as e:
                    reason = str(e)
                    if "Timeout during endstop homing" in reason:
                        reason += HINT_TIMEOUT
                    raise self.printer.command_error(reason)
                # Allow axis_twist_compensation to update results
                self.printer.send_event("probe:update_results", epos)
                # Report results
                self.gcode.respond_info("MFE trigger : %s=%.3f"
                                % (self.axis, epos[axis_to_num],))
                samples_sum += epos[axis_to_num]
                # 回缩
                if (self.sample_retract_dist > 0.000001):
                    retract_pos[axis_to_num] = (
                        epos[axis_to_num] + self.retract_len)
                    self._move(retract_pos, self.move_speed)
                # 计算下一次触摸终结位置
                touch_pos[axis_to_num] = (epos[axis_to_num] + extend_len)
            
            # 记录位置信息, 打印信息?
            touch_pos[axis_to_num] = samples_sum/self.samples
            self.stepper_map[self.axis]['touch_pos'] = touch_pos[:]
            logging.info("%s axis touch pos %s" % (self.axis, self.stepper_map))
        else:
            self.printer.command_error(
                "Can't find the %s axis!" % (self.axis,))


    # get toolheads z offset
    def _move_to_safe_z(self, gcmd):
        self.logger.info(f"Moving to safe Z height: {self.safe_z_height}")
        toolhead = self.printer.lookup_object('toolhead')
        curpos = toolhead.get_position()
        curpos[2] = self.safe_z_height
        try:
            toolhead.move(curpos, self.lift_speed) 
            toolhead.wait_moves()
        except self.printer.command_error as e:
            self.logger.error(f"Error moving to safe Z height: {str(e)}")
            raise gcmd.error(str(e))

    def _measure_nozzle_height(self, gcmd, is_right_nozzle=False):
        self.logger.info(f"Starting nozzle height measurement for {'right' if is_right_nozzle else 'left'} nozzle")
        toolhead = self.printer.lookup_object('toolhead')
        curtime = self.printer.get_reactor().monotonic()
        self.logger.info(f"Current toolhead position: {toolhead.get_position()}")
        self.logger.info(f"Homed axes: {toolhead.get_status(curtime)['homed_axes']}")
        toolhead.wait_moves()
        
        # Move to safe Z height
        self._move_to_safe_z(gcmd)
        
        # Move to probing position XY
        probe_x = self.switch_position[0]
        probe_y = self.switch_position[1]
        if is_right_nozzle:
            probe_x += self.right_nozzle_x_offset
            probe_y += self.right_nozzle_y_offset
        
        self.logger.info(f"Moving to probing position XY: {probe_x}, {probe_y}")
        pos = toolhead.get_position()
        pos[0] = probe_x
        pos[1] = probe_y
        try:
            toolhead.move(pos, self.speed)
            toolhead.wait_moves()
        except self.printer.command_error as e:
            self.logger.error(f"Error moving to probing position XY: {str(e)}")
            raise gcmd.error(str(e))
        
        # Probe
        self.logger.info(f"Starting probe with {self.samples} samples")
        measured_heights = []
        retract_pos = list(pos)
        touch_pos = list(pos)
        touch_pos[2] = self.switch_position[2]
        retract_len = self.retract_dir * self.sample_retract_dist
        extend_len = self.extend_dir * self.sample_extend_compensation
        for i in range(self.samples):
            try:
                # pos = self._probe(self.probing_speed)
                
                epos = self.manual_home_to_endstop(list(self.endstops),
                        touch_pos, self.detection_speed)
                measured_height = epos[2]
                measured_heights.append(measured_height)
                self.logger.info(f"Sample {i+1}: Measured height: {measured_height}")
                
                # Retract between samples if not the last sample
                # if i < self.samples - 1:
                #     self._retract(pos)
            except self.printer.command_error as e:
                self.logger.error(f"Error during probing: {str(e)}")
                raise gcmd.error(str(e))
            # 回缩
            retract_pos[2] = (epos[2] + retract_len)
            toolhead.move(retract_pos, self.lift_speed)
            toolhead.wait_moves()
            # 计算下一次触摸终结位置
            touch_pos[2] = (epos[2] + extend_len)
        
        # Calculate average height
        average_height = statistics.mean(measured_heights)
        self.logger.info(f"Average measured height: {average_height}")
        return average_height

    cmd_MFE_CALIBRATION_TOOLHEAD_Z_OFFSET_help = "Calibrate the height "\
        "difference between two nozzles"
    def cmd_MFE_CALIBRATION_TOOLHEAD_Z_OFFSET(self, gcmd):
        try:
            self.logger.info("Starting dual nozzle calibration")
            # Ensure the printer is homed
            toolhead = self.printer.lookup_object('toolhead')
            if 'xyz' not in toolhead.get_status(self.printer.get_reactor().monotonic())['homed_axes']:
                raise gcmd.error("Must home printer first")

            # Get values from Variables
            save_variables = self.printer.lookup_object('save_variables')
            status = save_variables.get_status(self.printer.get_reactor().monotonic())
            variables = status.get('variables', {})
            if not variables:
                self.logger.warning("No variables found in save_variables status")
            
            self.switch_position = [
                variables.get('switch_xpos', 0.),
                variables.get('switch_ypos', 0.),
                variables.get('zendstop', 0.)
            ]
            self.safe_z_height = variables.get('switch_zpos', 10.)
            self.right_nozzle_x_offset = variables.get('e1_xoffset', 0.)
            self.right_nozzle_y_offset = variables.get('e1_yoffset', 0.)
            
            self.logger.info(f"Using switch_position: {self.switch_position}")
            self.logger.info(f"Using safe_z_height: {self.safe_z_height}")
            self.logger.info(f"Using right_nozzle_x_offset: {self.right_nozzle_x_offset}")
            self.logger.info(f"Using right_nozzle_y_offset: {self.right_nozzle_y_offset}")

            # Measure height of first nozzle
            self.logger.info("Measuring height of first nozzle")
            self.gcode.run_script_from_command("T0")
            height1 = self._measure_nozzle_height(gcmd)
            self.logger.info(f"Height of first nozzle: {height1}")
            
            # Move to safe Z height before switching nozzles
            self._move_to_safe_z(gcmd)
            
            # Measure height of second nozzle
            self.logger.info("Measuring height of second nozzle")
            self.gcode.run_script_from_command("T1")
            height2 = self._measure_nozzle_height(gcmd, is_right_nozzle=True)
            self.logger.info(f"Height of second nozzle: {height2}")
            
            # Calculate height difference
            height_diff = round(height2 - height1, 3)  # 保留三位小数
            self.logger.info(f"Calculated height difference: {height_diff:.3f}")
            
            # Move to safe Z height
            self._move_to_safe_z(gcmd)
            
            # Save results to Variables
            save_gcmd = self.gcode.create_gcode_command('SAVE_VARIABLE', 'SAVE_VARIABLE', {'VARIABLE': 'e1_zoffset', 'VALUE': f"{height_diff:.3f}"})
            save_variables.cmd_SAVE_VARIABLE(save_gcmd)
            
            gcmd.respond_info(
                f"Nozzle height difference: {height_diff:.3f}\n"
                "The height difference has been saved to 'e1_zoffset' in Variables.\n"
                f"Dual nozzle height calibration completed. reboot to use the height difference: {height_diff:.3f}")
            self.logger.info(f"Dual nozzle calibration completed successfully. Height difference: {height_diff:.3f}")

            self.gcode.run_script_from_command("T0")  # 切换回左侧喷嘴

        except Exception as e:
            self.logger.error(f"Error during calibration: {str(e)}",
                              exc_info=True)
            raise


    def _detection_toolhead(self, gcmd, samples):
        curtime = self.printer.get_reactor().monotonic()
        axis_minimum = self.toolhead.get_status(curtime)["axis_minimum"]
        axis_maximum = self.toolhead.get_status(curtime)["axis_maximum"]
        axis_to_num = ord(self.axis) - ord('x')
        pos = self.toolhead.get_position()
        retract_pos = list(pos)
        touch_pos = list(pos)
        # 防越界
        if (self.move_distance >= 0):
            touch_pos[axis_to_num] = min(
                (touch_pos[axis_to_num] + self.move_distance),
                axis_maximum[axis_to_num])
        else:
            touch_pos[axis_to_num] = max(
                (touch_pos[axis_to_num] + self.move_distance),
                axis_minimum[axis_to_num])
            
        logging.info(("detection toolhead position:%s-->%s" %
                     (pos, touch_pos,)))
        samples_sum = 0
        retract_len = self.retract_dir * self.sample_retract_dist
        extend_len = self.extend_dir * self.sample_extend_compensation
        # 重复获取样本
        for num in list(range(0, samples, 1)):
            # 开始去触碰endstop
            try:
                epos = self.manual_home_to_endstop(list(self.endstops),
                        touch_pos, self.detection_speed)
            except self.printer.command_error as e:
                reason = str(e)
                if "Timeout during endstop homing" in reason:
                    reason += HINT_TIMEOUT
                raise self.printer.command_error(reason)
            # Allow axis_twist_compensation to update results
            # self.printer.send_event("probe:update_results", epos)
            # Report results
            gcmd.respond_info("MFE nozzle trigger : %s=%.3f" %
                               (self.axis, epos[axis_to_num],))
            samples_sum += epos[axis_to_num]
            # 回缩
            if (self.sample_retract_dist > 0.000001):
                retract_pos[axis_to_num] = (
                    epos[axis_to_num] + retract_len)
                self._move(retract_pos, self.lift_speed)
            # 计算下一次触摸终结位置
            touch_pos[axis_to_num] = (epos[axis_to_num] + extend_len)

        # 记录位置信息, 打印信息?
        touch_pos[axis_to_num] = samples_sum/samples
        self.stepper_map['toolhead_offsets']['touch_pos'] = touch_pos[:]
        # logging.info("%s axis touch pos %s" % (self.axis, self.stepper_map))
        return touch_pos[axis_to_num]

    def _detection_probe(self, gcmd, samples):
        curtime = self.printer.get_reactor().monotonic()
        axis_minimum = self.toolhead.get_status(curtime)["axis_minimum"]
        axis_maximum = self.toolhead.get_status(curtime)["axis_maximum"]
        axis_to_num = ord(self.axis) - ord('x')
        pos = self.toolhead.get_position()
        retract_pos = list(pos)
        touch_pos = list(pos)
        # 防越界
        if (self.move_distance >= 0):
            touch_pos[axis_to_num] = min(
                (touch_pos[axis_to_num] + self.move_distance),
                axis_maximum[axis_to_num])
        else:
            touch_pos[axis_to_num] = max(
                (touch_pos[axis_to_num] + self.move_distance),
                axis_minimum[axis_to_num])

        logging.info(("detection probe position:%s --> %s" %
                     (pos, touch_pos,)))
        samples_sum = 0
        retract_len = self.retract_dir * self.sample_retract_dist
        extend_len = self.extend_dir * self.sample_extend_compensation
        # 重复获取样本
        for num in list(range(0, samples, 1)):
            # 开始去触碰endstop
            try:
                epos = self.manual_home_to_endstop(list(self.endstops),
                        touch_pos, self.detection_speed)
            except self.printer.command_error as e:
                reason = str(e)
                if "Timeout during endstop homing" in reason:
                    reason += HINT_TIMEOUT
                raise self.printer.command_error(reason)
            # Allow axis_twist_compensation to update results
            # self.printer.send_event("probe:update_results", epos)
            # Report results
            gcmd.respond_info("MFE probe trigger : %s=%.3f" %
                               (self.axis, epos[axis_to_num],))
            samples_sum += epos[axis_to_num]
            # 回缩
            if (self.sample_retract_dist > 0.000001):
                retract_pos[axis_to_num] = (
                    epos[axis_to_num] + retract_len)
                self._move(retract_pos, self.lift_speed)
            # 计算下一次触摸终结位置
            touch_pos[axis_to_num] = (epos[axis_to_num] + extend_len)

        # 记录位置信息, 打印信息?
        touch_pos[axis_to_num] = samples_sum/samples
        self.stepper_map['probe_offsets']['touch_pos'] = touch_pos[:]
        # logging.info("%s axis touch pos %s" % (self.axis, self.stepper_map))
        return touch_pos[axis_to_num]

    def _save_probe_offset(self, gcmd, name, offset,
                           endstop_offset=None):
        z_offset = offset
        configfile = self.printer.lookup_object('configfile')
        configfile.set(name, 'z_offset', "%.3f" % (z_offset,))
        msg = ""
        if endstop_offset is not None:
            configfile.set('stepper_z', 'position_endstop',
                           "%.3f" % (endstop_offset,))
            msg = "stepper_z: position_endstop=%.3f\n" % (endstop_offset,)
        gcmd.respond_info(msg+
            "%s: z_offset=%.3f\n"
            "The SAVE_CONFIG command will update the printer config file\n"
            "with the above and restart the printer." % (name, z_offset,))

    cmd_MFE_CALIBRATION_PROBE_OFFSET_help = "Automatic calibration of z offset"
    def cmd_MFE_CALIBRATION_PROBE_OFFSET(self, gcmd):
        try:
            # 开始前的准备
            self.activate_gcode.run_gcode_from_command()

            self.logger.info("Starting probe offset calibration")
            probe_info = self.printer.lookup_object(
                self.stepper_map['probe_offsets']['probe_object'], None)
            if probe_info is None:
                raise gcmd.error(("%s is None!" %
                    (self.stepper_map['probe_offsets']['probe_object'],)))
            name = probe_info.cmd_helper.name
            # Ensure the printer is homed
            curtime = self.printer.get_reactor().monotonic()
            if ('xyz' not in self.toolhead.get_status(curtime)['homed_axes']):
                raise gcmd.error("Must home printer first")
            axis_minimum = self.toolhead.get_status(curtime)["axis_minimum"]
            axis_maximum = self.toolhead.get_status(curtime)["axis_maximum"]

            # Get values from Variables
            save_variables = self.printer.lookup_object('save_variables')
            status = save_variables.get_status(curtime)
            variables = status.get('variables', {})
            if not variables:
                self.logger.warning("No variables found in save_variables status")
            # get switch position
            switch_position = [
                variables.get('switch_xpos'),
                variables.get('switch_ypos'),
                variables.get('switch_zpos')
            ]
            park_pos_x = gcmd.get_float('X', switch_position[0])
            park_pos_y = gcmd.get_float('Y', switch_position[1])
            park_pos_z = gcmd.get_float('Z', switch_position[2])
            park_pos_f = gcmd.get_int('F', (self.speed * 60))
            the_samples = gcmd.get_int('SAMPLES', self.samples, minval=1)
            if ((park_pos_x is None) or
                (park_pos_y is None) or
                (park_pos_z is None)):
                raise gcmd.error(
                    ("Unknown position: %s" %
                    ([str(park_pos_x), str(park_pos_y), str(park_pos_z)],)))

            bed_mesh = self.printer.lookup_object('bed_mesh', default=None)
            bed_mesh_name = None
            if bed_mesh is not None:
                bed_mesh_name = bed_mesh.get_mesh()
                bed_mesh.set_mesh(None)

            # 通过G0指令将打印头移动到指定位置
            self.gcode.run_script_from_command("G0 Z%.2f F%d" %
                (park_pos_z, park_pos_f,))
            self.gcode.run_script_from_command("M400")
            self.gcode.run_script_from_command("G0 X%.2f Y%.2f F%d" %
                (park_pos_x, park_pos_y, park_pos_f,))
            self.gcode.run_script_from_command("M400")

            # 开始测试喷嘴位置
            nozzle_zero = self._detection_toolhead(gcmd, the_samples)

            # 探针检测前的准备
            self.probe_activate_gcode.run_gcode_from_command()
            x_offset, y_offset, z_offset = probe_info.get_offsets()
            # 移动探针到指定位置
            park_pos_x -= (x_offset + self.switch_offsets[0])
            park_pos_y -= (y_offset + self.switch_offsets[1])
            self.logger.info("probe_offsets:[%.3f, %.3f, %.3f].\n"
                             "switch_offsets:%s.\n"
                             "park_pos:[%.3f, %.3f, %.3f]" %
                             (x_offset, y_offset, z_offset,
                              self.switch_offsets,
                              park_pos_x, park_pos_y, park_pos_z,))
            # park_pos_z -= (z_offset + self.switch_offsets[2])
            self.gcode.run_script_from_command("G0 Z%.2f F%d" %
                (park_pos_z, park_pos_f,))
            self.gcode.run_script_from_command("M400")
            self.gcode.run_script_from_command("G0 X%.2f Y%.2f F%d" %
                (park_pos_x, park_pos_y, park_pos_f,))
            self.gcode.run_script_from_command("M400")
            # 开始测试探针位置
            probe_zero = self._detection_probe(gcmd, the_samples)
            # 是否需要复位探针?
            endstop_offset = None
            home_zero = []
            homing_pos = self.stepper_map['probe_offsets']['homing_pos']
            if ((homing_pos[0] is not None) and (homing_pos[1] is not None)):
                homing_pos_x = homing_pos[0] - x_offset
                homing_pos_y = homing_pos[1] - y_offset
                self.gcode.run_script_from_command("G0 Z%.2f F%d" %
                    (park_pos_z, park_pos_f,))
                self.gcode.run_script_from_command("M400")
                self.gcode.run_script_from_command("G0 X%.2f Y%.2f F%d" %
                    (homing_pos_x, homing_pos_y, park_pos_f,))
                self.gcode.run_script_from_command("M400")
                home_zero = probe.run_single_probe(probe_info, gcmd)[:]
                endstop_offset = home_zero[2]
                self.logger.info("probe_home_pos:[%.3f, %.3f, %.3f]" %
                    (home_zero[0], home_zero[1], home_zero[2],))
            # 探针检测后的收尾
            self.probe_deactivate_gcode.run_gcode_from_command()

            # 结束后的收尾
            self.deactivate_gcode.run_gcode_from_command()
            # 保存
            probe_zoffset = probe_zero + self.switch_offsets[2] - nozzle_zero
            self.logger.info(
                "probe(%.3f) + switch_offset(%.3f) - nozzle(%.3f) "
                "= probe_zoffset:(%.3f)" %
                (probe_zero, self.switch_offsets[2], nozzle_zero,
                 probe_zoffset,))
            if endstop_offset is not None:
                old_z_endstop_offset = self.manual_probe.z_position_endstop
                if old_z_endstop_offset is not None:
                    endstop_offset = (old_z_endstop_offset +
                            probe_zoffset - home_zero[2])
                    self.logger.info(
                        "old_z_endstop_offset(%.3f) + "
                        "probe_zoffset(%.3f) - probe_home_z(%.3f) "
                        "= z_endstop_offset(%.3f)" %
                        (old_z_endstop_offset,
                        probe_zoffset, home_zero[2],
                        endstop_offset,))
                else:
                    endstop_offset = None

            self._save_probe_offset(gcmd, name, probe_zoffset, endstop_offset)
            if bed_mesh_name is not None:
                bed_mesh.set_mesh(bed_mesh_name)

        except Exception as e:
            self.logger.error(f"Error during calibration: {str(e)}",
                              exc_info=True)
            raise

def load_config(config):
    return MultiFunctionEndstop(config)
