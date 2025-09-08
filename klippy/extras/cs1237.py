# cs1237 Support
#
# Copyright (C) 2024 Gareth Farrington <gareth@waves.ky>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
from . import bulk_sensor

#
# Constants
#
BYTES_PER_SAMPLE = 4  # samples are 4 byte wide unsigned integers
MAX_SAMPLES_PER_MESSAGE = bulk_sensor.MAX_BULK_MSG_SIZE // BYTES_PER_SAMPLE
UPDATE_INTERVAL = 0.10
SAMPLE_ERROR_DESYNC = -0x80000000
SAMPLE_ERROR_LONG_READ = 0x40000000

# Implementation of cs1237
class CS1237Base:
    HOLD_TIMES = 3
    def __init__(self, config, sensor_type,
                 sample_rate_options, default_sample_rate,
                 gain_options, default_gain):
        self.printer = printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.last_error_count = 0
        self.consecutive_fails = 0
        self.sensor_type = sensor_type
        # debug
        self.debug_flag = config.getboolean('debug', False)
        #data processing
        self.old_data = 0
        self.change_threshold = config.getint(
            'change_threshold', default=10000)
        self.change_num = config.getint('change_num', default=5)
        self.change_num_now = 0
        # Chip options
        dout_pin_name = config.get('dout_pin')
        sclk_pin_name = config.get('sclk_pin')
        ppins = printer.lookup_object('pins')
        dout_ppin = ppins.lookup_pin(dout_pin_name)
        sclk_ppin = ppins.lookup_pin(sclk_pin_name)
        self.mcu = mcu = dout_ppin['chip']
        self.oid = mcu.create_oid()
        if sclk_ppin['chip'] is not mcu:
            raise config.error("%s config error: All pins must be "
                               "connected to the same MCU" % (self.name,))
        self.dout_pin = dout_ppin['pin']
        self.sclk_pin = sclk_ppin['pin']
        # Samples per second choices
        speed_dist = {0:10, 1:40, 2:640, 3:1280}
        self.speed_sel = int(config.getchoice('sample_rate',
                                sample_rate_options,
                                default=default_sample_rate))
        self.sps = speed_dist[self.speed_sel]
        # self.sps = int(0.8 * self.sps)
        # Maximum number of error samples per data processing
        self.avg_num = config.getint(
            'number_of_averages', default=10, minval=1, maxval=64)
        self.max_error_samples_num = config.getint(
            'error_num', default=0, minval=0, maxval=10)
        self.hold_times = config.getint(
            'hold_times', self.HOLD_TIMES, minval=1)
        # (1.0/self.sps)*MAX_SAMPLES_PER_MESSAGE
        self.min_batch_time = config.getfloat(
            'min_batch_time', default=UPDATE_INTERVAL, minval=0.01)
        if (self.min_batch_time < (MAX_SAMPLES_PER_MESSAGE/self.sps)):
            self.min_batch_time = ((MAX_SAMPLES_PER_MESSAGE/self.sps) * 1.2)
        # gain/channel choices
        self.channel_sel = 0
        self.gain_sel = int(config.getchoice('gain', gain_options,
                                default=default_gain))
        ## Bulk Sensor Setup
        self.bulk_queue = bulk_sensor.BulkDataQueue(mcu, oid=self.oid)
        # Clock tracking
        chip_smooth = self.sps * self.min_batch_time * 2
        self.ffreader = bulk_sensor.FixedFreqReader(mcu, chip_smooth, "<i")
        # Process messages in batches
        self.batch_bulk = bulk_sensor.BatchBulkHelper(
            self.printer, self._process_batch, self._start_measurements,
            self._finish_measurements, self.min_batch_time)
        self.debug_info("cs1237 init data: t:%s s:%s g:%s"
            % (self.min_batch_time, self.sps, self.gain_sel,))
        # Command Configuration
        self.query_cs1237_cmd = None
        self.cmd_queue = self.mcu.alloc_command_queue()
        self.debug_info(
            "config_cs1237 oid=%d channel=%d gain=%d speed=%d"\
            " dout_pin=%s sclk_pin=%s avg_num=%d"
            % (self.oid, self.channel_sel, self.gain_sel, self.speed_sel,
               self.dout_pin, self.sclk_pin, self.avg_num))
        mcu.add_config_cmd(
            "config_cs1237 oid=%d channel=%d gain=%d speed=%d"\
            " dout_pin=%s sclk_pin=%s avg_num=%d"
            % (self.oid, self.channel_sel, self.gain_sel, self.speed_sel,
               self.dout_pin, self.sclk_pin, self.avg_num))
        mcu.add_config_cmd("query_cs1237 oid=%d rest_ticks=0"
                           % (self.oid,), on_restart=True)

        mcu.register_config_callback(self._build_config)

        self.gcode = self.printer.lookup_object('gcode')

    def _build_config(self):
        self.query_cs1237_cmd = self.mcu.lookup_command(
            "query_cs1237 oid=%c rest_ticks=%u"
        )
        self.ffreader.setup_query_command(
            "query_cs1237_status oid=%c",
            oid=self.oid,
            cq=self.cmd_queue
        )

        # start home
        self.cs1237_home_cmd = self.mcu.lookup_command(
            "cs1237_home oid=%c trsync_oid=%c trigger_reason=%c "\
            "error_reason=%c threshold_down=%u threshold_up=%u hold_times=%c",
            # "error_reason=%c threshold=%u hold_times=%c",
            cq=self.cmd_queue
        )
        # stop home
        self.query_cs1237_home_cmd = self.mcu.lookup_query_command(
            "query_cs1237_home oid=%c",
            "cs1237_home_state oid=%c trigger_clock=%u trigger_data=%u",
            oid=self.oid, cq=self.cmd_queue
        )

    def debug_info(self, msg=None):
        if (msg is not None) and (self.debug_flag):
            logging.info("cs1237_debug(%s): %s", self.name, msg)

    def get_mcu(self):
        return self.mcu

    def get_samples_per_second(self):
        return self.sps

    def get_bulk_update(self):
        return self.min_batch_time

    # returns a tuple of the minimum and maximum value of the sensor, used to
    # detect if a data value is saturated
    def get_range(self):
        # # 0 ~ ((1<<24)-1)
        # return 0x000000, 0xFFFFFF
        # (0.2*0xFFFFFF) ~ (0.8*0xFFFFFF)
        return 0x333333, 0xCCCCCC

    # add_client interface, direct pass through to bulk_sensor API
    def add_client(self, callback):
        self.batch_bulk.add_client(callback)

    # Measurement decoding
    def _convert_samples(self, samples):
        adc_factor = 1. / (1 << 23)
        count = 0
        continuous_counter = 0
        for ptime, val in samples:
            if continuous_counter > self.max_error_samples_num:
                # A sequential error occurs, exit early
                #self.debug_info("break")
                break
            elif val == SAMPLE_ERROR_DESYNC or val == SAMPLE_ERROR_LONG_READ:
                self.last_error_count += 1
                # break  # additional errors are duplicates
                continuous_counter += 1
                #self.debug_info("continue:%d"%(continuous_counter,))
                continue
            else:
                continuous_counter = 0
            # Get the changed value
            diff = abs(val - self.old_data)
            if ((diff > self.change_threshold) and
                (self.change_num_now<self.change_num)):
                new_data = self.old_data
                self.change_num_now += 1
            else:
                new_data = val
                self.old_data = new_data
                self.change_num_now = 0
            samples[count] = (round(ptime, 4), new_data,
                              round(new_data*adc_factor, 4))
            count += 1
        #self.debug_info("count:%d"%(count,))
        del samples[count:]

    # Start, stop, and process message batches
    def _start_measurements(self):
        self.consecutive_fails = 0
        self.last_error_count = 0
        # Start bulk reading
        rest_ticks = self.mcu.seconds_to_clock(1. / self.sps)
        self.query_cs1237_cmd.send([self.oid, rest_ticks])
        logging.info("%s starting '%s' measurements, oid:%s, tick:%s:%.3fms",
            self.sensor_type, self.name,
            self.oid, rest_ticks, (1000./self.sps))
        # Initialize clock tracking
        self.ffreader.note_start()

    def _finish_measurements(self):
        # don't use serial connection after shutdown
        if self.printer.is_shutdown():
            return
        # Halt bulk reading
        self.query_cs1237_cmd.send_wait_ack([self.oid, 0])
        self.ffreader.note_end()
        logging.info("%s finished '%s' measurements",
                    self.sensor_type, self.name)

    def _process_batch(self, eventtime):
        prev_overflows = self.ffreader.get_last_overflows()
        prev_error_count = self.last_error_count
        samples = self.ffreader.pull_samples()
        self._convert_samples(samples)

        overflows = self.ffreader.get_last_overflows() - prev_overflows
        errors = self.last_error_count - prev_error_count
        if errors > self.max_error_samples_num:
            logging.error("%s: Forced sensor restart due to error", self.name)
            self._finish_measurements()
            self._start_measurements()
        elif overflows > 0:
            self.consecutive_fails += 1
            if self.consecutive_fails > 4:
                logging.error("%s: Forced sensor restart due to overflows",
                              self.name)
                self._finish_measurements()
                self._start_measurements()
        else:
            self.consecutive_fails = 0
        self.debug_info("_p_b:%s" % (samples,))
        return {'data': samples, 'errors': errors,
                'overflows': overflows}

    def setup_home(self, ts_oid, trigger_reason, error_reason, down, up):
        # self._start_measurements()
        send_params = [self.oid, ts_oid, trigger_reason, error_reason,
             down, up, self.hold_times]
        self.debug_info("setup_home: %s" % (send_params,))
        # self.gcode.respond_info("setup_home: %s" % (send_params,))
        self.cs1237_home_cmd.send(send_params)

    def clear_home(self):
        send_params = [self.oid, 0, 0, 0, 0, 0, 0]
        self.cs1237_home_cmd.send(send_params)
        params = self.query_cs1237_home_cmd.send([self.oid])
        tclock = self.mcu.clock32_to_clock64(params['trigger_clock'])
        self.debug_info("clear_home: %s" % (params,))
        # self.gcode.respond_info("clear_home: %s" % (params,))
        return self.mcu.clock_to_print_time(tclock)
    
    # def setup_home(self, ts_oid, trigger_reason, error_reason, threshold):
    #     # self._start_measurements()
    #     send_params = [self.oid, ts_oid, trigger_reason, error_reason,
    #         threshold, self.hold_times]
    #     logging.info("setup_home: %s", send_params)
    #     self.cs1237_home_cmd.send(send_params)

    # def clear_home(self):
    #     send_params = [self.oid, 0, 0, 0, 0, 0]
    #     logging.info("clear_home: %s", send_params)
    #     self.cs1237_home_cmd.send(send_params)
    #     params = self.query_cs1237_home_cmd.send([self.oid])
    #     tclock = self.mcu.clock32_to_clock64(params['trigger_clock'])
    #     return self.mcu.clock_to_print_time(tclock)


def CS1237(config):
    return  CS1237Base(config, "cs1237",
                # CS1237 SPEED_SEL options, default 10Hz
                {'10': 0, '40': 1, '640': 2, '1280': 3}, '10',
                # CS1237 PGA_SEL options, default 1
                {'1': 0, '2': 1, '64': 2, '128': 3}, '1',
                # CS1237 CS_SEL options, default channel A
            )

CS1237_SENSOR_TYPES = {
    "cs1237": CS1237,
}
