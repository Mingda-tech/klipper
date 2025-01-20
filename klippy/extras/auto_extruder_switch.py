# Auto extruder switch support
#
# Copyright (C) 2024  <your name>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging

class AutoExtruderSwitch:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.auto_switch_enabled = False
        self.is_paused = False
        self.right_head_only = False  # 是否只使用右打印头
        self.left_head_only = False  # 是否只使用左打印头
        
        # Register commands
        self.gcode.register_command(
            'ENABLE_AUTO_EXTRUDER_SWITCH', self.cmd_ENABLE_AUTO_EXTRUDER_SWITCH,
            desc=self.cmd_ENABLE_AUTO_EXTRUDER_SWITCH_help)
        self.gcode.register_command(
            'DISABLE_AUTO_EXTRUDER_SWITCH', self.cmd_DISABLE_AUTO_EXTRUDER_SWITCH,
            desc=self.cmd_DISABLE_AUTO_EXTRUDER_SWITCH_help)
        self.gcode.register_command(
            'START_PRINT', self.cmd_START_PRINT,
            desc=self.cmd_START_PRINT_help)
            
        # Register event handlers
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.printer.register_event_handler("idle_timeout:printing", 
                                          self._handle_printing)
        self.printer.register_event_handler("idle_timeout:ready",
                                          self._handle_not_printing)
        self.printer.register_event_handler("idle_timeout:idle",
                                          self._handle_not_printing)
                                          
        # Setup timer for checking conditions
        self.check_timer = self.reactor.register_timer(
            self._check_conditions)
            
    def _handle_ready(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        self.pause_resume = self.printer.lookup_object('pause_resume')
        self.dual_carriage = self.printer.lookup_object('dual_carriage', None)
        self.filament_switch_sensors = []
        
        # Find all filament sensors
        for name in self.printer.lookup_objects('filament_switch_sensor'):
            self.filament_switch_sensors.append(name[1])
            
        # 监听暂停状态变化
        self.printer.register_event_handler('pause_resume:paused', 
                                          self._handle_paused)
        self.printer.register_event_handler('pause_resume:resumed', 
                                          self._handle_resumed)
            
    def _handle_paused(self):
        self.is_paused = True
        # 如果启用了自动切换，立即检查是否需要切换
        if self.auto_switch_enabled:
            self.reactor.update_timer(self.check_timer, self.reactor.NOW)
            
    def _handle_resumed(self):
        self.is_paused = False
            
    def _handle_printing(self, print_time):
        if self.auto_switch_enabled:
            self.reactor.update_timer(self.check_timer, self.reactor.NOW)
            
    def _handle_not_printing(self, print_time):
        self.reactor.update_timer(self.check_timer, self.reactor.NEVER)
        self.is_paused = False
        self.right_head_only = False  # 重置右头标志
        self.left_head_only = False  # 重置左头标志

    cmd_START_PRINT_help = "Start the print with specified temperatures"
    def cmd_START_PRINT(self, gcmd):
        # 检查打印头温度设置
        extruder_temp = gcmd.get_float('EXTRUDER', 0)
        extruder1_temp = gcmd.get_float('EXTRUDER1', 0)
        
        # 如果只设置了右打印头温度，标记为右头打印
        self.right_head_only = (extruder1_temp > 0 and extruder_temp == 0)
        self.left_head_only = (extruder_temp > 0 and extruder1_temp == 0)
        # 转发原始命令
        self.gcode.run_script_from_command(gcmd.get_raw_command_parameters())
        
    def _is_single_extruder_print(self):
        # 1. 如果只设置了右头温度，则为单头打印
        if self.right_head_only:
            return True
            
        # 2. 如果只设置了左头温度
        if self.left_head_only:
            # 2.1 如果没有配置dual_carriage，认为是单头打印
            if self.dual_carriage is None:
                return True
                
            # 2.2 如果配置了dual_carriage，检查打印模式
            status = self.dual_carriage.get_status()
            carriage_1_mode = status.get('carriage_1', 'PRIMARY')
            # 如果第二个打印头不是COPY或MIRROR模式，则是单头打印
            return carriage_1_mode not in ['COPY', 'MIRROR']
            
        return False
        
    def _check_conditions(self, eventtime):
        if not self.auto_switch_enabled:
            return self.reactor.NEVER
            
        # 如果不是单头打印，不执行自动切换
        if not self._is_single_extruder_print():
            return eventtime + 1.
            
        # 如果没有暂停，不执行切换
        if not self.is_paused:
            return eventtime + 1.
            
        # Get current extruder
        cur_extruder = self.toolhead.get_extruder()
        cur_extruder_name = cur_extruder.get_name()
        
        # Check if current extruder has no filament
        cur_sensor = None
        for sensor in self.filament_switch_sensors:
            if sensor.name == cur_extruder_name:
                cur_sensor = sensor
                break
                
        if cur_sensor is None or cur_sensor.filament_present:
            return eventtime + 1.
            
        # Find another extruder with filament
        extruders = []
        for name in self.printer.lookup_objects('extruder'):
            if name[0] != 'extruder_stepper':
                extruders.append(name[1])
                
        for extruder in extruders:
            if extruder.get_name() == cur_extruder_name:
                continue
                
            # Check if this extruder has filament
            for sensor in self.filament_switch_sensors:
                if sensor.name == extruder.get_name() and sensor.filament_present:
                    # Switch to this extruder using T0/T1 command
                    if extruder.get_name() == 'extruder':
                        self.gcode.run_script_from_command("T0")
                    else:
                        self.gcode.run_script_from_command("T1")
                    # 恢复打印
                    self.gcode.run_script_from_command("RESUME")
                    return eventtime + 1.
                    
        return eventtime + 1.
        
    cmd_ENABLE_AUTO_EXTRUDER_SWITCH_help = "Enable automatic extruder switching"
    def cmd_ENABLE_AUTO_EXTRUDER_SWITCH(self, gcmd):
        if self.auto_switch_enabled:
            gcmd.respond_info("Auto extruder switch already enabled")
            return
            
        self.auto_switch_enabled = True
        self.reactor.update_timer(self.check_timer, self.reactor.NOW)
        gcmd.respond_info("Auto extruder switch enabled")
        
    cmd_DISABLE_AUTO_EXTRUDER_SWITCH_help = "Disable automatic extruder switching"
    def cmd_DISABLE_AUTO_EXTRUDER_SWITCH(self, gcmd):
        if not self.auto_switch_enabled:
            gcmd.respond_info("Auto extruder switch already disabled")
            return
            
        self.auto_switch_enabled = False
        self.reactor.update_timer(self.check_timer, self.reactor.NEVER)
        gcmd.respond_info("Auto extruder switch disabled")

def load_config(config):
    return AutoExtruderSwitch(config) 