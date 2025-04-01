import logging

ADC_REPORT_TIME = 0.500
ADC_SAMPLE_TIME = 0.03
ADC_SAMPLE_COUNT = 15

class ADCSensor:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.adc_name = config.get_name().split()[-1]
        self.adc_sensor_pin = config.get('pin')
        self.runout_range = config.getfloatlist('runout_range', count=2)
        self.inversion = config.getboolean('inversion', False)
        self.raw_value = 0
        self.state = "triggered" # or "open", default state
        # self.is_log =config.getboolean('logging', False)
        self.report_time = config.getfloat('report_time', ADC_REPORT_TIME)
        self.sample_time = config.getfloat('sample_time', ADC_SAMPLE_TIME)
        self.sample_count = config.getint('sample_count', ADC_SAMPLE_COUNT)
        if (self.report_time < (2*self.sample_time*self.sample_count)):
            raise config.error("\"report_time\" cannot be less than "
                               "\"(2*sample_time*sample_count)\"")
        # self.toolhead = None
        # self.printer.register_event_handler("klippy:ready", self.handle_ready)
        # Start adc
        self.ppins = self.mcu_adc = None
        self.ppins = self.printer.lookup_object('pins')
        self.mcu_adc = self.ppins.setup_pin('adc', self.adc_sensor_pin)
        self.mcu_adc.setup_adc_sample(self.sample_time, self.sample_count)
        self.mcu_adc.setup_adc_callback(self.report_time, self.adc_callback)
        # Register commands
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_mux_command(
            'ADC_SENSOR_QUERY', "SENSOR", self.adc_name,
            self.cmd_ADC_SENSOR_QUERY,
            desc=self.cmd_ADC_SENSOR_QUERY_help)

    # # Initialization
    # def handle_ready(self):
    #     # Load printer objects
    #     self.toolhead = self.printer.lookup_object('toolhead')

    def adc_callback(self, read_time, read_value):
        # read sensor value
        self.raw_value = read_value
        if self.inversion:
            if ((self.raw_value > self.runout_range[1]) and (self.state != "open")):
                self.state = "open"
            elif ((self.raw_value < self.runout_range[0]) and (self.state != "triggered")):
                self.state = "triggered"
        else:
            if ((self.raw_value > self.runout_range[1]) and (self.state == "open")):
                self.state = "triggered"
            elif ((self.raw_value < self.runout_range[0]) and (self.state == "triggered")):
                self.state = "open"

    cmd_ADC_SENSOR_QUERY_help = "Query the adc sensor value"
    def cmd_ADC_SENSOR_QUERY(self, gcmd):
        data = self.get_status(None)
        response = "%s: %s" % (str(self.adc_name), data, )
        self.gcode.respond_info(response)
        return

    def get_status(self, eventtime):
        return {
            'raw': self.raw_value,
            'state': self.state,
        }

def load_config_prefix(config):
    return ADCSensor(config)
