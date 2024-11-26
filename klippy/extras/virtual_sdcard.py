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
        self.cmd_counter = 0
        self.save_state_threshold = 30
        config_path = os.path.expanduser('~/printer_data/config')
        self.state_file_1 = os.path.join(config_path, 'print_state.cfg')
        self.state_file_2 = os.path.join(config_path, 'print_state_temp.cfg')
        self.last_saved_to_first = True
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
            # 检查是否需要保存状态
            self.cmd_counter += 1
            if self.cmd_counter >= self.save_state_threshold:
                self.save_print_state()
                self.cmd_counter = 0
            # 检查是否是换层命令 (M73)
            if line.strip().startswith('M73'):
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
    def save_print_state(self):
        if self.current_file is None:
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
            # 获取工具头对象和位置信息
            gcode_move = self.printer.lookup_object('gcode_move')
            
            # 获取相对坐标
            if gcode_move:
                gcode_position = gcode_move.get_status(self.reactor.monotonic())['gcode_position']
                config['position'] = {
                    'x': '{:.2f}'.format(gcode_position[0]),
                    'y': '{:.2f}'.format(gcode_position[1]),
                    'z': '{:.2f}'.format(gcode_position[2]),
                    'e': '{:.2f}'.format(gcode_position[3])
                }
            
            # 从toolhead获取当前活跃挤出头信息
            toolhead = self.printer.lookup_object('toolhead')
            if toolhead:
                config['extruder'] = {}
                active_extruder = toolhead.get_extruder().get_name()
                config['extruder']['active_extruder'] = str(active_extruder)
                
            # 获取所有挤出头温度
            config['temperatures'] = {}
            for i in range(2):  # 最多支持2个挤出头
                extruder_name = 'extruder' if i == 0 else f'extruder{i}'
                extruder = self.printer.lookup_object(extruder_name, None)
                if extruder:
                    temp = extruder.get_status(self.reactor.monotonic())['temperature']
                    config['temperatures'][extruder_name] = '{:.2f}'.format(temp)
                    
            # 获取热床温度
            heater_bed = self.printer.lookup_object('heater_bed', None)
            if heater_bed:
                temp = heater_bed.get_status(self.reactor.monotonic())['temperature']
                config['temperatures']['bed'] = '{:.2f}'.format(temp)
            
            # 获取打印速度
            if gcode_move:
                speed = gcode_move.get_status(self.reactor.monotonic())['speed']
                speed_factor = gcode_move.get_status(self.reactor.monotonic())['speed_factor']
                config['speed'] = {
                    'speed': '{:.2f}'.format(speed),
                    'speed_factor': '{:.2f}'.format(speed_factor)
                }
            
            # 获取风扇速度
            fan = self.printer.lookup_object('fan', None)
            if fan:
                speed = fan.get_status(self.reactor.monotonic())['speed']
                config['fan'] = {
                    'speed': '{:.2f}'.format(speed)
                }
                
        except:
            logging.exception("Error getting printer state data")
        
        # 交替保存到两个文件
        save_file = self.state_file_1 if self.last_saved_to_first else self.state_file_2
        try:
            with open(save_file, 'w') as f:
                config.write(f)
            self.last_saved_to_first = not self.last_saved_to_first
        except:
            logging.exception("Error saving print state")
    # 添加恢复打印的命令处理函数
    cmd_RESTORE_PRINT_help = "Restore the previous print after power loss"
    def cmd_RESTORE_PRINT(self, gcmd):
        if self.work_timer is not None:
            raise gcmd.error("Already printing")
        
        # 尝试读取两个状态文件
        config = configparser.ConfigParser()
        state_file = None
        state_data = None
        
        try:
            # 先尝试读取第一个文件
            config.read(self.state_file_1)
            if 'print_state' in config:
                state_file = self.state_file_1
                state_data = config
        except:
            logging.exception("Error reading first state file")
        
        if state_data is None:
            try:
                # 如果第一个文件无效，尝试读取第二个文件
                config = configparser.ConfigParser()
                config.read(self.state_file_2)
                if 'print_state' in config:
                    state_file = self.state_file_2
                    state_data = config
            except:
                logging.exception("Error reading second state file")
        
        if state_data is None:
            raise gcmd.error("No valid print state found")
        
        try:
            # 获取打印状态
            print_state = state_data['print_state']
            file_path = print_state['file_path']
            file_position = int(print_state['file_position'])
            
            # 重置当前打印状态
            self._reset_file()
            
            # 使用新函数加载文件
            self._load_file_by_path(gcmd, file_path)
            
            # 设置文件位置
            self.file_position = file_position
            
            # 如果有温度信息，恢复温度
            if 'temperatures' in state_data:
                temps = state_data['temperatures']
                if 'extruder' in temps:
                    self.gcode.run_script(f"M104 S{float(temps['extruder'])}")
                if 'bed' in temps:
                    self.gcode.run_script(f"M140 S{float(temps['bed'])}")
            
            # 等待温度达到目标值
            if 'temperatures' in state_data:
                temps = state_data['temperatures']
                if 'extruder' in temps or 'bed' in temps:
                    self.gcode.run_script("M109 R" + temps['extruder'])
                    self.gcode.run_script("M190 R" + temps['bed'])
            
            # 如果有位置信息，恢复位置
            if 'position' in state_data:
                pos = state_data['position']
                self.gcode.run_script(f"G92 X{pos['x']} Y{pos['y']} Z{pos['z']} E{pos['e']}")
            
            # 如果有速度信息，恢复速度
            if 'speed' in state_data:
                speed = state_data['speed']
                if 'speed_factor' in speed:
                    self.gcode.run_script(f"M220 S{float(speed['speed_factor'])*100}")
            
            # 开始打印
            self.do_resume()
            
            # 删除状态文件
            if state_file:
                try:
                    os.remove(state_file)
                except:
                    logging.exception("Error removing state file")
                
        except:
            logging.exception("Error restoring print")
            raise gcmd.error("Failed to restore print")
    def _load_file_by_path(self, gcmd, filepath):
        try:
            f = io.open(filepath, 'r', newline='')
            f.seek(0, os.SEEK_END)
            fsize = f.tell()
            f.seek(0)
        except:
            logging.exception("virtual_sdcard file open")
            raise gcmd.error("Unable to open file")
        
        gcmd.respond_raw("File opened:%s Size:%d" % (filepath, fsize))
        gcmd.respond_raw("File selected")
        self.current_file = f
        self.file_position = 0
        self.file_size = fsize
        self.print_stats.set_current_file(filepath)

def load_config(config):
    return VirtualSD(config)
