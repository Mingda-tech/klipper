# Load Cell Implementation
#
# Copyright (C) 2024 Gareth Farrington <gareth@waves.ky>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from . import hx71x, probe, cs1237
from . import ads1220
from .bulk_sensor import BatchWebhooksClient
import collections, itertools
import chelper, logging, math, mcu
# We want either Python 3's zip() or Python 2's izip() but NOT 2's zip():
zip_impl = zip
try:
    from itertools import izip as zip_impl # python 2.x izip
except ImportError: # will be Python 3.x
    pass

# alternative to numpy's column selection:
def select_column(data, column_idx):
    return list(zip_impl(*data))[column_idx]

def avg(data):
    return sum(data) / len(data)

# Helper for event driven webhooks and subscription based API clients
class ApiClientHelper(object):
    def __init__(self, printer):
        self.printer = printer
        self.client_cbs = []
        self.webhooks_start_resp = {}

    # send data to clients
    def send(self, msg):
        for client_cb in list(self.client_cbs):
            res = client_cb(msg)
            if not res:
                # This client no longer needs updates - unregister it
                self.client_cbs.remove(client_cb)

    # Add a client that gets data callbacks
    def add_client(self, client_cb):
        self.client_cbs.append(client_cb)

    # Add Webhooks client and send header
    def _add_webhooks_client(self, web_request):
        whbatch = BatchWebhooksClient(web_request)
        self.add_client(whbatch.handle_batch)
        web_request.send(self.webhooks_start_resp)

    # Set up a webhooks endpoint with a static header
    def add_mux_endpoint(self, path, key, value, webhooks_start_resp):
        self.webhooks_start_resp = webhooks_start_resp
        wh = self.printer.lookup_object('webhooks')
        wh.register_mux_endpoint(path, key, value, self._add_webhooks_client)

# Class for handling commands related ot load cells
class LoadCellCommandHelper:
    def __init__(self, config, load_cell):
        self.printer = config.get_printer()
        self.load_cell = load_cell
        name_parts = config.get_name().split()
        self.name = name_parts[-1]
        self.register_commands(self.name)
        if len(name_parts) == 1:
            self.register_commands(None)

    def register_commands(self, name):
        # Register commands
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command(
            "LOAD_CELL_TARE", "LOAD_CELL", name,
            self.cmd_LOAD_CELL_TARE, desc=self.cmd_LOAD_CELL_TARE_help)
        gcode.register_mux_command(
            "LOAD_CELL_CALIBRATE", "LOAD_CELL", name,
            self.cmd_LOAD_CELL_CALIBRATE,
            desc=self.cmd_CALIBRATE_LOAD_CELL_help)
        gcode.register_mux_command(
            "LOAD_CELL_READ", "LOAD_CELL", name,
            self.cmd_LOAD_CELL_READ, desc=self.cmd_LOAD_CELL_READ_help)
        gcode.register_mux_command(
            "LOAD_CELL_DIAGNOSTIC", "LOAD_CELL", name,
            self.cmd_LOAD_CELL_DIAGNOSTIC,
            desc=self.cmd_LOAD_CELL_DIAGNOSTIC_help)
        gcode.register_mux_command(
            "LOAD_CELL_START_STREAMING", "LOAD_CELL", name,
            self.cmd_LOAD_CELL_START_STREAMING,
            desc=self.cmd_LOAD_CELL_START_STREAMING_help)
        gcode.register_mux_command(
            "LOAD_CELL_STOP_STREAMING", "LOAD_CELL", name,
            self.cmd_LOAD_CELL_STOP_STREAMING,
            desc=self.cmd_LOAD_CELL_STOP_STREAMING_help)

    cmd_LOAD_CELL_TARE_help = "Set the Zero point of the load cell"
    def cmd_LOAD_CELL_TARE(self, gcmd):
        tare_counts = self.load_cell.avg_counts()
        self.load_cell.tare(tare_counts)
        tare_percent = self.load_cell.counts_to_percent(tare_counts)
        gcmd.respond_info("Load cell tare value: %.2f%% (%i)"
                          % (tare_percent, tare_counts))

    cmd_CALIBRATE_LOAD_CELL_help = "Start interactive calibration tool"
    def cmd_LOAD_CELL_CALIBRATE(self, gcmd):
        LoadCellGuidedCalibrationHelper(self.printer, self.load_cell)

    cmd_LOAD_CELL_READ_help = "Take a reading from the load cell"
    def cmd_LOAD_CELL_READ(self, gcmd):
        counts = self.load_cell.avg_counts()
        percent = self.load_cell.counts_to_percent(counts)
        force = self.load_cell.counts_to_grams(counts)
        if percent >= 100 or percent <= -100:
            gcmd.respond_info("Err (%.2f%%)" % (percent,))
        if force is None:
            gcmd.respond_info("---.-g (%.2f%%)" % (percent,))
        else:
            gcmd.respond_info("%.1fg (%.2f%%)" % (force, percent))

    cmd_LOAD_CELL_DIAGNOSTIC_help = "Check the health of the load cell"
    def cmd_LOAD_CELL_DIAGNOSTIC(self, gcmd):
        gcmd.respond_info("Collecting load cell data for 10 seconds...")
        collector = self.load_cell.get_collector()
        reactor = self.printer.get_reactor()
        collector.start_collecting()
        reactor.pause(reactor.monotonic() + 10.)
        samples, errors = collector.stop_collecting()
        if errors:
            gcmd.respond_info("Sensor reported errors: %i errors,"
                              " %i overflows" % (errors[0], errors[1]))
        else:
            gcmd.respond_info("Sensor reported no errors")
        if not samples:
            raise gcmd.error("No samples returned from sensor!")
        counts = select_column(samples, 2)
        range_min, range_max = self.load_cell.saturation_range()
        good_count = 0
        saturation_count = 0
        for sample in counts:
            if sample >= range_max or sample <= range_min:
                saturation_count += 1
            else:
                good_count += 1
        gcmd.respond_info("Samples Collected: %i" % (len(samples)))
        if len(samples) > 2:
            sensor_sps = self.load_cell.sensor.get_samples_per_second()
            sps = float(len(samples)) / (samples[-1][0] - samples[0][0])
            gcmd.respond_info("Measured samples per second: %.1f, "
                              "configured: %.1f" % (sps, sensor_sps))
        gcmd.respond_info("Good samples: %i, Saturated samples: %i, Unique"
                          " values: %i" % (good_count, saturation_count,
                          len(set(counts))))
        max_pct = self.load_cell.counts_to_percent(max(counts))
        min_pct = self.load_cell.counts_to_percent(min(counts))
        gcmd.respond_info("Sample range: [%.2f%% to %.2f%%]"
                          % (min_pct, max_pct))
        gcmd.respond_info("Sample range / sensor capacity: %.5f%%"
                          % ((max_pct - min_pct) / 2.))

    cmd_LOAD_CELL_START_STREAMING_help = "Load cell start streaming!"
    def cmd_LOAD_CELL_START_STREAMING(self, gcmd):
        self.load_cell._start_streaming()
    cmd_LOAD_CELL_STOP_STREAMING_help = "Load cell stop streaming!"
    def cmd_LOAD_CELL_STOP_STREAMING(self, gcmd):
        self.load_cell._finish_streaming()

