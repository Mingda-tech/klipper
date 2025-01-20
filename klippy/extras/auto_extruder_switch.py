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
        
        # Register commands
        self.gcode.register_command(
            'ENABLE_AUTO_EXTRUDER_SWITCH', self.cmd_ENABLE_AUTO_EXTRUDER_SWITCH,
            desc=self.cmd_ENABLE_AUTO_EXTRUDER_SWITCH_help)
        self.gcode.register_command(
            'DISABLE_AUTO_EXTRUDER_SWITCH', self.cmd_DISABLE_AUTO_EXTRUDER_SWITCH,
            desc=self.cmd_DISABLE_AUTO_EXTRUDER_SWITCH_help)
            
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
        self.filament_switch_sensors = []
        
        # Find all filament sensors
        for name in self.printer.lookup_objects('filament_switch_sensor'):
            self.filament_switch_sensors.append(name[1])
            
    def _handle_printing(self, print_time):
        if self.auto_switch_enabled:
            self.reactor.update_timer(self.check_timer, self.reactor.NOW)
            
    def _handle_not_printing(self, print_time):
        self.reactor.update_timer(self.check_timer, self.reactor.NEVER)
        
    def _check_conditions(self, eventtime):
        if not self.auto_switch_enabled:
            return self.reactor.NEVER
            
        # Get current extruder
        cur_extruder = self.toolhead.get_extruder()
        cur_extruder_name = cur_extruder.get_name()
        
        # Check if we are in single extruder mode
        extruders = []
        for name in self.printer.lookup_objects('extruder'):
            if name[0] != 'extruder_stepper':
                extruders.append(name[1])
                
        if len(extruders) < 2:
            return eventtime + 1.
            
        # Check if current extruder has no filament
        cur_sensor = None
        for sensor in self.filament_switch_sensors:
            if sensor.name == cur_extruder_name:
                cur_sensor = sensor
                break
                
        if cur_sensor is None or cur_sensor.filament_present:
            return eventtime + 1.
            
        # Find another extruder with filament
        for extruder in extruders:
            if extruder.get_name() == cur_extruder_name:
                continue
                
            # Check if this extruder has filament
            for sensor in self.filament_switch_sensors:
                if sensor.name == extruder.get_name() and sensor.filament_present:
                    # Switch to this extruder
                    self.gcode.run_script_from_command(
                        "ACTIVATE_EXTRUDER EXTRUDER=%s" % (extruder.get_name(),))
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