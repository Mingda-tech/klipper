import logging

class TemperatureComparator:
    def __init__(self, config):
        self._last_temperature = None
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.master_sensor_id = config.get('master_sensor_id')
        self.slave_sensor_id = config.get('slave_sensor_id')
        self.check_heating = []
        heating = config.getlist('check_heating', default=[])
        if len(heating) > 0:
            self.check_heating = [word[0].lower() for word in heating]
        self.debug_flag = config.getboolean('debug', default=False)
        self.sift_num = config.getint('sift_num', default=1, minval=1)
        self.trigger_threshold = config.getfloat('trigger_threshold')
        self.check_time = config.getfloat(
            'check_time', default=1.0, minval=0.01)
        self._triggered_flag = 0
        self.master_temp = self.slave_temp = 0.0

        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_mux_command(
            'TEMP_COMP_CLEAN_FLAG', 'NAME', self.name,
            self.cmd_CLEAN_FLAG, desc=self.cmd_CLEAN_FLAG_help)
        gcode_macro = self.printer.load_object(config, 'gcode_macro')
        self.trigger_run_gcode = gcode_macro.load_template(
            config, 'trigger_run_gcode', '')
        self.printer.register_event_handler(
            "klippy:connect", self.handle_connect)

    def handle_connect(self):
        self.pheaters = self.printer.lookup_object('heaters')
        self.master_sensor_obj = self._get_sensor_object(self.master_sensor_id)
        self.slave_sensor_obj = self._get_sensor_object(self.slave_sensor_id)
        self._debug_info("starting checks:%s" % (self.check_heating,))
        reactor = self.printer.get_reactor()
        waketime = reactor.monotonic() + self.check_time
        self.check_timer = reactor.register_timer(self._check_event, waketime)
    
    def _get_sensor_object(self, sensor_id):
        if sensor_id in self.pheaters.gcode_id_to_sensor:
            return self.pheaters.gcode_id_to_sensor[sensor_id]
        else:
            raise self.printer.config_error(
                "Unknown sensor ID: %s" % sensor_id)

    def _check_event(self, eventtime):
        master_temp, master_target = self.master_sensor_obj.get_temp(eventtime)
        slave_temp, slave_target = self.slave_sensor_obj.get_temp(eventtime)
        self.master_temp = round(master_temp, 2)
        self.slave_temp = round(slave_temp, 2)
        heating_flag = 0
        if ('m' in self.check_heating) and ('s' in self.check_heating):
            if (master_target > 0.0) or (slave_target > 0.0):
                heating_flag = 1
        elif ('m' in self.check_heating):
            if (master_target > 0.0):
                heating_flag = 1
        elif ('s' in self.check_heating):
            if (slave_target > 0.0):
                heating_flag = 1
        else:
            heating_flag = 1
        
        diff_temp = self.master_temp - self.slave_temp
        if (diff_temp > self.trigger_threshold) and heating_flag:
            if self._triggered_flag < 0:
                self._triggered_flag = 1
            elif self._triggered_flag < self.sift_num:
                self._triggered_flag += 1
            elif self._triggered_flag == self.sift_num:
                self._triggered_flag = 127
                self.gcode.run_script(self.trigger_run_gcode.render())
        elif ((-diff_temp) > self.trigger_threshold) and heating_flag:
            if self._triggered_flag > 0:
                self._triggered_flag = -1
            elif (-self._triggered_flag) < self.sift_num:
                self._triggered_flag -= 1
            elif (-self._triggered_flag) == self.sift_num:
                self._triggered_flag = -127
                self.gcode.run_script(self.trigger_run_gcode.render())
        else:
            self._triggered_flag = 0
        self._debug_info("Time:%s, Master: %s, Slave: %s" %
                         (eventtime, master_temp, slave_temp))
        return eventtime + self.check_time
    
    def _debug_info(self, msg=None):
        if (msg is not None) and (self.debug_flag):
            logging.info("Temperature comparator(%s): %s", self.name, msg)

    def get_status(self, eventtime):
        res = {
            'msster_sensor_temp': self.master_temp,
            'slave_sensor_temp': self.slave_temp,
            'triggered_flag': self._triggered_flag,
        }
        return res

    cmd_CLEAN_FLAG_help = "Clean the temperature comparator triggered flag"
    def cmd_CLEAN_FLAG(self, gcmd):
        self._triggered_flag = 0


def load_config_prefix(config):
    return TemperatureComparator(config)