# Class to guide the user through calibrating a load cell
class LoadCellGuidedCalibrationHelper:
    def __init__(self, printer, load_cell):
        self.printer = printer
        self.gcode = printer.lookup_object('gcode')
        self.load_cell = load_cell
        self._tare_counts = self._counts_per_gram = None
        self.tare_percent = 0.
        self.register_commands()
        self.gcode.respond_info(
            "Starting load cell calibration. \n"
            "1.) Remove all load and run TARE. \n"
            "2.) Apply a known load, run CALIBRATE GRAMS=nnn. \n"
            "Complete calibration with the ACCEPT command.\n"
            "Use the ABORT command to quit.")

    def verify_no_active_calibration(self,):
        try:
            self.gcode.register_command('TARE', 'dummy')
        except self.printer.config_error as e:
            raise self.gcode.error(
                "Already Calibrating a Load Cell. Use ABORT to quit.")
        self.gcode.register_command('TARE', None)

    def register_commands(self):
        self.verify_no_active_calibration()
        register_command = self.gcode.register_command
        register_command("ABORT", self.cmd_ABORT, desc=self.cmd_ABORT_help)
        register_command("ACCEPT", self.cmd_ACCEPT, desc=self.cmd_ACCEPT_help)
        register_command("TARE", self.cmd_TARE, desc=self.cmd_TARE_help)
        register_command("CALIBRATE", self.cmd_CALIBRATE,
                         desc=self.cmd_CALIBRATE_help)

    # convert the delta of counts to a counts/gram metric
    def counts_per_gram(self, grams, cal_counts):
        return float(abs(int(self._tare_counts - cal_counts))) / grams

    # calculate max force that the load cell can register
    # given tare bias, at saturation in kilograms
    def capacity_kg(self, counts_per_gram):
        range_min, range_max = self.load_cell.saturation_range()
        return (int((range_max - abs(self._tare_counts)) / counts_per_gram)
                / 1000.)

    def finalize(self, save_results=False):
        for name in ['ABORT', 'ACCEPT', 'TARE', 'CALIBRATE']:
            self.gcode.register_command(name, None)
        if not save_results:
            self.gcode.respond_info("Load cell calibration aborted")
            return
        if self._counts_per_gram is None or self._tare_counts is None:
            self.gcode.respond_info("Calibration process is incomplete, "
                                    "aborting")
        self.load_cell.set_calibration(
            self._counts_per_gram, self._tare_counts)
        self.gcode.respond_info("Load cell calibration settings:\n\n"
            "counts_per_gram: %.6f\n"
            "reference_tare_counts: %i\n\n"
            "The SAVE_CONFIG command will update the printer config file"
            " with the above and restart the printer."
            % (self._counts_per_gram, self._tare_counts))
        self.load_cell.tare(self._tare_counts)

    cmd_ABORT_help = "Abort load cell calibration tool"
    def cmd_ABORT(self, gcmd):
        self.finalize(False)

    cmd_ACCEPT_help = "Accept calibration results and apply to load cell"
    def cmd_ACCEPT(self, gcmd):
        self.finalize(True)

    cmd_TARE_help = "Tare the load cell"
    def cmd_TARE(self, gcmd):
        self._tare_counts = self.load_cell.avg_counts()
        self._counts_per_gram = None  # require re-calibration on tare
        self.tare_percent = self.load_cell.counts_to_percent(self._tare_counts)
        gcmd.respond_info("Load cell tare value: %.2f%% (%i)"
                          % (self.tare_percent, self._tare_counts))
        if self.tare_percent > 2.:
            gcmd.respond_info(
                "WARNING: tare value is more than 2% away from 0!\n"
                "The load cell's range will be impacted.\n"
                "Check for external force on the load cell.")
        gcmd.respond_info("Now apply a known force to the load cell and enter \
                         the force value with:\n CALIBRATE GRAMS=nnn")

    cmd_CALIBRATE_help = "Enter the load cell value in grams"
    def cmd_CALIBRATE(self, gcmd):
        if self._tare_counts is None:
            gcmd.respond_info("You must use TARE first.")
            return
        grams = gcmd.get_float("GRAMS", minval=50., maxval=25000.)
        cal_counts = self.load_cell.avg_counts()
        cal_percent = self.load_cell.counts_to_percent(cal_counts)
        c_per_g = self.counts_per_gram(grams, cal_counts)
        cap_kg = self.capacity_kg(c_per_g)
        gcmd.respond_info("Calibration value: %.2f%% (%i), Counts/gram: %.5f, \
            Total capacity: +/- %0.2fKg"
                % (cal_percent, cal_counts, c_per_g, cap_kg))
        range_min, range_max = self.load_cell.saturation_range()
        if cal_counts >= range_max or cal_counts <= range_min:
            raise self.printer.command_error(
                "ERROR: Sensor is saturated with too much load!\n"
                "Use less force to calibrate the load cell.")
        if cal_counts == self._tare_counts:
            raise self.printer.command_error(
                "ERROR: Tare and Calibration readings are the same!\n"
                "Check wiring and validate sensor with "\
                "READ_LOAD_CELL command.")
        if (abs(cal_percent - self.tare_percent)) < 1.:
            raise self.printer.command_error(
                "ERROR: Tare and Calibration readings are less than 1% "
                "different!\n"
                "Use more force when calibrating or a higher sensor gain.")
        # only set _counts_per_gram after all errors are raised
        self._counts_per_gram = c_per_g
        if cap_kg < 1.:
            gcmd.respond_info(
                "WARNING: Load cell capacity is less than 1kg!\n"
                "Check wiring and consider using a lower sensor gain.")
        if cap_kg > 25.:
            gcmd.respond_info(
                "WARNING: Load cell capacity is more than 25Kg!\n"
                "Check wiring and consider using a higher sensor gain.")
        gcmd.respond_info("Accept calibration with the ACCEPT command.")


