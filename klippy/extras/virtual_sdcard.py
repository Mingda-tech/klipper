# Virtual sdcard support (print files directly from a host g-code file)
#
# Copyright (C) 2018-2024  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import os, sys, logging, io, configparser

VALID_GCODE_EXTS = ['gcode', 'g', 'gco']

DEFAULT_ERROR_GCODE = """
{% if 'heaters' in printer %}
   TURN_OFF_HEATERS
{% endif %}
"""

class VirtualSD:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.printer.register_event_handler("klippy:shutdown",
                                            self.handle_shutdown)
        # sdcard state
        sd = config.get('path')
        self.sdcard_dirname = os.path.normpath(os.path.expanduser(sd))
        self.current_file = None
        self.file_position = self.file_size = 0
        # Print Stat Tracking
        self.print_stats = self.printer.load_object(config, 'print_stats')
        # Work timer
        self.reactor = self.printer.get_reactor()
        self.must_pause_work = self.cmd_from_sd = False
        self.next_file_position = 0
        self.work_timer = None
        # Error handling
        gcode_macro = self.printer.load_object(config, 'gcode_macro')
        self.on_error_gcode = gcode_macro.load_template(
            config, 'on_error_gcode', DEFAULT_ERROR_GCODE)
        # Register commands
        self.gcode = self.printer.lookup_object('gcode')
        for cmd in ['M20', 'M21', 'M23', 'M24', 'M25', 'M26', 'M27']:
            self.gcode.register_command(cmd, getattr(self, 'cmd_' + cmd))
        for cmd in ['M28', 'M29', 'M30']:
            self.gcode.register_command(cmd, self.cmd_error)
        self.gcode.register_command(
            "SDCARD_RESET_FILE", self.cmd_SDCARD_RESET_FILE,
            desc=self.cmd_SDCARD_RESET_FILE_help)
        self.gcode.register_command(
            "SDCARD_PRINT_FILE", self.cmd_SDCARD_PRINT_FILE,
            desc=self.cmd_SDCARD_PRINT_FILE_help)
        # 添加打印状态保存相关的变量
        self.last_save_time = 0
        self.save_interval = 5.0  # 保存间隔为5秒
        config_path = os.path.expanduser('~/printer_data/config')
        self.state_file = os.path.join(config_path, 'print_state.cfg')
        # 状态保存相关
        self.save_state_pending = False
        self.save_state_timer = None
        self.saved_state = None
        # 添加恢复打印命令
        self.gcode.register_command(
            "RESTORE_PRINT", self.cmd_RESTORE_PRINT,
            desc=self.cmd_RESTORE_PRINT_help)
    def handle_shutdown(self):
        if self.work_timer is not None:
            self.must_pause_work = True
            try:
                readpos = max(self.file_position - 1024, 0)
                readcount = self.file_position - readpos
                self.current_file.seek(readpos)
                data = self.current_file.read(readcount + 128)
            except:
                logging.exception("virtual_sdcard shutdown read")
                return
            logging.info("Virtual sdcard (%d): %s\nUpcoming (%d): %s",
                         readpos, repr(data[:readcount]),
                         self.file_position, repr(data[readcount:]))
    def stats(self, eventtime):
        if self.work_timer is None:
            return False, ""
        return True, "sd_pos=%d" % (self.file_position,)
    def get_file_list(self, check_subdirs=False):
        if check_subdirs:
            flist = []
            for root, dirs, files in os.walk(
                    self.sdcard_dirname, followlinks=True):
                for name in files:
                    ext = name[name.rfind('.')+1:]
                    if ext not in VALID_GCODE_EXTS:
                        continue
                    full_path = os.path.join(root, name)
                    r_path = full_path[len(self.sdcard_dirname) + 1:]
                    size = os.path.getsize(full_path)
                    flist.append((r_path, size))
            return sorted(flist, key=lambda f: f[0].lower())
        else:
            dname = self.sdcard_dirname
            try:
                filenames = os.listdir(self.sdcard_dirname)
                return [(fname, os.path.getsize(os.path.join(dname, fname)))
                        for fname in sorted(filenames, key=str.lower)
                        if not fname.startswith('.')
                        and os.path.isfile((os.path.join(dname, fname)))]
            except:
                logging.exception("virtual_sdcard get_file_list")
                raise self.gcode.error("Unable to get file list")
    def get_status(self, eventtime):
        return {
            'file_path': self.file_path(),
            'progress': self.progress(),
            'is_active': self.is_active(),
            'file_position': self.file_position,
            'file_size': self.file_size,
        }
    def file_path(self):
        if self.current_file:
            return self.current_file.name
        return None
    def progress(self):
        if self.file_size:
            return float(self.file_position) / self.file_size
        else:
            return 0.
    def is_active(self):
        return self.work_timer is not None
    def do_pause(self):
        if self.work_timer is not None:
            self.must_pause_work = True
            while self.work_timer is not None and not self.cmd_from_sd:
                self.reactor.pause(self.reactor.monotonic() + .001)
    def do_resume(self):
        if self.work_timer is not None:
            raise self.gcode.error("SD busy")
        self.must_pause_work = False
        self.work_timer = self.reactor.register_timer(
            self.work_handler, self.reactor.NOW)
    def do_cancel(self):
        if self.current_file is not None:
            self.do_pause()
            self.current_file.close()
            self.current_file = None
            self.print_stats.note_cancel()
        self.file_position = self.file_size = 0
    # G-Code commands
    def cmd_error(self, gcmd):
        raise gcmd.error("SD write not supported")
    def _reset_file(self):
        if self.current_file is not None:
            self.do_pause()
            self.current_file.close()
            self.current_file = None
        self.file_position = self.file_size = 0
        self.print_stats.reset()
        self.printer.send_event("virtual_sdcard:reset_file")
    cmd_SDCARD_RESET_FILE_help = "Clears a loaded SD File. Stops the print "\
        "if necessary"
    def cmd_SDCARD_RESET_FILE(self, gcmd):
        if self.cmd_from_sd:
            raise gcmd.error(
                "SDCARD_RESET_FILE cannot be run from the sdcard")
        self._reset_file()
    cmd_SDCARD_PRINT_FILE_help = "Loads a SD file and starts the print.  May "\
        "include files in subdirectories."
    def cmd_SDCARD_PRINT_FILE(self, gcmd):
        if self.work_timer is not None:
            raise gcmd.error("SD busy")
        self._reset_file()
        filename = gcmd.get("FILENAME")
        if filename[0] == '/':
            filename = filename[1:]
        self._load_file(gcmd, filename, check_subdirs=True)
        self.do_resume()
    def cmd_M20(self, gcmd):
        # List SD card
        files = self.get_file_list()
        gcmd.respond_raw("Begin file list")
        for fname, fsize in files:
            gcmd.respond_raw("%s %d" % (fname, fsize))
        gcmd.respond_raw("End file list")
    def cmd_M21(self, gcmd):
        # Initialize SD card
        gcmd.respond_raw("SD card ok")
    def cmd_M23(self, gcmd):
        # Select SD file
        if self.work_timer is not None:
            raise gcmd.error("SD busy")
        self._reset_file()
        filename = gcmd.get_raw_command_parameters().strip()
        if filename.startswith('/'):
            filename = filename[1:]
        self._load_file(gcmd, filename)
    def _load_file(self, gcmd, filename, check_subdirs=False):
        files = self.get_file_list(check_subdirs)
        flist = [f[0] for f in files]
        files_by_lower = { fname.lower(): fname for fname, fsize in files }
        fname = filename
        try:
            if fname not in flist:
                fname = files_by_lower[fname.lower()]
            fname = os.path.join(self.sdcard_dirname, fname)
            f = io.open(fname, 'r', newline='')
            f.seek(0, os.SEEK_END)
            fsize = f.tell()
            f.seek(0)
        except:
            logging.exception("virtual_sdcard file open")
            raise gcmd.error("Unable to open file")
        gcmd.respond_raw("File opened:%s Size:%d" % (filename, fsize))
        gcmd.respond_raw("File selected")
        self.current_file = f
        self.file_position = 0
        self.file_size = fsize
        self.print_stats.set_current_file(filename)
    def cmd_M24(self, gcmd):
        # Start/resume SD print
        self.do_resume()
    def cmd_M25(self, gcmd):
        # Pause SD print
        self.do_pause()
    def cmd_M26(self, gcmd):
        # Set SD position
        if self.work_timer is not None:
            raise gcmd.error("SD busy")
        pos = gcmd.get_int('S', minval=0)
        self.file_position = pos
    def cmd_M27(self, gcmd):
        # Report SD print status
        if self.current_file is None:
            gcmd.respond_raw("Not SD printing.")
            return
        gcmd.respond_raw("SD printing byte %d/%d"
                         % (self.file_position, self.file_size))
    def get_file_position(self):
        return self.next_file_position
    def set_file_position(self, pos):
        self.next_file_position = pos
    def is_cmd_from_sd(self):
        return self.cmd_from_sd
    # Background work timer
    def work_handler(self, eventtime):
        logging.info("Starting SD card print (position %d)", self.file_position)
        self.reactor.unregister_timer(self.work_timer)
        try:
            self.current_file.seek(self.file_position)
        except:
            logging.exception("virtual_sdcard seek")
            self.work_timer = None
            return self.reactor.NEVER
        self.print_stats.note_start()
        gcode_mutex = self.gcode.get_mutex()
        partial_input = ""
        lines = []
        error_message = None
        while not self.must_pause_work:
            if not lines:
                # Read more data
                try:
                    data = self.current_file.read(8192)
                except:
                    logging.exception("virtual_sdcard read")
                    break
                if not data:
                    # End of file
                    self.current_file.close()
                    self.current_file = None
                    logging.info("Finished SD card print")
                    self.gcode.respond_raw("Done printing file")
                    break
                lines = data.split('\n')
                lines[0] = partial_input + lines[0]
                partial_input = lines.pop()
                lines.reverse()
                self.reactor.pause(self.reactor.NOW)
                continue
            # Pause if any other request is pending in the gcode class
            if gcode_mutex.test():
                self.reactor.pause(self.reactor.monotonic() + 0.100)
                continue
            # Dispatch command
            self.cmd_from_sd = True
            line = lines.pop()
            if sys.version_info.major >= 3:
                next_file_position = self.file_position + len(line.encode()) + 1
            else:
                next_file_position = self.file_position + len(line) + 1
            self.next_file_position = next_file_position
            try:
                self.gcode.run_script(line)
            except self.gcode.error as e:
                error_message = str(e)
                try:
                    self.gcode.run_script(self.on_error_gcode.render())
                except:
                    logging.exception("virtual_sdcard on_error")
                break
            except:
                logging.exception("virtual_sdcard dispatch")
                break
            self.cmd_from_sd = False
            self.file_position = self.next_file_position
            # Do we need to skip around?
            if self.next_file_position != next_file_position:
                try:
                    self.current_file.seek(self.file_position)
                except:
                    logging.exception("virtual_sdcard seek")
                    self.work_timer = None
                    return self.reactor.NEVER
                lines = []
                partial_input = ""
            # 检查是否需要基于时间保存状态
            if self.last_save_time + self.save_interval < eventtime:
                self.save_print_state()
                self.last_save_time = eventtime
            # 检查是否是换层命令 (M73)
            if line.strip().startswith('M73'):
                self.save_print_state()
            # 检查是否是换头命令 (T0/T1)
            stripped_line = line.strip()
            if stripped_line == 'T0' or stripped_line == 'T1':
                self.save_print_state()
        logging.info("Exiting SD card print (position %d)", self.file_position)
        self.work_timer = None
        self.cmd_from_sd = False
        if error_message is not None:
            self.print_stats.note_error(error_message)
        elif self.current_file is not None:
            self.print_stats.note_pause()
        else:
            self.print_stats.note_complete()
        return self.reactor.NEVER
    def _save_state_to_disk(self, eventtime):
        if self.saved_state is None:
            return self.reactor.NEVER
        
        # 使用临时文件进行原子写入操作
        temp_file = self.state_file + '.tmp'
        try:
            # 先写入临时文件
            with open(temp_file, 'w') as f:
                self.saved_state.write(f)
                f.flush()
                os.fsync(f.fileno())  # 确保数据写入磁盘
            
            # 然后进行原子重命名操作
            os.replace(temp_file, self.state_file)
            
            # 清理状态
            self.saved_state = None
            self.save_state_pending = False
            self.save_state_timer = None
        except:
            # 如果发生错误，尝试清理临时文件
            try:
                os.unlink(temp_file)
            except:
                pass
            logging.exception("Error saving print state")
        return self.reactor.NEVER
    def save_print_state(self):
        if self.current_file is None:
            return
        
        # 如果已经有一个保存操作在等待，就跳过这次保存
        if self.save_state_pending:
            return
            
        config = configparser.ConfigParser()
        
        # 基本打印状态
        config['print_state'] = {
            'file_path': str(self.file_path()),
            'file_position': str(self.file_position),
            'file_size': str(self.file_size),
            'progress': '{:.2f}'.format(self.progress())
        }
        
        # 获取打印机状态
        try:
            # 获取gcode_move对象
            gcode_move = self.printer.lookup_object('gcode_move')
            
            if gcode_move:
                status = gcode_move.get_status(self.reactor.monotonic())
                
                # 获取相对坐标
                gcode_position = status['gcode_position']
                config['position'] = {
                    'x': '{:.2f}'.format(gcode_position[0]),
                    'y': '{:.2f}'.format(gcode_position[1]),
                    'z': '{:.2f}'.format(gcode_position[2]),
                    'e': '{:.2f}'.format(gcode_position[3])
                }
                
                # 保存坐标模式
                config['motion_mode'] = {
                    'absolute_coordinates': str(status['absolute_coordinates']),
                    'absolute_extrude': str(status['absolute_extrude'])
                }
                
                # 保存速度和挤出相关设置
                config['speed'] = {
                    'speed': '{:.2f}'.format(status['speed']),
                    'speed_factor': '{:.2f}'.format(status['speed_factor']),
                    'extrude_factor': '{:.2f}'.format(status['extrude_factor'])
                }
            
            # 获取dual_carriage状态
            dual_carriage = self.printer.lookup_object('dual_carriage', None)
            if dual_carriage:
                dc_status = dual_carriage.get_status(self.reactor.monotonic())
                config['dual_carriage'] = {
                    'carriage_0': dc_status['carriage_0'],
                    'carriage_1': dc_status['carriage_1']
                }
                
                # 如果在复制或镜像模式下，保存两个挤出头的位置
                if dc_status['carriage_1'] in ['COPY', 'MIRROR']:
                    config['dual_carriage']['mode'] = dc_status['carriage_1']
            
            # 从toolhead获取当前活跃挤出头信息
            toolhead = self.printer.lookup_object('toolhead')
            if toolhead:
                config['extruder'] = {}
                active_extruder = toolhead.get_extruder().get_name()
                config['extruder']['active_extruder'] = str(active_extruder)
                
                # 获取打印头最大速度和加速度
                status = toolhead.get_status(self.reactor.monotonic())
                config['motion_limits'] = {
                    'max_velocity': '{:.2f}'.format(status['max_velocity']),
                    'max_accel': '{:.2f}'.format(status['max_accel']),
                    'square_corner_velocity': '{:.2f}'.format(status['square_corner_velocity'])
                }
                    
            # 获取所有挤出头温度
            config['temperatures'] = {}
            for i in range(2):  # 最多支持2个挤出头
                extruder_name = 'extruder' if i == 0 else f'extruder{i}'
                extruder = self.printer.lookup_object(extruder_name, None)
                if extruder:
                    status = extruder.get_status(self.reactor.monotonic())
                    # 只保存目标温度
                    if status['target'] > 0:  # 只在有目标温度时保存
                        config['temperatures'][extruder_name] = '{:.2f}'.format(status['target'])
                    
            # 获取热床温度
            heater_bed = self.printer.lookup_object('heater_bed', None)
            if heater_bed:
                status = heater_bed.get_status(self.reactor.monotonic())
                # 只保存目标温度
                if status['target'] > 0:  # 只在有目标温度时保存
                    config['temperatures']['bed'] = '{:.2f}'.format(status['target'])
            
            # 获取风扇速度
            config['fans'] = {}
            try:
                # 获取机箱风扇
                case_fan = self.printer.lookup_object('fan_generic Case_Fan', None)
                if case_fan:
                    fan_status = case_fan.get_status(self.reactor.monotonic())
                    config['fans']['case_fan'] = '{:.2f}'.format(fan_status['speed'])

                # 获取CPU风扇
                cpu_fan = self.printer.lookup_object('temperature_fan CPU_Temperature', None)
                if cpu_fan:
                    fan_status = cpu_fan.get_status(self.reactor.monotonic())
                    config['fans']['cpu_fan'] = '{:.2f}'.format(fan_status['speed'])

                # 获取辅助冷却风扇
                aux_fan = self.printer.lookup_object('fan_generic Auxiliary_Cooling_Fan', None)
                if aux_fan:
                    fan_status = aux_fan.get_status(self.reactor.monotonic())
                    config['fans']['auxiliary_fan'] = '{:.2f}'.format(fan_status['speed'])

                # 获取热端风扇
                hotend_fans = ['heater_fan Hotend_Fan0', 'heater_fan Hotend_Fan1']
                for fan_name in hotend_fans:
                    fan = self.printer.lookup_object(fan_name, None)
                    if fan:
                        fan_status = fan.get_status(self.reactor.monotonic())
                        config['fans'][fan_name.replace('heater_fan ', '').lower()] = '{:.2f}'.format(fan_status['speed'])

                # 获取喷嘴风扇
                nozzle_fans = ['fan_generic Nozzle_Fan0', 'fan_generic Nozzle_Fan1']
                for fan_name in nozzle_fans:
                    fan = self.printer.lookup_object(fan_name, None)
                    if fan:
                        fan_status = fan.get_status(self.reactor.monotonic())
                        config['fans'][fan_name.replace('fan_generic ', '').lower()] = '{:.2f}'.format(fan_status['speed'])

            except Exception as e:
                logging.exception("Error getting fan speeds: %s", str(e))
        
        except:
            logging.exception("Error getting printer state data")
            return
            
        # 设置保存状态
        self.saved_state = config
        self.save_state_pending = True
        
        # 注册延迟保存定时器
        if self.save_state_timer is None:
            self.save_state_timer = self.reactor.register_timer(
                self._save_state_to_disk, self.reactor.NOW + 0.1)  # 延迟100ms保存
    # 添加恢复打印的命令处理函数
    cmd_RESTORE_PRINT_help = "Restore the previous print after power loss"
    def cmd_RESTORE_PRINT(self, gcmd):
        if self.work_timer is not None:
            logging.info("RESTORE_PRINT: Already printing")
            raise gcmd.error("Already printing")
        
        logging.info("RESTORE_PRINT: Starting restore process")
        
        # 读取状态文件
        config = configparser.ConfigParser()
        state_file = None
        state_data = None
        
        try:
            # 读取状态文件
            logging.info("RESTORE_PRINT: Trying to read state file: %s", self.state_file)
            config.read(self.state_file)
            if 'print_state' in config:
                state_file = self.state_file
                state_data = config
                logging.info("RESTORE_PRINT: Successfully read state file")
        except:
            logging.exception("RESTORE_PRINT: Error reading state file")
            raise gcmd.error("No valid print state found")
        
        if state_data is None:
            logging.info("RESTORE_PRINT: No valid state file found")
            raise gcmd.error("No valid print state found")

        try:
            # 获取打印状态
            print_state = state_data['print_state']
            file_path = print_state['file_path']
            file_position = int(print_state['file_position'])
            logging.info("RESTORE_PRINT: Found state - file: %s, position: %d", file_path, file_position)

            # 重置打印状态
            self._reset_file()
            self.must_pause_work = False

            # 1. 先恢复温度
            if 'temperatures' in state_data:
                temps = state_data['temperatures']
                if 'bed' in temps:
                    self.gcode.run_script_from_command(f"M140 S{float(temps['bed'])}")
                if 'extruder' in temps:
                    extruder = self.printer.lookup_object('extruder', None)
                    if extruder:
                        status = extruder.get_status(self.reactor.monotonic())
                        if status['temperature'] < 80:
                            self.gcode.run_script_from_command("M109 S80")
                    self.gcode.run_script_from_command(f"M104 S{float(temps['extruder'])}")
                if 'extruder1' in temps:
                    extruder1 = self.printer.lookup_object('extruder1', None)
                    if extruder1:
                        status = extruder1.get_status(self.reactor.monotonic())
                        if status['temperature'] < 80:
                            self.gcode.run_script_from_command("M109 T1 S80")
                    self.gcode.run_script_from_command(f"M104 T1 S{float(temps['extruder1'])}")
                logging.info("RESTORE_PRINT: Temperature commands sent")

            # 2. 设置绝对坐标模式
            self.gcode.run_script_from_command("G90")  # 设置绝对坐标模式
            self.gcode.run_script_from_command("M83")  # 设置相对挤出模式

            
            # 3. 设置Z坐标值
            if 'position' in state_data and 'extruder' in state_data:
                try:
                    pos = state_data['position']
                    active_extruder = state_data['extruder']['active_extruder']
                    z_pos = float(pos['z'])
                                       
                    # 设置当前Z坐标值
                    self.gcode.run_script_from_command(f"SET_KINEMATIC_POSITION Z={z_pos}")
                    logging.info(f"RESTORE_PRINT: Set Z position to {z_pos} for {active_extruder}")
                    
                except Exception as e:
                    logging.exception("RESTORE_PRINT: Error setting Z position")

            # 执行回零
            self.gcode.run_script_from_command("G28 X Y S")
            logging.info("RESTORE_PRINT: Homing completed")

            if active_extruder == 'extruder1':  # 右头
                self.gcode.run_script_from_command(f"T1 R0")
                
            # 等待温度
            if 'temperatures' in state_data:
                temps = state_data['temperatures']
                if 'extruder' in temps:
                    self.gcode.run_script_from_command(f"M109 T0 S{float(temps['extruder'])}")
                if 'extruder1' in temps:
                    self.gcode.run_script_from_command(f"M109 T1 S{float(temps['extruder1'])}")
                if 'bed' in temps:
                    self.gcode.run_script_from_command(f"M190 S{float(temps['bed'])}")
                logging.info("RESTORE_PRINT: Temperature reached")

            # 4. 加载文件
            logging.info("RESTORE_PRINT: Loading file: %s", file_path)
            f = io.open(file_path, 'r', newline='')
            f.seek(0, os.SEEK_END)
            fsize = f.tell()
            f.seek(file_position)
            self.current_file = f
            self.file_position = file_position
            self.file_size = fsize
            self.print_stats.set_current_file(os.path.basename(file_path))
            logging.info("RESTORE_PRINT: File loaded and positioned")

            # 5. 恢复打印设置
            if 'motion_mode' in state_data:
                motion = state_data['motion_mode']
                if motion.get('absolute_coordinates', 'true').lower() == 'true':
                    self.gcode.run_script_from_command("G90")
                else:
                    self.gcode.run_script_from_command("G91")
                if motion.get('absolute_extrude', 'true').lower() == 'true':
                    self.gcode.run_script_from_command("M82")
                else:
                    self.gcode.run_script_from_command("M83")

            if 'dual_carriage' in state_data:
                try:
                    dc_state = state_data['dual_carriage']
                    # 设置为保存的模式
                    if dc_state['carriage_1'] == 'COPY':
                        self.gcode.run_script_from_command(f"M605 S2")
                    elif dc_state['carriage_1'] == 'MIRROR':
                        self.gcode.run_script_from_command(f"M605 S3")
                        logging.info(f"RESTORE_PRINT: Restored dual carriage mode to {dc_state['carriage_1']}")
                except Exception as e:
                    logging.exception("RESTORE_PRINT: Error restoring dual carriage mode")
            
            # 6. 恢复位置
            if 'position' in state_data:
                pos = state_data['position']
                # 设置绝对坐标模式
                self.gcode.run_script_from_command("G90")
                # 先移动到XY轴位置
                self.gcode.run_script_from_command(f"G0 X{pos['x']} Y{pos['y']} F3000")
                self.gcode.run_script_from_command("M400")
                # 移动到Z轴位置
                self.gcode.run_script_from_command(f"G0 Z{pos['z']} F600")
                self.gcode.run_script_from_command("M400")
                # 先设置E轴位置为0
                self.gcode.run_script_from_command("G92 E0")
                logging.info("RESTORE_PRINT: Position restored to X:%.2f Y:%.2f Z:%.2f E:%.2f", 
                           float(pos['x']), float(pos['y']), float(pos['z']), float(pos['e']))

            # 7. 恢复速度设置
            if 'speed' in state_data:
                speed = state_data['speed']
                if 'speed_factor' in speed:
                    speed_value = float(speed['speed_factor']) * 50
                    self.gcode.run_script_from_command(f"M220 S{speed_value}")
                if 'extrude_factor' in speed:
                    extrude_value = float(speed['extrude_factor']) * 100
                    self.gcode.run_script_from_command(f"M221 S{extrude_value}")

            # 8. 恢复风扇设置
            if 'fans' in state_data:
                fans = state_data['fans']
                for fan_name, speed in fans.items():
                    if fan_name == 'nozzle_fan':
                        self.gcode.run_script_from_command(f"M106 S{int(float(speed)*255)}")
                    elif fan_name == 'nozzle_fan1':
                        self.gcode.run_script_from_command(f"M106 P1 S{int(float(speed)*255)}")
                    elif fan_name == 'auxiliary_fan':
                        self.gcode.run_script_from_command(f"M106 P2 S{int(float(speed)*255)}")

            # 9. 开始打印
            logging.info("RESTORE_PRINT: Starting print")
            self.work_timer = self.reactor.register_timer(
                self.work_handler, self.reactor.NOW)
            self.print_stats.note_start()
            logging.info("RESTORE_PRINT: Print started")

        except Exception as e:
            logging.exception("RESTORE_PRINT: Error during restore process")
            raise gcmd.error(f"Failed to restore print: {str(e)}")

def load_config(config):
    return VirtualSD(config)
