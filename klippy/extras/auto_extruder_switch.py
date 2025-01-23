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
        self.right_head_only = False  # 是否只使用右打印头
        self.left_head_only = False  # 是否只使用左打印头
        
        # 保存打印状态
        self.saved_state = {
            'speed_factor': 1.0,  # M220
            'extrude_factor': 1.0,  # M221
            'pressure_advance': 0.0,
            'smooth_time': 0.0,
            'print_speed': 0.0  # 添加打印速度字段
        }
        
        # Register commands
        self.gcode.register_command(
            'ENABLE_AUTO_EXTRUDER_SWITCH', self.cmd_ENABLE_AUTO_EXTRUDER_SWITCH,
            desc=self.cmd_ENABLE_AUTO_EXTRUDER_SWITCH_help)
        self.gcode.register_command(
            'DISABLE_AUTO_EXTRUDER_SWITCH', self.cmd_DISABLE_AUTO_EXTRUDER_SWITCH,
            desc=self.cmd_DISABLE_AUTO_EXTRUDER_SWITCH_help)
        self.gcode.register_command(
            'CHECK_AND_SWITCH_EXTRUDER', self.cmd_CHECK_AND_SWITCH_EXTRUDER,
            desc=self.cmd_CHECK_AND_SWITCH_EXTRUDER_help)
            
        # Register START_PRINT wrapper
        self.original_start_print = self.gcode.register_command('START_PRINT', None)
        if self.original_start_print is not None:
            self.gcode.register_command('START_PRINT', self._handle_gcode_command)
            
        # Register event handlers
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        
    def _handle_ready(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        self.pause_resume = self.printer.lookup_object('pause_resume')
        self.dual_carriage = self.printer.lookup_object('dual_carriage', None)
        
        # 获取料丝传感器
        self.sensor0 = self.printer.lookup_object('filament_switch_sensor Filament_Sensor0', None)
        self.sensor1 = self.printer.lookup_object('filament_switch_sensor Filament_Sensor1', None)
        
    def _save_current_state(self):
        """保存当前打印头的状态"""
        gcode_move = self.printer.lookup_object('gcode_move')
        cur_extruder = self.toolhead.get_extruder()
        
        # 保存速度和流量比例
        self.saved_state['speed_factor'] = gcode_move._get_gcode_speed_override()
        self.saved_state['extrude_factor'] = gcode_move.extrude_factor
        
        # 保存压力提前
        if hasattr(cur_extruder, 'pressure_advance'):
            self.saved_state['pressure_advance'] = cur_extruder.pressure_advance
            self.saved_state['smooth_time'] = cur_extruder.pressure_advance_smooth_time
            
        # 保存当前打印速度 (mm/min)
        self.saved_state['print_speed'] = gcode_move._get_gcode_speed()
        
        # 打印保存的状态
        logging.info("保存打印状态: speed_factor=%.2f, print_speed=%.2f mm/min, extrude_factor=%.2f, pressure_advance=%.4f",
                    self.saved_state['speed_factor'],
                    self.saved_state['print_speed'],
                    self.saved_state['extrude_factor'],
                    self.saved_state['pressure_advance'])
            
    def _restore_state_to_extruder(self, extruder_name):
        """将保存的状态恢复到指定打印头"""
        
        # 打印要恢复的状态和计算值
        base_speed = self.saved_state['print_speed']  # 已经是mm/min
        logging.info("恢复打印状态到 %s: speed_factor=%.2f, print_speed=%.2f mm/min, extrude_factor=%.2f",
                    extruder_name,
                    self.saved_state['speed_factor'],
                    self.saved_state['print_speed'],
                    self.saved_state['extrude_factor'])
        
        # 恢复流量比例
        self.gcode.run_script_from_command(
            "M221 S%.0f" % (self.saved_state['extrude_factor'] * 100.))
            
        # 恢复压力提前
        self.gcode.run_script_from_command(
            "SET_PRESSURE_ADVANCE EXTRUDER=%s ADVANCE=%.4f SMOOTH_TIME=%.4f" % 
            (extruder_name, self.saved_state['pressure_advance'], 
             self.saved_state['smooth_time']))
             
        # 恢复速度因子和打印速度
        # 注意：先设置速度因子，再设置基础速度
        self.gcode.run_script_from_command(
            "M220 S%.0f" % (self.saved_state['speed_factor'] * 100.))
        self.gcode.run_script_from_command(
            "G1 F%.1f" % base_speed)
            
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
        
    def _handle_gcode_command(self, gcmd):
        cmd = gcmd.get_command()
        if cmd == 'START_PRINT':
            # 检查打印头温度设置
            extruder_temp = gcmd.get_float('EXTRUDER', 0)
            extruder1_temp = gcmd.get_float('EXTRUDER1', 0)
            
            # 如果只设置了右打印头温度，标记为右头打印
            self.right_head_only = (extruder1_temp > 150 and extruder_temp < 150)
            self.left_head_only = (extruder_temp > 150 and extruder1_temp < 150)
            
            # 执行原始的 START_PRINT 命令
            if self.original_start_print is not None:
                self.original_start_print(gcmd)
                
    cmd_CHECK_AND_SWITCH_EXTRUDER_help = "检查并切换打印头（如果需要）"
    def cmd_CHECK_AND_SWITCH_EXTRUDER(self, gcmd):
        if not self.auto_switch_enabled:
            self.gcode.run_script_from_command("M118 Filament run out, pausing print")
            self.gcode.run_script_from_command("PAUSE")
            return
            
        # 如果不是单头打印，不执行自动切换
        if not self._is_single_extruder_print():
            self.gcode.run_script_from_command("M118 Not in single extruder mode, pausing print")
            self.gcode.run_script_from_command("PAUSE")
            return
            
        # Get current extruder and position
        cur_extruder = self.toolhead.get_extruder()
        cur_extruder_name = cur_extruder.get_name()
        
        # 根据当前打印头选择对应的传感器
        cur_sensor = self.sensor0 if cur_extruder_name == 'extruder' else self.sensor1
        other_sensor = self.sensor1 if cur_extruder_name == 'extruder' else self.sensor0
        other_extruder_name = 'extruder1' if cur_extruder_name == 'extruder' else 'extruder'
        
        if cur_sensor is None:
            self.gcode.run_script_from_command("M118 Current sensor not configured, pausing print")
            self.gcode.run_script_from_command("PAUSE")
            return
            
        if other_sensor is None:
            self.gcode.run_script_from_command("M118 Other sensor not configured, pausing print")
            self.gcode.run_script_from_command("PAUSE")
            return
            
        # 获取传感器状态
        cur_status = cur_sensor.get_status(self.reactor.monotonic())
        other_status = other_sensor.get_status(self.reactor.monotonic())
        
        if cur_status['filament_detected']:
            self.gcode.run_script_from_command("M118 Current extruder has filament, continuing print")
            return
            
        if not other_status['filament_detected']:
            self.gcode.run_script_from_command("M118 Both extruders out of filament, pausing print")
            self.gcode.run_script_from_command("PAUSE")
            return
            
        self.gcode.run_script_from_command("M118 Filament run out, switching extruder")
        
        # 保存当前打印头状态
        self._save_current_state()

        # 抬升Z轴2mm
        self.gcode.run_script_from_command("G91")  # Relative positioning
        self.gcode.run_script_from_command("G1 Z2 F600")
        self.gcode.run_script_from_command("G90")  # Absolute positioning
        
        # 获取当前打印头的温度
        cur_heater = self.printer.lookup_object(cur_extruder_name)
        other_heater = self.printer.lookup_object(other_extruder_name)
        cur_temp = cur_heater.get_status(self.reactor.monotonic())['target']
        other_temp = other_heater.get_status(self.reactor.monotonic())['target']
        
        # 如果另一个打印头温度太低，先预热
        if other_temp < cur_temp - 30:  # Allow 30 degree difference
            if other_extruder_name == 'extruder':
                self.gcode.run_script_from_command("M104 T0 S%.1f" % cur_temp)
                self.gcode.run_script_from_command("M104 T1 S0")
            else:
                self.gcode.run_script_from_command("M104 T1 S%.1f" % cur_temp)
                self.gcode.run_script_from_command("M104 T0 S0")
            # Wait for heating
            if other_extruder_name == 'extruder':
                self.gcode.run_script_from_command("M109 T0 S%.1f" % cur_temp)
            else:
                self.gcode.run_script_from_command("M109 T1 S%.1f" % cur_temp)
        
      
        # 切换打印头
        if other_extruder_name == 'extruder':
            # Switch to left extruder
            self.gcode.run_script_from_command("M118 Switching to left extruder")
            self.gcode.run_script_from_command("T0")  # Let T0 macro handle all offsets
        else:
            # Switch to right extruder
            self.gcode.run_script_from_command("M118 Switching to right extruder")
            self.gcode.run_script_from_command("T1")
            
        # Wait a moment to ensure switch is complete
        self.toolhead.dwell(0.5)
        
        # Lower Z by 2mm
        self.gcode.run_script_from_command("G91")  # Relative positioning
        self.gcode.run_script_from_command("G1 Z-2 F600")
        self.gcode.run_script_from_command("G90")  # Absolute positioning
        
        # Sync extruder position
        self.gcode.run_script_from_command("G92 E0")  # Reset extruder position

        # Restore extruder state
        self._restore_state_to_extruder(other_extruder_name)
        
        # Show completion message
        self.gcode.run_script_from_command("M118 Extruder switch complete, resuming print")
        
    cmd_ENABLE_AUTO_EXTRUDER_SWITCH_help = "Enable automatic extruder switching"
    def cmd_ENABLE_AUTO_EXTRUDER_SWITCH(self, gcmd):
        if self.auto_switch_enabled:
            self.gcode.run_script_from_command("M118 Auto extruder switch already enabled")
            return
            
        self.auto_switch_enabled = True
        self.gcode.run_script_from_command("M118 Auto extruder switch enabled, this feature only works when printing with a single extruder")
        
    cmd_DISABLE_AUTO_EXTRUDER_SWITCH_help = "Disable automatic extruder switching"
    def cmd_DISABLE_AUTO_EXTRUDER_SWITCH(self, gcmd):
        if not self.auto_switch_enabled:
            self.gcode.run_script_from_command("M118 Auto extruder switch already disabled")
            return
            
        self.auto_switch_enabled = False
        self.gcode.run_script_from_command("M118 Auto extruder switch disabled")

def load_config(config):
    return AutoExtruderSwitch(config) 