# Utility to collect some samples from the LoadCell for later analysis
# Optionally blocks execution while collecting with reactor.pause()
# can collect a minimum n samples or collect until a specific print_time
# samples returned in [[time],[force],[counts]] arrays for easy processing
RETRY_DELAY = 0.05  # 20Hz
class LoadCellSampleCollector:
    def __init__(self, printer, load_cell):
        self._printer = printer
        self._load_cell = load_cell
        self._reactor = printer.get_reactor()
        self._mcu = load_cell.sensor.get_mcu()
        self.min_time = 0.
        self.max_time = float("inf")
        self.min_count = float("inf")  # In Python 3.5 math.inf is better
        self.is_started = False
        self._samples = []
        self._errors = 0
        self._overflows = 0

    def _on_samples(self, msg):
        if not self.is_started:
            del self._samples[:]
            return False  # already stopped, ignore
        self._errors += msg['errors']
        self._overflows += msg['overflows']
        samples = msg['data']
        for sample in samples:
            time = sample[0]
            if self.min_time <= time <= self.max_time:
                self._samples.append(sample)
            if time > self.max_time:
                self.is_started = False
        if len(self._samples) >= self.min_count:
            self.is_started = False
        return self.is_started

    def _finish_collecting(self):
        self.is_started = False
        self.min_time = 0.
        self.max_time = float("inf")
        self.min_count = float("inf")  # In Python 3.5 math.inf is better
        samples = self._samples
        self._samples = []
        errors = self._errors
        self._errors = 0
        overflows = self._overflows
        self._overflows = 0
        return samples, (errors, overflows) if errors or overflows else 0

    def _collect_until(self, timeout):
        self.start_collecting()
        while self.is_started:
            now = self._reactor.monotonic()
            if self._mcu.estimated_print_time(now) > timeout:
                self._finish_collecting()
                raise self._printer.command_error(
                    "LoadCellSampleCollector timed out! Errors: %i,"
                    " Overflows: %i" % (self._errors, self._overflows))
            self._reactor.pause(now + RETRY_DELAY)
        return self._finish_collecting()

    # start collecting with no automatic end to collection
    def start_collecting(self, min_time=None):
        if self.is_started:
            return
        self.min_time = min_time if min_time is not None else self.min_time
        self.is_started = True
        self._load_cell.add_client(self._on_samples)

    # stop collecting immediately and return results
    def stop_collecting(self):
        return self._finish_collecting()

    # block execution until at least min_count samples are collected
    # will return all samples collected, not just up to min_count
    def collect_min(self, min_count=1):
        self.min_count = min_count
        if len(self._samples) >= min_count:
            return self._finish_collecting()
        print_time = self._mcu.estimated_print_time(self._reactor.monotonic())
        start_time = max(print_time, self.min_time)
        sps = self._load_cell.sensor.get_samples_per_second()
        return self._collect_until(start_time + 1. + (min_count / sps))

    # returns when a sample is collected with a timestamp after print_time
    def collect_until(self, print_time=None):
        self.max_time = print_time
        if len(self._samples) and self._samples[-1][0] >= print_time:
            return self._finish_collecting()
        return self._collect_until(self.max_time + 1.)

# Printer class that controls the load cell
MIN_COUNTS_PER_GRAM = 1.
class LoadCell:
    def __init__(self, config, sensor):
        self.printer = printer = config.get_printer()
        self.config_name = config.get_name()
        self.name = config.get_name().split()[-1]
        self.sensor = sensor   # must implement BulkSensorAdc
        buffer_size = sensor.get_samples_per_second() // 2
        self._force_buffer = collections.deque(maxlen=buffer_size)
        self._force_buffer_raw = collections.deque(maxlen=buffer_size)
        self.reference_tare_counts = config.getint('reference_tare_counts',
                                                   default=None)
        self.tare_counts = self.reference_tare_counts
        self.counts_per_gram = config.getfloat('counts_per_gram',
                                   minval=MIN_COUNTS_PER_GRAM, default=None)
        self.invert = config.getchoice('sensor_orientation',
                        {'normal': 1., 'inverted': -1.}, default="normal")
        LoadCellCommandHelper(config, self)
        # Probe interface
        self.use_probe_command = config.getboolean('use_probe_command', True)
        self.mcu_probe = LoadCellEndstop(config, self)
        if self.use_probe_command:
            self.cmd_helper = probe.ProbeCommandHelper(
                config, self, self.mcu_probe.query_endstop)
            self.probe_offsets = probe.ProbeOffsetsHelper(config)
        self.global_detection = config.getboolean('global_detection', True)
        self.probe_session = probe.ProbeSessionHelper(config, self.mcu_probe)
        # Client support:
        self.clients = ApiClientHelper(printer)
        self._need_stop = True
        header = {"header": ["time", "force (g)", "counts", "tare_counts"]}
        self.clients.add_mux_endpoint("load_cell/dump_force",
                                      "load_cell", self.name, header)
        # startup, when klippy is ready, start capturing data
        printer.register_event_handler("klippy:ready", self._handle_ready)
        probe_object_name = config.get('probe_object', "probe")
        self.printer.add_object(probe_object_name, self)
        
    def _handle_ready(self):
        if self.global_detection:
            self._start_streaming()
        # announce calibration status on ready
        if self.is_calibrated():
            self.printer.send_event("load_cell:calibrate", self)
        if self.is_tared():
            self.printer.send_event("load_cell:tare", self)

    # convert raw counts to grams and broadcast to clients
    def _add_measurement(self, msg):
        if self._need_stop:
            return False
        data = msg.get("data")
        errors = msg.get("errors")
        overflows = msg.get("overflows")
        if data is None:
            return None
        samples = []
        for row in data:
            # [time, grams, counts, tare_counts]
            samples.append([row[0], self.counts_to_grams(row[1]), row[1],
                            self.tare_counts])
        msg = {'data': samples, 'errors': errors, 'overflows': overflows}
        self.clients.send(msg)
        return True
    
    def _start_streaming(self):
        logging.info("load_cell _start_streaming")
        if not self._need_stop:
            return
        self._need_stop = False
        self.sensor.add_client(self._add_measurement)
        self.add_client(self._track_force)
        logging.info("load_cell add_client")
        
    def _finish_streaming(self):
        logging.info("load_cell _stop_streaming")
        self._need_stop = True
        self._force_buffer.clear()
        self._force_buffer_raw.clear()

    # get internal events of force data
    def add_client(self, callback):
        self.clients.add_client(callback)

    def tare(self, tare_counts):
        self.tare_counts = int(tare_counts)
        self.printer.send_event("load_cell:tare", self)

    def set_calibration(self, counts_per_gram, tare_counts):
        if (counts_per_gram is None
                or abs(counts_per_gram) < MIN_COUNTS_PER_GRAM):
            raise self.printer.command_error("Invalid counts per gram value")
        if tare_counts is None:
            raise self.printer.command_error("Missing tare counts")
        self.counts_per_gram = counts_per_gram
        self.reference_tare_counts = int(tare_counts)
        configfile = self.printer.lookup_object('configfile')
        configfile.set(self.config_name, 'counts_per_gram',
                       "%.5f" % (self.counts_per_gram,))
        configfile.set(self.config_name, 'reference_tare_counts',
                       "%i" % (self.reference_tare_counts,))
        self.printer.send_event("load_cell:calibrate", self)

    def counts_to_grams(self, sample):
        if not self.is_calibrated() or not self.is_tared():
            return None
        sample_delta = float(sample - self.tare_counts)
        return self.invert * (sample_delta / self.counts_per_gram)

    # The maximum range of the sensor based on its bit width
    def saturation_range(self):
        return self.sensor.get_range()

    # convert raw counts to a +/- percentage of the sensors range
    def counts_to_percent(self, counts):
        range_min, range_max = self.saturation_range()
        return (float(counts) / float(range_max)) * 100.

    # read 1 second of load cell data and average it
    # performs safety checks for saturation
    def avg_counts(self, num_samples=None):
        # self._start_streaming()
        if num_samples is None:
            num_samples = self.sensor.get_samples_per_second()
        samples, errors = self.get_collector().collect_min(num_samples)
        # self._finish_streaming()
        if errors:
            raise self.printer.command_error(
                "Sensor reported %i errors while sampling"
                    % (errors[0] + errors[1]))
        # check samples for saturated readings
        range_min, range_max = self.saturation_range()
        for sample in samples:
            if sample[2] >= range_max or sample[2] <= range_min:
                raise self.printer.command_error(
                    "Some samples are saturated (+/-100%)")
        return avg(select_column(samples, 2))

    # Provide ongoing force tracking/averaging for status updates
    def _track_force(self, msg):
        if (not len(msg)):
            return True
        samples = msg['data']
        # selectColumn unusable here because Python 2 lacks deque.extend
        if not (self.is_calibrated() and self.is_tared()):
            for sample in samples:
                self._force_buffer.append(sample[1])
                self._force_buffer_raw.append(sample[2])
        else:
            for sample in samples:
                self._force_buffer_raw.append(sample[2])
        return True

    def _force_g(self):
        state = {}
        if (len(self._force_buffer_raw) > 0):
            state.update({
                "force_r": round(avg(self._force_buffer_raw), 1),
                "min_force_r": round(min(self._force_buffer_raw), 1),
                "max_force_r": round(max(self._force_buffer_raw), 1),
            })
        if ((self.is_calibrated()) and (self.is_tared())
                and (len(self._force_buffer) > 0)):
            state.update({
                "force_g": round(avg(self._force_buffer), 1),
                "min_force_g": round(min(self._force_buffer), 1),
                "max_force_g": round(max(self._force_buffer), 1),
            })
        return state
    def get_force_r(self):
        res = 0
        if (len(self._force_buffer_raw) > 0):
            res = round(avg(self._force_buffer_raw), 1)
        return res
    def clear_force_r(self):
        self._force_buffer_raw.clear()

    def is_tared(self):
        return self.tare_counts is not None

    def is_calibrated(self):
        return (self.counts_per_gram is not None
                and self.reference_tare_counts is not None)

    def get_sensor(self):
        return self.sensor

    def get_reference_tare_counts(self):
        return self.reference_tare_counts

    def get_tare_counts(self):
        return self.tare_counts

    def get_counts_per_gram(self):
        return self.counts_per_gram

    def get_collector(self):
        return LoadCellSampleCollector(self.printer, self)
    
    def get_probe_params(self, gcmd=None):
        return self.probe_session.get_probe_params(gcmd)
    
    def get_offsets(self):
        if self.use_probe_command:
            return self.probe_offsets.get_offsets()
        else:
            return [0,0,0]
    
    def start_probe_session(self, gcmd):
        return self.probe_session.start_probe_session(gcmd)

    def get_status(self, eventtime):
        status = {}
        if self.use_probe_command:
            status.update(dict(self.cmd_helper.get_status(eventtime)))
            x_offset, y_offset, z_offset = self.get_offsets()
            status.update({
                'offsets': {'x': x_offset, 'y': y_offset, 'z': z_offset}
            })
        status.update(self._force_g())
        status.update({
            'is_calibrated': self.is_calibrated(),
            'counts_per_gram': self.counts_per_gram,
            'reference_tare_counts': self.reference_tare_counts,
            'tare_counts': self.tare_counts,
        })
        return status

class LoadCellEndstop:
    def __init__(self, config, load_cell, calibration=None):
        self._printer = config.get_printer()
        self.load_cell = load_cell
        self._sensor_helper = load_cell.sensor
        self._mcu = self._sensor_helper.get_mcu()
        # self._calibration = calibration
        self._dispatch = mcu.TriggerDispatch(self._mcu)
        self.reason_endstop_trigger = mcu.MCU_trsync.REASON_ENDSTOP_HIT
        self.reason_endstop_error = mcu.MCU_trsync.REASON_COMMS_TIMEOUT + 1
        self._trigger_time = 0.
        self.trigger_value_down = 0.
        self.trigger_value_up = 0.
        self.update_value_down = 0.
        self.update_value_up = 0.
        self.probing_sample_count = 0
        # self._gather = None
        self.pressure_change_value = config.getint(
            'pressure_change', 3000, minval=0
        )
        self.flexible_pressure_change = config.getint(
            'flexible_pressure_change', self.pressure_change_value/2,
            minval=0
        )
        self.calm_down = config.getfloat(
            'time_for_calm_down', default=1.0, minval=0.1)
        self.press_deformation_offset = config.getfloat(
            'press_deformation_offset', minval=0.
        )
        # self._printer.register_event_handler(
        #     "homing:home_rails_begin",
        #     self._handle_home_rails_begin
        # )
        # self._printer.register_event_handler(
        #     "homing:home_rails_end",
        #     self._handle_home_rails_end
        # )
    def _handle_home_rails_begin(self, homing_state, rails):
        logging.info("handle rails begin")
        # self.load_cell._start_streaming()
    def _handle_home_rails_end(self, homing_state, rails):
        logging.info("handle rails end")
        if 2 in homing_state.get_axes():
            homing_state.set_homed_position(
                [None, None, -self.press_deformation_offset]
            )
        # self.load_cell._finish_streaming()
    # Interface for MCU_endstop
    def get_mcu(self):
        return self._mcu
    def add_stepper(self, stepper):
        logging.info("load_cell_endstop:add_stepper:%s", stepper)
        self._dispatch.add_stepper(stepper)
    def get_steppers(self):
        return self._dispatch.get_steppers()
    def home_start(self, print_time, sample_time, sample_count, rest_time,
                   triggered=True):
        toolhead = self._printer.lookup_object("toolhead")
        self._trigger_time = 0.
        # 获取当前值
        trigger_completion = self._dispatch.start(print_time)
        range_min, range_max = self.load_cell.saturation_range()
        # toolhead.wait_moves()
        if self.probing_sample_count > 0:
            self._sensor_helper.setup_home(
                self._dispatch.get_oid(),
                self.reason_endstop_trigger, self.reason_endstop_error,
                self.trigger_value_down, self.trigger_value_up)
            return trigger_completion
        max_wait_num = 5 # 最少等待5轮
        min_calm_num = 3
        wait_num = 0
        wait_time = self._sensor_helper.get_bulk_update() * 2 # 每轮等待时间
        if wait_time < 0.2:
            # 最少等待5轮, 设定的平静时间越长, 等待越久
            max_wait_num = int(self.calm_down/wait_time)
            min_calm_num = min(max_wait_num/4, int(0.5/wait_time))
        # reactor = self._printer.get_reactor()

        # Reset the data before each detection?
        need_update_data = 1
        methods = 2
        if (methods == 1):
            self.load_cell.clear_force_r() # 清除旧数据的干扰
            for the_i in range(max_wait_num):
                # reactor.pause(reactor.monotonic() + wait_time)
                current_value = int(self.load_cell.get_force_r())
                if (range_min<current_value) and (current_value<range_max):
                    need_update_data = 0
                    break
                toolhead.dwell(wait_time)
            if (current_value <= range_min) or (current_value >= range_max):
                raise self._printer.command_error(
                    "Load_Cell: Invalid data has been detected: %d"
                    % (current_value,))
            trigger_value_down = current_value - self.pressure_change_value
            trigger_value_up = current_value + self.pressure_change_value
        elif (methods == 2):
            for the_i in range(max_wait_num):
                current_value = int(self.load_cell.get_force_r())
                if (range_min>=current_value) or (range_max<=current_value):
                    raise self._printer.command_error(
                        "Load_Cell: Out of range: %d"
                        % (current_value,))
                if ((self.trigger_value_down <= range_min) or
                    (self.trigger_value_up >= range_max) or
                    (self.update_value_down <= range_min) or
                    (self.update_value_up >= range_max)):
                    self.trigger_value_down = (current_value -
                                               self.pressure_change_value)
                    self.trigger_value_up = (current_value +
                                             self.pressure_change_value)
                    self.update_value_down = (current_value -
                                              self.flexible_pressure_change)
                    self.update_value_up = (current_value +
                                            self.flexible_pressure_change)
                if (wait_num >= min_calm_num):
                    need_update_data = 0
                    break
                elif (wait_num):
                    if ((self.update_value_down < current_value) and
                        (current_value < self.update_value_up)):
                        wait_num += 1
                    else:
                        wait_num = 0
                else:
                    wait_num = 1
                toolhead.dwell(wait_time)
            if (need_update_data):
                if ((current_value <= self.trigger_value_down) or
                    (current_value >= self.trigger_value_up)):
                    raise self._printer.command_error(
                        "Load_Cell: Invalid data has been detected: %d(%d~%d)"
                        " min_calm_num:%d"
                        % (current_value, self.trigger_value_down,
                        self.trigger_value_up, min_calm_num))
                self.trigger_value_down = (current_value -
                                           self.pressure_change_value)
                self.trigger_value_up = (current_value +
                                         self.pressure_change_value)
                self.update_value_down = (current_value -
                                          self.flexible_pressure_change)
                self.update_value_up = (current_value +
                                        self.flexible_pressure_change)
                logging.info("update pressure change: %d" %
                             (self.flexible_pressure_change))
            trigger_value_down = self.trigger_value_down
            trigger_value_up = self.trigger_value_up
        else:
            for the_i in range(max_wait_num):
                # reactor.pause(reactor.monotonic() + wait_time)
                current_value = int(self.load_cell.get_force_r())
                if ((self.trigger_value_down < current_value) and
                    (current_value < self.trigger_value_up)):
                    need_update_data = 0
                    break
                toolhead.dwell(wait_time)
            if ((current_value <= self.trigger_value_down) or
                (current_value >= self.trigger_value_up)):
                raise self._printer.command_error(
                    "Load_Cell: Invalid data has been detected: %d(%d~%d)"
                    % (current_value, self.trigger_value_down,
                    self.trigger_value_up))
            trigger_value_down = self.trigger_value_down
            trigger_value_up = self.trigger_value_up

        logging.info("home_start: cur:%d, wt:%.2f" %
            (current_value, wait_time*(the_i+1)))
        self._sensor_helper.setup_home(
            self._dispatch.get_oid(),
            self.reason_endstop_trigger, self.reason_endstop_error,
            trigger_value_down, trigger_value_up)
        # toolhead.dwell(wait_time)
        # self._sensor_helper.setup_home(
        #     self._dispatch.get_oid(),
        #     self.reason_endstop_trigger, self.reason_endstop_error,
        #     self.pressure_change_value)
        return trigger_completion
    def home_wait(self, home_end_time):
        self._dispatch.wait_end(home_end_time)
        res = self._dispatch.stop()
        trigger_time = self._sensor_helper.clear_home()
        if res >= mcu.MCU_trsync.REASON_COMMS_TIMEOUT:
            if res == mcu.MCU_trsync.REASON_COMMS_TIMEOUT:
                raise self._printer.command_error(
                    "Communication timeout during homing")
            raise self._printer.command_error("Eddy current sensor error")
        if res != self.reason_endstop_trigger:
            return 0.
        if self._mcu.is_fileoutput():
            return home_end_time
        self._trigger_time = trigger_time
        return trigger_time
        # return home_end_time

    def query_endstop(self, print_time):
        return False
    # Interface for ProbeEndstopWrapper
    def probing_move(self, pos, speed):
        # Perform probing move
        self.probing_sample_count = 0
        phoming = self._printer.lookup_object('homing')
        epos = phoming.probing_move(self, pos, speed)
        # Eliminate deformation caused by pressure
        epos[2] += self.press_deformation_offset
        return epos
        # trig_pos = phoming.probing_move(self, pos, speed)
        # if not self._trigger_time:
        #     return trig_pos
        # Extract samples
        # start_time = self._trigger_time + 0.050
        # end_time = start_time + 0.100
        # toolhead = self._printer.lookup_object("toolhead")
        # toolhead_pos = toolhead.get_position()
        # self._gather.note_probe(start_time, end_time, toolhead_pos)
        # return self._gather.pull_probed()[0]
    def probing_move_2(self, pos, speed, count):
        # Perform probing move
        self.probing_sample_count = count
        phoming = self._printer.lookup_object('homing')
        epos = phoming.probing_move(self, pos, speed)
        # Eliminate deformation caused by pressure
        epos[2] += self.press_deformation_offset
        return epos
    def multi_probe_begin(self):
        # self.load_cell._start_streaming()
        toolhead = self._printer.lookup_object("toolhead")
        self._trigger_time = 0.
        # 获取当前值
        range_min, range_max = self.load_cell.saturation_range()
        run_error = 0
        max_wait_num = 5
        min_calm_num = 3
        wait_num = 0
        wait_time = self._sensor_helper.get_bulk_update() * 2
        if wait_time < 0.2:
            max_wait_num = int(self.calm_down/wait_time)
            # min number of clam down, max wait time is 0.5s
            min_calm_num = min(max_wait_num/4, int(0.5/wait_time))
        # reactor = self._printer.get_reactor()
        toolhead.wait_moves()
        self.load_cell.clear_force_r() # 清除旧数据的干扰
        toolhead.dwell(0.5)
        for the_i in range(max_wait_num):
            current_value = int(self.load_cell.get_force_r())
            # if (range_min < current_value) and (current_value < range_max):
            #     break
            if (wait_num >= min_calm_num):
                # 数据稳定, 提前退出
                break
            elif (wait_num):
                # 比较数据
                if ((self.update_value_down < current_value) and
                    (current_value < self.update_value_up)):
                    wait_num += 1
                else:
                    # 重置记录
                    # self.update_value_down = (current_value -
                    #                           self.flexible_pressure_change)
                    # self.update_value_up = (current_value +
                    #                         self.flexible_pressure_change)
                    # wait_num = 1

                    # 重新记录
                    wait_num = 0
            else:
                # 记录数据
                self.update_value_down = (current_value -
                                          self.flexible_pressure_change)
                self.update_value_up = (current_value +
                                        self.flexible_pressure_change)
                wait_num = 1
            toolhead.dwell(wait_time)
        if (wait_num < min_calm_num):
            raise self._printer.command_error(
                "Load_Cell: The sensor can't calm down!"
            )

        if (current_value <= range_min) or (current_value >= range_max):
            raise self._printer.command_error(
                "Load_Cell: Invalid data has been detected: %d"
                % (current_value,))
        self.trigger_value_down = (current_value -
                                   self.pressure_change_value)
        self.trigger_value_up = (current_value +
                                 self.pressure_change_value)
        self.update_value_down = (current_value -
                                  self.flexible_pressure_change)
        self.update_value_up = (current_value +
                                self.flexible_pressure_change)
        logging.info("multi_pb: cur:%d, wt:%.2f, wait_num:%d",
                     current_value, wait_time*(the_i+1), wait_num)
        return 0
    def multi_probe_end(self):
        # self.load_cell._finish_streaming()
        logging.info("multi_pe")
        return 0
    def probe_prepare(self, hmove):
        pass
    def probe_finish(self, hmove):
        pass
    def get_position_endstop(self):
        return -self.press_deformation_offset

def load_config(config):
    # Sensor types
    sensors = {}
    # sensors.update(hx71x.HX71X_SENSOR_TYPES)
    # sensors.update(ads1220.ADS1220_SENSOR_TYPE)
    sensors.update(cs1237.CS1237_SENSOR_TYPES)
    sensor_class = config.getchoice('sensor_type', sensors)
    return LoadCell(config, sensor_class(config))

def load_config_prefix(config):
    return load_config(config)
