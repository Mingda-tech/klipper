# This is used to control the MINGDA distance sensor

import threading
import logging
import chelper
import pins
import math
import time
import queue
import json
import struct
import numpy as np
import copy
from numpy.polynomial import Polynomial
from . import manual_probe
from . import probe
from . import bed_mesh
from . import thermistor
from . import adc_temperature
from mcu import MCU, MCU_trsync
from clocksync import SecondarySync

STREAM_BUFFER_LIMIT_DEFAULT = 100
STREAM_TIMEOUT = 2.0
probe_object_name = "probe"

class MD_Dist_Probe:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name()

        self.speed = config.getfloat(
            "speed", 5.0, above=0.0
        )
        self.lift_speed = config.getfloat(
            "lift_speed", self.speed, above=0.0
        )
        self.backlash_comp = config.getfloat(
            "backlash_comp", 0.5
        )
        self.x_offset = config.getfloat(
            "x_offset", 0.0
        )
        self.y_offset = config.getfloat(
            "y_offset", 0.0
        )
        self.trigger_distance = config.getfloat(
            "trigger_distance", 2.0
        )
        self.trigger_dive_threshold = config.getfloat(
            "trigger_dive_threshold", 1.0
        )
        self.trigger_hysteresis = config.getfloat(
            "trigger_hysteresis", 0.006
        )
        self.z_settling_time = config.getint(
            "z_settling_time", 5, minval=0
        )

        # If using paper for calibration, this would be .1mm
        self.cal_nozzle_z = config.getfloat(
            "cal_nozzle_z", 0.1
        )
        self.cal_floor = config.getfloat(
            "cal_floor", 0.2
        )
        self.cal_ceil = config.getfloat(
            "cal_ceil", 5.0
        )
        self.cal_speed = config.getfloat(
            "cal_speed", 1.0
        )
        self.cal_move_speed = config.getfloat(
            "cal_move_speed", 10.0
        )

        self.end_park_x, self.end_park_y = config.getfloatlist(
            "end_park_pos", [None, None], count=2
        )
        self.first_move_axis = config.getchoice(
            "first_move_axis", {'x':'x', 'X':'x', 'y':'y', 'Y':'y'}, 'y'
        )
        self.enable_temp_compensate = config.getboolean(
            "enable_temp_compensate", True
        )
        self.z_virtual_endstop = config.getboolean(
            "z_virtual_endstop", True
        )

        # Load models
        self.model = None
        self.models = {}
        self.temp_t1 = config.getfloat(
            "temperature1", 25.0, minval=thermistor.KELVIN_TO_CELSIUS)
        self.temp_r1 = config.getfloat(
            "resistance1", 47000.0, minval=0.)
        self.temp_beta = config.getfloat(
            "beta", 4041.0, above=0.)
        self.temp_pullup = config.getfloat(
            'pullup_resistor', 10000.0, above=0.)
        self.temp_inline_resistor = config.getfloat(
            'inline_resistor', 0., minval=0.)
        self.model_temp_builder = MD_Dist_Temp_Model_Builder.load(config)
        self.model_temp = None
        self.fmin = None
        self.default_model_name = config.get(
            "default_model_name",
            "default"
        )
        self.model_manager = Model_Manager(self)

        # Temperature sensor integration
        self.last_temp = 0
        self.measured_min = 99999999.0
        self.measured_max = 0.0

        self.last_sample = None
        self.hardware_failure = None

        self.mesh_helper = MD_Dist_Mesh_Helper.create(self, config)

        self._stream_en = 0
        self._stream_timeout_timer = self.reactor.register_timer(
            self._stream_timeout
        )
        self._stream_callbacks = {}
        self._stream_latency_requests = {}
        self._stream_buffer = []
        self._stream_buffer_limit = STREAM_BUFFER_LIMIT_DEFAULT
        self._stream_buffer_limit_new = self._stream_buffer_limit
        self._stream_samples_queue = queue.Queue()
        self._stream_flush_event = threading.Event()
        self._log_stream = None
        self._data_filter = Alpha_Beta_Filter(
            config.getfloat("filter_alpha", 0.5),
            config.getfloat("filter_beta", 0.000001),
        )
        self.trapq = None

        # Chip options
        dout_pin_name = config.get('dout_pin', None)
        sclk_pin_name = config.get('sclk_pin', None)
        if (dout_pin_name is not None) and (sclk_pin_name is not None):
            ppins = self.printer.lookup_object('pins')
            self.addr = config.getint('addr', 0x2A)
            temp_pin_name = config.get('temp_pin')
            temp_ppin = ppins.lookup_pin(temp_pin_name)
            dout_ppin = ppins.lookup_pin(dout_pin_name)
            sclk_ppin = ppins.lookup_pin(sclk_pin_name)
            self._mcu = mcu = dout_ppin['chip']
            # self.oid = mcu.create_oid()
            if ((sclk_ppin['chip'] is not mcu) or
                (temp_ppin['chip'] is not mcu)):
                raise config.error("%s config error: All pins must be "
                                "connected to the same MCU" % (self.name,))
            self.dout_pin = dout_ppin['pin']
            self.sclk_pin = sclk_ppin['pin']
            self.temp_pin = temp_ppin['pin']
            soft_i2c = config.getint('use_software_i2c', 0)
            logging.info(
            "md_dist_config dout_pin=%s sclk_pin=%s temp_pin=%s addr=%s"
            " soft_i2c=%s"
            % (self.dout_pin, self.sclk_pin, self.temp_pin, self.addr,
               soft_i2c,))
            mcu.add_config_cmd(
            "md_dist_config dout_pin=%s sclk_pin=%s temp_pin=%s addr=%s"
            " soft_i2c=%s"
            % (self.dout_pin, self.sclk_pin, self.temp_pin, self.addr,
               soft_i2c,))
        else:
            mainsync = self.printer.lookup_object("mcu")._clocksync
            self._mcu = MCU(config, SecondarySync(self.reactor, mainsync))
            self.printer.add_object("mcu " + self.name, self._mcu)
        self.cmd_queue = self._mcu.alloc_command_queue()
        self.mcu_probe = MD_Dist_Endstop_Wrapper(self)

        global probe_object_name
        probe_object_name = config.get('probe_object', "probe")
        # Register z_virtual_endstop
        if self.z_virtual_endstop:
            self.printer.lookup_object("pins").register_chip(
                probe_object_name, self
            )
        # Register event handlers
        self.printer.register_event_handler(
            "klippy:connect",
            self._handle_connect
        )
        self.printer.register_event_handler(
            "klippy:mcu_identify",
            self._handle_mcu_identify
        )
        self._mcu.register_config_callback(self._build_config)
        self._mcu.register_response(
            self._handle_md_dist_data,
            "md_dist_data"
        )
        # Register webhooks
        webhooks = self.printer.lookup_object("webhooks")
        self._api_dump_helper = API_Dump_Helper(self)
        webhooks.register_endpoint("md_dist/status", self._handle_req_status)
        webhooks.register_endpoint("md_dist/dump", self._handle_req_dump)
        # Register gcode commands
        self.gcode = self.printer.lookup_object("gcode")
        self.gcode.register_command(
            "MD_DIST_STREAM",
            self.cmd_MD_DIST_STREAM,
            desc=self.cmd_MD_DIST_STREAM_help
        )
        self.gcode.register_command(
            "MD_DIST_QUERY",
            self.cmd_MD_DIST_QUERY,
            desc=self.cmd_MD_DIST_QUERY_help
        )
        self.gcode.register_command(
            "MD_DIST_CALIBRATE",
            self.cmd_MD_DIST_CALIBRATE,
            desc=self.cmd_MD_DIST_CALIBRATE_help
        )
        self.gcode.register_command(
            "MD_DIST_ESTIMATE_BACKLASH",
            self.cmd_MD_DIST_ESTIMATE_BACKLASH,
            desc=self.cmd_MD_DIST_ESTIMATE_BACKLASH_help
        )
        if self.z_virtual_endstop:
            self.gcode.register_command(
                "PROBE",
                self.cmd_PROBE,
                desc=self.cmd_PROBE_help
            )
            self.gcode.register_command(
                "PROBE_ACCURACY",
                self.cmd_PROBE_ACCURACY,
                desc=self.cmd_PROBE_ACCURACY_help
            )
            self.gcode.register_command(
                "Z_OFFSET_APPLY_PROBE",
                self.cmd_Z_OFFSET_APPLY_PROBE,
                desc=self.cmd_Z_OFFSET_APPLY_PROBE_help
            )
        else:
            # If z_virtual_endstop is not used,
            # the z_offset_apply of md_dist needs to be preserved
            self.gcode.register_command(
                "Z_OFFSET_APPLY_MD_DIST",
                self.cmd_Z_OFFSET_APPLY_PROBE,
                desc=self.cmd_Z_OFFSET_APPLY_PROBE_help
            )

    # Event handlers
    def _handle_connect(self):
        self.phoming = self.printer.lookup_object("homing")

        # Ensure streaming mode is stopped
        self.md_dist_stream_cmd.send([0])

        self.model_temp = self.model_temp_builder.build_with_base(self)
        if self.model_temp:
            self.fmin = self.model_temp.fmin
        self.model = self.models.get(self.default_model_name, None)
        if self.model:
            self._apply_threshold()

    def _handle_mcu_identify(self):
        constants = self._mcu.get_constants()

        if self._mcu._mcu_freq < 20000000:
            self.sensor_freq =  self._mcu._mcu_freq
        elif self._mcu._mcu_freq < 100000000:
            self.sensor_freq = self._mcu._mcu_freq/2
        else:
            self.sensor_freq = self._mcu._mcu_freq/6

        self.inv_adc_max = 1.0 / constants.get("ADC_MAX")
        self.temp_smooth_count = constants.get("MD_DIST_ADC_SMOOTH_COUNT")
        self.thermistor = thermistor.Thermistor(
            self.temp_pullup, self.temp_inline_resistor)
        self.thermistor.setup_coefficients_beta(
            self.temp_t1, self.temp_r1, self.temp_beta)

        self.toolhead = self.printer.lookup_object("toolhead")
        self.trapq = self.toolhead.get_trapq()

    def _build_config(self):
        self.md_dist_stream_cmd = self._mcu.lookup_command(
            "md_dist_stream en=%u",
            cq=self.cmd_queue
        )
        self.md_dist_set_threshold_cmd = self._mcu.lookup_command(
            "md_dist_set_threshold trigger=%u untrigger=%u",
            cq=self.cmd_queue
        )
        self.md_dist_home_cmd = self._mcu.lookup_command(
            "md_dist_home trsync_oid=%c trigger_reason=%c trigger_invert=%c",
            cq=self.cmd_queue
        )
        self.md_dist_stop_home_cmd = self._mcu.lookup_command(
            "md_dist_stop_home",
            cq=self.cmd_queue
        )
        self.md_dist_base_read_cmd = self._mcu.lookup_query_command(
            "md_dist_base_read len=%c offset=%hu",
            "md_dist_base_data bytes=%*s offset=%hu",
            cq=self.cmd_queue
        )

    def stats(self, eventtime):
        return False, "%s: coil_temp=%.1f" % (self.name, self.last_temp)

    # Virtual endstop
    def setup_pin(self, pin_type, pin_params):
        if pin_type != "endstop" or pin_params["pin"] != "z_virtual_endstop":
            raise pins.error(
                "Probe virtual endstop only useful as endstop pin"
            )
        if pin_params["invert"] or pin_params["pullup"]:
            raise pins.error(
                "Can not pullup/invert probe virtual endstop"
            )
        return self.mcu_probe

    # Probe interface
    def multi_probe_begin(self):
        self._start_streaming()

    def multi_probe_end(self):
        self._stop_streaming()

    def get_offsets(self):
        return self.x_offset, self.y_offset, self.trigger_distance

    def get_lift_speed(self, gcmd=None):
        if gcmd is not None:
            return gcmd.get_float("LIFT_SPEED", self.lift_speed, above=0.)
        return self.lift_speed

    def run_probe(self, gcmd):
        if self.model is None:
            raise self.printer.command_error("No md_dist model loaded")

        speed = gcmd.get_float("PROBE_SPEED", self.speed, above=0.0)
        allow_faulty = gcmd.get_int("ALLOW_FAULTY_COORDINATE", 0) != 0
        lift_speed = self.get_lift_speed(gcmd)
        toolhead = self.printer.lookup_object("toolhead")
        curtime = self.reactor.monotonic()
        if 'z' not in toolhead.get_status(curtime)["homed_axes"]:
            raise self.printer.command_error("Must home before probe")

        self._start_streaming()
        try:
            return self._probe(speed, allow_faulty=allow_faulty)
        finally:
            self._stop_streaming()

    def _move_to_probing_height(self, speed):
        target = self.trigger_distance
        top = target + self.backlash_comp
        cur_z = self.toolhead.get_position()[2]
        if cur_z < top:
            self.toolhead.manual_move([None, None, top], speed)
        self.toolhead.manual_move([None, None, target], speed)
        self.toolhead.wait_moves()

    def _probing_move_to_probing_height(self, speed):
        curtime = self.reactor.monotonic()
        status = self.toolhead.get_kinematics().get_status(curtime)
        pos = self.toolhead.get_position()
        pos[2] = status["axis_minimum"][2]
        try:
            self.phoming.probing_move(self.mcu_probe, pos, speed)
            self._sample_printtime_sync(self.z_settling_time)
        except self.printer.command_error as e:
            reason = str(e)
            if "Timeout during probing move" in reason:
                reason += probe.HINT_TIMEOUT
            raise self.printer.command_error(reason)

    def _probe(self, speed, num_samples=10, allow_faulty=False):
        target = self.trigger_distance
        tdt = self.trigger_dive_threshold
        (dist, samples) = self._sample(5, num_samples)

        x, y = samples[0]["pos"][0:2]
        if self._is_faulty_coordinate(x, y, True):
            msg = "Probing within a faulty area"
            if not allow_faulty:
                raise self.printer.command_error(msg)
            else:
                self.gcode.respond_raw("!! " + msg + "\n")

        if dist > target + tdt:
            # If we are above the dive threshold right now, we'll need to
            # do probing move and then re-measure
            self._probing_move_to_probing_height(speed)
            (dist, samples) = self._sample(self.z_settling_time, num_samples)
        elif math.isinf(dist) and dist < 0:
            # We were below the valid range of the model
            msg = """Attempted to probe with
                  MD_DIST below calibrated model range"""
            raise self.printer.command_error(msg)
        elif self.toolhead.get_position()[2] < target - tdt:
            # We are below the probing target height, we'll move to the
            # correct height and take a new sample.
            self._move_to_probing_height(speed)
            (dist, samples) = self._sample(self.z_settling_time, num_samples)

        pos = samples[0]["pos"]

        self.gcode.respond_info(
            "probe at %.3f,%.3f,%.3f is z=%.6f"
            % (pos[0], pos[1], pos[2], dist)
        )

        return [pos[0], pos[1], pos[2] + target - dist]

    # Calibration routines
    def _start_calibration(self, gcmd):
        if self.z_virtual_endstop:
            allow_faulty = gcmd.get_int("ALLOW_FAULTY_COORDINATE", 0) != 0
            if gcmd.get("SKIP_MANUAL_PROBE", None) is not None:
                kin = self.toolhead.get_kinematics()
                kin_spos = {
                    s.get_name(): s.get_commanded_position() \
                        for s in kin.get_steppers()
                }
                kin_pos = kin.calc_position(kin_spos)
                if self._is_faulty_coordinate(kin_pos[0], kin_pos[1]):
                    msg = "Calibrating within a faulty area"
                    if not allow_faulty:
                        raise gcmd.error(msg)
                    else:
                        gcmd.respond_raw("!! " + msg + "\n")
                self._calibrate(gcmd, kin_pos, False)
            else:
                curtime = self.printer.get_reactor().monotonic()
                kin_status = self.toolhead.get_status(curtime)
                if "xy" not in kin_status["homed_axes"]:
                    raise self.printer.command_error(
                        "Must home X and Y before calibration"
                    )

                kin_pos = self.toolhead.get_position()
                if self._is_faulty_coordinate(kin_pos[0], kin_pos[1]):
                    msg = "Calibrating within a faulty area"
                    if not allow_faulty:
                        raise gcmd.error(msg)
                    else:
                        gcmd.respond_raw("!! " + msg + "\n")

                forced_z = False
                if 'z' not in kin_status["homed_axes"]:
                    self.toolhead.get_last_move_time()
                    pos = self.toolhead.get_position()
                    pos[2] = kin_status["axis_maximum"][2] - 1.0
                    self.toolhead.set_position(pos, homing_axes=[2])
                    forced_z = True

                cb = lambda kin_pos: self._calibrate(gcmd, kin_pos, forced_z)
                manual_probe.ManualProbeHelper(self.printer, gcmd, cb)
        else:
            curtime = self.printer.get_reactor().monotonic()
            kin_status = self.toolhead.get_status(curtime)
            if "xyz" not in kin_status["homed_axes"]:
                raise self.printer.command_error(
                    "Must home before calibration"
                )
            kin_pos = self.toolhead.get_position()
            if self._is_faulty_coordinate(kin_pos[0], kin_pos[1]):
                msg = "Calibrating within a faulty area"
                if not allow_faulty:
                    raise gcmd.error(msg)
                else:
                    gcmd.respond_raw("!! " + msg + "\n")
            forced_z = False
            # kin_pos[2] = self.cal_nozzle_z
            self._calibrate(gcmd, kin_pos, forced_z)

    def _calibrate(self, gcmd, kin_pos, forced_z):
        if kin_pos is None:
            if forced_z:
                kin = self.toolhead.get_kinematics()
                if hasattr(kin, "note_z_not_homed"):
                    kin.note_z_not_homed()
            return

        gcmd.respond_info("MD_DIST calibration starting")
        cal_nozzle_z = gcmd.get_float("NOZZLE_Z", self.cal_nozzle_z)
        cal_floor = gcmd.get_float("FLOOR", self.cal_floor)
        cal_ceil = gcmd.get_float("CEIL", self.cal_ceil)
        cal_min_z = kin_pos[2] - cal_nozzle_z + cal_floor
        cal_max_z = kin_pos[2] - cal_nozzle_z + cal_ceil
        cal_speed = gcmd.get_float("SPEED", self.cal_speed)
        move_speed = gcmd.get_float("MOVE_SPEED", self.cal_move_speed)

        toolhead = self.toolhead
        curtime = self.reactor.monotonic()
        toolhead.wait_moves()
        pos = toolhead.get_position()

        # Move over to probe coordinate and pull out backlash
        curpos = self.toolhead.get_position()

        curpos[2] = cal_max_z + self.backlash_comp
        toolhead.manual_move(curpos, move_speed) # Up
        curpos[0] -= self.x_offset
        curpos[1] -= self.y_offset
        toolhead.manual_move(curpos, move_speed) # Over
        curpos[2] = cal_max_z
        toolhead.manual_move(curpos, move_speed) # Down
        toolhead.wait_moves()

        samples = []
        def cb(sample):
            samples.append(sample)

        try:
            self._start_streaming()
            self._sample_printtime_sync(50)
            with self.streaming_session(cb) as ss:
                self._sample_printtime_sync(50)
                toolhead.dwell(0.250)
                curpos[2] = cal_min_z
                toolhead.manual_move(curpos, cal_speed)
                toolhead.dwell(0.250)
                self._sample_printtime_sync(50)
        finally:
            self._stop_streaming()

        # Fit the sampled data
        z_offset = [s["pos"][2]-cal_min_z+cal_floor for s in samples]
        freq = [s["freq"] for s in samples]
        temp = [s["temp"] for s in samples]
        inv_freq = [1/f for f in freq]
        poly = Polynomial.fit(inv_freq, z_offset, 9)
        temp_median = median(temp)
        self.model = MD_Dist_Model(
            "default", self, poly, temp_median, min(z_offset), max(z_offset)
        )
        self.models[self.model.name] = self.model
        self.model.save(self)
        self._apply_threshold()

        self.toolhead.get_last_move_time()
        pos = self.toolhead.get_position()
        pos[2] = cal_floor
        self.toolhead.set_position(pos)

        # Dump calibration curve
        fn = "/tmp/md_dist_calibrate-"+time.strftime("%Y%m%d_%H%M%S")+".csv"
        f = open(fn, "w")
        f.write("freq,z,temp\n")
        for i in range(len(freq)):
            f.write("%.5f,%.5f,%.3f\n" % (freq[i], z_offset[i], temp[i]))
        f.close()

        gcmd.respond_info(
            "MD_DIST calibrated at %.3f,%.3f from "
            "%.3f to %.3f, speed %.2f mm/s, temp %.2fC"
            % (pos[0], pos[1], cal_min_z, cal_max_z, cal_speed, temp_median)
        )

    # Internal
    def _update_thresholds(self, moving_up=False):
        self.trigger_freq = self.dist_to_freq(
            self.trigger_distance,
            self.last_temp
        )
        self.untrigger_freq = self.trigger_freq * (1-self.trigger_hysteresis)

    def _apply_threshold(self, moving_up=False):
        self._update_thresholds()
        trigger_c = int(self.freq_to_count(self.trigger_freq))
        untrigger_c = int(self.freq_to_count(self.untrigger_freq))
        self.md_dist_set_threshold_cmd.send([trigger_c, untrigger_c])

    def _register_model(self, name, model):
        if name in self.models:
            raise self.printer.config_error(
                "Multiple md_dist models with same name '%s'" % (name,)
            )
        self.models[name] = model

    def _is_faulty_coordinate(self, x, y, add_offsets=False):
        if not self.mesh_helper:
            return False
        return self.mesh_helper._is_faulty_coordinate(x, y, add_offsets)

    # Streaming mode
    def _check_hardware(self, sample):
        if not self.hardware_failure:
            msg = None
            if sample["data"] == 0xFFFFFFF:
                msg = "coil is shorted or not connected"
            elif self.fmin is not None and sample["freq"] > 1.35*self.fmin:
                msg = "coil expected max frequency exceeded"
            if msg:
                msg = "MD_DIST hardware issue: " + msg
                self.hardware_failure = msg
                logging.error(msg)
                if self._stream_en:
                    self.printer.invoke_shutdown(msg)
                else:
                    self.gcode.respond_raw("!! " + msg + "\n")
        elif self._stream_en:
            self.printer.invoke_shutdown(self.hardware_failure)

    def _enrich_sample_time(self, sample):
        clock = sample["clock"] = self._mcu.clock32_to_clock64(
            sample["clock"]
        )
        sample["time"] = self._mcu.clock_to_print_time(clock)

    def _enrich_sample_temp(self, sample):
        temp_adc = sample["temp"] / self.temp_smooth_count * self.inv_adc_max
        sample["temp"] = self.thermistor.calc_temp(temp_adc)

    def _enrich_sample_freq(self, sample):
        sample["data_smooth"] = self._data_filter.value()
        sample["freq"] = self.count_to_freq(sample["data_smooth"])
        self._check_hardware(sample)

    def _enrich_sample(self, sample):
        sample["dist"] = self.freq_to_dist(sample["freq"], sample["temp"])
        pos, vel = self._get_trapq_position(sample["time"])

        if pos is None:
            return
        sample["pos"] = pos
        sample["vel"] = vel

    def _start_streaming(self):
        if self._stream_en == 0:
            self.md_dist_stream_cmd.send([1])
            curtime = self.reactor.monotonic()
            self.reactor.update_timer(
                self._stream_timeout_timer,
                curtime + STREAM_TIMEOUT
            )
        self._stream_en += 1
        self._data_filter.reset()
        self._stream_flush()
    def _stop_streaming(self):
        self._stream_en -= 1
        if self._stream_en == 0:
            self.reactor.update_timer(
                self._stream_timeout_timer,
                self.reactor.NEVER
            )
            self.md_dist_stream_cmd.send([0])
        self._stream_flush()

    def _stream_timeout(self, eventtime):
        if not self._stream_en:
            return self.reactor.NEVER
        msg = "MD_DIST sensor not receiving data"
        logging.error(msg)
        self.printer.invoke_shutdown(msg)
        return self.reactor.NEVER

    def request_stream_latency(self, latency):
        next_key = 0
        if self._stream_latency_requests:
            next_key = max(self._stream_latency_requests.keys()) + 1
        new_limit = STREAM_BUFFER_LIMIT_DEFAULT
        self._stream_latency_requests[next_key] = latency
        min_requested = min(self._stream_latency_requests.values())
        if min_requested < new_limit:
            new_limit = min_requested
        if new_limit < 1:
            new_limit = 1
        self._stream_buffer_limit_new = new_limit
        return next_key

    def drop_stream_latency_request(self, key):
        self._stream_latency_requests.pop(key, None)
        new_limit = STREAM_BUFFER_LIMIT_DEFAULT
        if self._stream_latency_requests:
            min_requested = min(self._stream_latency_requests.values())
            if min_requested < new_limit:
                new_limit = min_requested
        if new_limit < 1:
            new_limit = 1
        self._stream_buffer_limit_new = new_limit

    def streaming_session(self, callback,
                          completion_callback=None,
                          latency=None):
        return Streaming_Helper(self, callback, completion_callback, latency)

    def _stream_flush(self):
        self._stream_flush_event.clear()
        while True:
            try:
                samples = self._stream_samples_queue.get_nowait()
                updated_timer = False
                for sample in samples:
                    if not updated_timer:
                        curtime = self.reactor.monotonic()
                        self.reactor.update_timer(
                            self._stream_timeout_timer,
                            curtime + STREAM_TIMEOUT
                        )
                        updated_timer = True

                    self._enrich_sample_temp(sample)
                    temp = sample["temp"]
                    if self.model_temp is not None and not (-40 < temp < 180):
                        msg = (
                            "MD_DIST temperature sensor faulty(read %.2f C),"
                            " disabling temperaure compensation" % (temp,)
                        )
                        logging.error(msg)
                        self.gcode.respond_raw("!! " + msg + "\n")
                        self.model_temp = None

                    self.last_temp = temp
                    if temp:
                        self.measured_min = min(self.measured_min, temp)
                        self.measured_max = max(self.measured_max, temp)

                    self._enrich_sample_time(sample)
                    self._data_filter.update(sample["time"], sample["data"])
                    self._enrich_sample_freq(sample)

                    if len(self._stream_callbacks) > 0:
                        self._enrich_sample(sample)
                        for cb in list(self._stream_callbacks.values()):
                            cb(sample)
            except queue.Empty:
                return

    def _stream_flush_schedule(self):
        # When streaming is disabled, let all through
        force = self._stream_en == 0
        if self._stream_buffer_limit_new != self._stream_buffer_limit:
            force = True
            self._stream_buffer_limit = self._stream_buffer_limit_new
        if not force and len(self._stream_buffer) < self._stream_buffer_limit:
            return
        self._stream_samples_queue.put_nowait(self._stream_buffer)
        self._stream_buffer = []
        if self._stream_flush_event.is_set():
            return
        self._stream_flush_event.set()
        self.reactor.register_async_callback(lambda e: self._stream_flush())

    def _handle_md_dist_data(self, params):
        if self.trapq is None:
            return

        self._stream_buffer.append(params.copy())
        self._stream_flush_schedule()

    def _get_trapq_position(self, print_time):
        ffi_main, ffi_lib = chelper.get_ffi()
        data = ffi_main.new("struct pull_move[1]")
        count = ffi_lib.trapq_extract_old(
            self.trapq, data, 1, 0.0, print_time
        )
        if not count:
            return None, None
        move = data[0]
        move_time = max(0.0, min(move.move_t, print_time - move.print_time))
        dist = (move.start_v + 0.5 * move.accel * move_time) * move_time
        pos = (
            move.start_x + move.x_r * dist,
            move.start_y + move.y_r * dist,
            move.start_z + move.z_r * dist
        )
        velocity = move.start_v + move.accel * move_time
        return pos, velocity

    def _sample_printtime_sync(self, skip=0, count=1):
        toolhead = self.printer.lookup_object("toolhead")
        move_time = toolhead.get_last_move_time()
        settle_clock = self._mcu.print_time_to_clock(move_time)
        samples = []
        total = skip + count

        def cb(sample):
            if sample["clock"] >= settle_clock:
                samples.append(sample)
                if len(samples) >= total:
                    raise Stop_Streaming

        with self.streaming_session(cb, latency=skip+count) as ss:
            ss.wait()

        samples = samples[skip:]

        if count == 1:
            return samples[0]
        else:
            return samples

    def _sample(self, skip, count):
        samples = self._sample_printtime_sync(skip, count)
        return (median([s["dist"] for s in samples]), samples)

    def _sample_async(self, count=1):
        samples = []
        def cb(sample):
            samples.append(sample)
            if len(samples) >= count:
                raise Stop_Streaming

        with self.streaming_session(cb, latency=count) as ss:
            ss.wait()

        if count == 1:
            return samples[0]
        else:
            return samples

    def count_to_freq(self, count):
        return count*self.sensor_freq/(2**28)

    def freq_to_count(self, freq):
        return freq*(2**28)/self.sensor_freq

    def dist_to_freq(self, dist, temp):
        if self.model is None:
            return None
        return self.model.dist_to_freq(dist, temp)

    def freq_to_dist(self, freq, temp):
        if self.model is None:
            return None
        return self.model.freq_to_dist(freq, temp)

    def get_status(self, eventtime):
        model = None
        if self.model is not None:
            model = self.model.name
        x_offset, y_offset, z_offset = self.get_offsets()
        return {
            'name': self.name,
            'last_sample': self.last_sample,
            'model': model,
            'offsets': {'x': x_offset, 'y': y_offset, 'z': z_offset}
        }

    # Webhook handlers

    def _handle_req_status(self, web_request):
        temp = None
        sample = self._sample_async()
        out = {
            "freq": sample["freq"],
            "dist": sample["dist"],
        }
        temp = sample["temp"]
        if temp is not None:
            out["temp"] = temp
        web_request.send(out)

    def _handle_req_dump(self, web_request):
        self._api_dump_helper.add_client(web_request)

    # GCode command handlers

    cmd_PROBE_help = "Probe Z-height at current XY position"
    def cmd_PROBE(self, gcmd):
        pos = self.run_probe(gcmd)
        gcmd.respond_info("Result is z=%.6f" % (pos[2],))

    cmd_MD_DIST_CALIBRATE_help = "Calibrate MD_DIST response curve"
    def cmd_MD_DIST_CALIBRATE(self,gcmd):
        self._start_calibration(gcmd)

    cmd_MD_DIST_ESTIMATE_BACKLASH_help = "Estimate Z axis backlash"
    def cmd_MD_DIST_ESTIMATE_BACKLASH(self, gcmd):
        # Get to correct Z height
        overrun = gcmd.get_float("OVERRUN", 1.0)
        speed = gcmd.get_float("PROBE_SPEED", self.speed, above=0.0)
        cur_z = self.toolhead.get_position()[2]
        self.toolhead.manual_move([None, None, cur_z+overrun], speed)
        self.run_probe(gcmd)

        lift_speed = self.get_lift_speed(gcmd)
        target = gcmd.get_float('Z', self.trigger_distance)

        num_samples = gcmd.get_int("SAMPLES", 20)
        wait = self.z_settling_time

        samples_up = []
        samples_down = []

        next_dir = -1

        try:
            self._start_streaming()

            (cur_dist, _samples) = self._sample(wait, 10)
            pos = self.toolhead.get_position()
            missing = target - cur_dist
            target = pos[2] + missing
            gcmd.respond_info("Target kinematic Z is %.3f" % (target,))

            if target - overrun < 0:
                raise gcmd.error("Target minus overrun must exceed 0mm")

            while len(samples_up) + len(samples_down) < num_samples:
                liftpos = [None, None, target + overrun * next_dir]
                self.toolhead.manual_move(liftpos, lift_speed)
                liftpos = [None, None, target]
                self.toolhead.manual_move(liftpos, lift_speed)
                self.toolhead.wait_moves()
                (dist, _samples) = self._sample(wait, 10)
                {-1: samples_up, 1: samples_down}[next_dir].append(dist)
                next_dir = next_dir * -1

        finally:
            self._stop_streaming()

        res_up = median(samples_up)
        res_down = median(samples_down)

        gcmd.respond_info(
            "Median distance moving up %.5f, down %.5f, "
            "delta %.5f over %d samples"
            % (res_up, res_down, res_down - res_up, num_samples)
        )

    cmd_MD_DIST_QUERY_help = "Take a sample from the sensor"
    def cmd_MD_DIST_QUERY(self, gcmd):
        sample = self._sample_async()
        last_value = sample["freq"]
        dist = sample["dist"]
        temp = sample["temp"]
        self.last_sample = {
            "time": sample["time"],
            "value": last_value,
            "temp": temp,
            "dist": dist,
        }
        if dist is None:
            gcmd.respond_info(
                "Last reading: %.2fHz, %.2fC, no model"
                % (last_value, temp,)
            )
        else:
            gcmd.respond_info(
                "Last reading: %.2fHz, %.2fC, %.5fmm"
                % (last_value, temp, dist)
            )

    cmd_MD_DIST_STREAM_help = "Enable MD_DIST Streaming"
    def cmd_MD_DIST_STREAM(self, gcmd):
        if self._log_stream is not None:
            self._log_stream.stop()
            self._log_stream = None
            gcmd.respond_info("MD_DIST Streaming disabled")
        else:
            f = None
            completion_cb = None
            fn = gcmd.get("FILENAME")
            f = open(fn, "w")
            def close_file():
                f.close()
            completion_cb = close_file
            f.write("time,data,data_smooth,freq,dist,\
                     temp,pos_x,pos_y,pos_z,vel\n")

            def cb(sample):
                pos = sample.get("pos", None)
                obj = "%.4f,%d,%.2f,%.5f,%.5f,%.2f,%s,%s,%s,%s\n" % (
                    sample["time"],
                    sample["data"],
                    sample["data_smooth"],
                    sample["freq"],
                    sample["dist"],
                    sample["temp"],
                    "%.3f" % (pos[0],) if pos is not None else "",
                    "%.3f" % (pos[1],) if pos is not None else "",
                    "%.3f" % (pos[2],) if pos is not None else "",
                    "%.3f" % (sample["vel"],) if "vel" in sample else ""
                )
                f.write(obj)

            self._log_stream = self.streaming_session(cb, completion_cb)
            gcmd.respond_info("MD_DIST Streaming enabled")

    cmd_PROBE_ACCURACY_help = "Probe Z-height accuracy at current XY position"
    def cmd_PROBE_ACCURACY(self, gcmd):
        speed = gcmd.get_float("PROBE_SPEED", self.speed, above=0.0)
        lift_speed = self.get_lift_speed(gcmd)
        sample_count = gcmd.get_int("SAMPLES", 10, minval=1)
        sample_retract_dist = gcmd.get_float("SAMPLE_RETRACT_DIST", 0)
        allow_faulty = gcmd.get_int("ALLOW_FAULTY_COORDINATE", 0) != 0
        pos = self.toolhead.get_position()
        gcmd.respond_info(
            "PROBE_ACCURACY at X:%.3f Y:%.3f Z:%.3f"
            " (samples=%d retract=%.3f"
            " speed=%.1f lift_speed=%.1f)\n"
            % (pos[0], pos[1], pos[2],
                sample_count, sample_retract_dist,
                speed, lift_speed)
        )

        start_height = self.trigger_distance + sample_retract_dist
        liftpos = [None, None, start_height]
        self.toolhead.manual_move(liftpos, lift_speed)

        self.multi_probe_begin()
        positions = []
        while len(positions) < sample_count:
            pos = self._probe(speed, allow_faulty=allow_faulty)
            positions.append(pos)
            self.toolhead.manual_move(liftpos, lift_speed)
        self.multi_probe_end()

        zs = [p[2] for p in positions]
        max_value = max(zs)
        min_value = min(zs)
        range_value = max_value - min_value
        avg_value = sum(zs) / len(positions)
        median_ = median(zs)

        deviation_sum = 0
        for i in range(len(zs)):
            deviation_sum += pow(zs[2] - avg_value, 2.0)
        sigma = (deviation_sum / len(zs)) ** 0.5

        gcmd.respond_info(
            "probe accuracy results: maximum %.6f, minimum %.6f, range %.6f, "
            "average %.6f, median %.6f, standard deviation %.6f"
            % (max_value, min_value, range_value, avg_value, median_, sigma))

    cmd_Z_OFFSET_APPLY_PROBE_help = "Adjust the probe's z_offset"
    def cmd_Z_OFFSET_APPLY_PROBE(self, gcmd):
        gcode_move = self.printer.lookup_object("gcode_move")
        offset = gcode_move.get_status()["homing_origin"].z

        if offset == 0:
            self.gcode.respond_info("Nothing to do: Z Offset is 0")
            return

        if not self.model:
            raise self.gcode.error("You must calibrate your model first, "
                                   "use MD_DIST_CALIBRATE.")

        # We use the model code to save the new offset, but we can't actually
        # apply that offset yet because the gcode_offset is still in effect.
        # If the user continues to do stuff after this, the newly set model
        # offset would compound with the gcode offset. To ensure this doesn't
        # happen, we revert to the old model offset afterwards.
        # Really, the user should just be calling `SAVE_CONFIG` now.
        old_offset = self.model.offset
        self.model.offset += offset
        self.model.save(self, False)
        gcmd.respond_info(
            "md_dist model offset has been updated\n"
            "You must run the SAVE_CONFIG command now to update the\n"
            "printer config file and restart the printer."
        )
        self.model.offset = old_offset

class MD_Dist_Model:
    @classmethod
    def load(cls, name, config, md_dist):
        coef = config.getfloatlist("model_coef")
        temp = config.getfloat("model_temp")
        domain = config.getfloatlist("model_domain", count=2)
        [min_z, max_z] = config.getfloatlist("model_range", count=2)
        offset = config.getfloat("model_offset", 0.0)
        poly = Polynomial(coef, domain)
        return MD_Dist_Model(name, md_dist, poly, temp, min_z, max_z, offset)

    def __init__(self, name, md_dist, poly, temp, min_z, max_z, offset=0):
        self.name = name
        self.md_dist = md_dist
        self.poly = poly
        self.min_z = min_z
        self.max_z = max_z
        self.temp = temp
        self.offset = offset
        self.enable_temp_compensate = md_dist.enable_temp_compensate

    def save(self, md_dist, show_message=True):
        configfile = md_dist.printer.lookup_object("configfile")
        section = "md_dist model " + self.name
        configfile.set(section, "model_coef",
                       ",\n  ".join(map(str, self.poly.coef)))
        configfile.set(section, "model_domain",
                       ",".join(map(str, self.poly.domain)))
        configfile.set(section, "model_range",
                       "%f,%f" % (self.min_z, self.max_z))
        configfile.set(section, "model_temp",
                       "%f" % (self.temp))
        configfile.set(section, "model_offset", "%.5f" % (self.offset,))
        if show_message:
            md_dist.gcode.respond_info(
                "MD_DIST calibration for model '%s' has "
                "been updated\nfor the current session. The SAVE_CONFIG "
                "command will\nupdate the printer config file and restart "
                "the printer." % (self.name,)
            )

    def freq_to_dist_raw(self, freq):
        [begin, end] = self.poly.domain
        invfreq = 1/freq
        if invfreq > end:
            return float("inf")
        elif invfreq < begin:
            return float("-inf")
        else:
            return float(self.poly(invfreq) - self.offset)

    def freq_to_dist(self, freq, temp):
        if (self.temp is not None
            and self.md_dist.model_temp is not None
            and self.enable_temp_compensate):
            freq = self.md_dist.model_temp.compensate(freq, temp, self.temp)
        return self.freq_to_dist_raw(freq)

    def dist_to_freq_raw(self, dist, max_e=0.00000001):
        if dist < self.min_z or dist > self.max_z:
            msg = ("Attempted to map out-of-range distance %f, valid range "
                   "[%.3f, %.3f]" % (dist, self.min_z, self.max_z))
            raise self.md_dist.printer.command_error(msg)
        dist += self.offset
        [begin, end] = self.poly.domain
        for _ in range(0, 50):
            f = (end + begin) / 2
            v = self.poly(f)
            if abs(v-dist) < max_e:
                return float(1.0/f)
            elif v < dist:
                begin = f
            else:
                end = f
        raise self.md_dist.printer.command_error(
                "md_dist model convergence error")

    def dist_to_freq(self, dist, temp, max_e=0.00000001):
        freq = self.dist_to_freq_raw(dist, max_e)
        if (self.temp is not None
            and self.md_dist.model_temp is not None
            and self.enable_temp_compensate):
            freq = self.md_dist.model_temp.compensate(freq, self.temp, temp)
        return freq

class MD_Dist_Temp_Model_Builder:
    _DEFAULTS = {
        "amfg": 1.0,
        "tcc": -1.56165495e-05,
        "tcfl": -1.11115902e-12,
        "tctl": 3.6738370e-16,
        "fmin" : None,
        "fmin_temp" : None
    }

    @classmethod
    def load(cls, config):
        return MD_Dist_Temp_Model_Builder(config)

    def __init__(self, config):
        self.parameters = MD_Dist_Temp_Model_Builder._DEFAULTS.copy()
        for key in self.parameters.keys():
            param = config.getfloat("tc_" + key, None)
            if param is not None:
                self.parameters[key] = param

    def build(self):
        if self.parameters["fmin"] is None \
           or self.parameters["fmin_temp"] is None:
            return None
        logging.info("MD_DIST: built tempco model %s", self.parameters)
        return MD_Dist_Temp_Model(**self.parameters)

    def build_with_base(self, md_dist):
        base_data = md_dist.md_dist_base_read_cmd.send([6, 0])
        (f_count, adc_count) = struct.unpack("<IH", base_data["bytes"])
        if f_count < 0xFFFFFFFF and adc_count < 0xFFFF:
            if self.parameters["fmin"] is None:
                self.parameters["fmin"] = md_dist.count_to_freq(f_count)
                logging.info(
                    "MD_DIST: loaded fmin=%.2f from base",
                    self.parameters["fmin"]
                )
            if self.parameters["fmin_temp"] is None:
                temp_adc = float(adc_count) / md_dist.temp_smooth_count * \
                           md_dist.inv_adc_max
                self.parameters["fmin_temp"] = md_dist.thermistor.calc_temp(
                    temp_adc
                )
                logging.info(
                    "MD_DIST: loaded fmin_temp=%.2f from base",
                    self.parameters["fmin_temp"]
                )
        else:
            logging.info("MD_DIST: fmin parameters not found in base")
        return self.build()

class MD_Dist_Temp_Model:
    def __init__(self, amfg, tcc, tcfl, tctl, fmin, fmin_temp):
        self.amfg = amfg
        self.tcc = tcc
        self.tcfl = tcfl
        self.tctl = tctl
        self.fmin = fmin
        self.fmin_temp = fmin_temp

    def _tcf(self, f, df, dt, tctl):
        tctl = self.tctl if tctl is None else tctl
        tc = self.tcc + self.tcfl * df + tctl * df * df
        return f + self.amfg * tc * dt * f

    def compensate(self, freq, temp_source, temp_target, tctl=None):
        dt = temp_target - temp_source
        dfmin = self.fmin * self.amfg * self.tcc * \
                (temp_source - self.fmin_temp)
        df = freq - (self.fmin + dfmin)
        if dt < 0.:
            f2 = self._tcf(freq, df, dt, tctl)
            dfmin2 = self.fmin * self.amfg * self.tcc * \
                    (temp_target - self.fmin_temp)
            df2 = f2 - (self.fmin + dfmin2)
            f3 = self._tcf(f2, df2, -dt, tctl)
            ferror = freq - f3
            freq = freq + ferror
            df = freq - (self.fmin + dfmin)
        return self._tcf(freq, df, dt, tctl)

class Model_Manager:
    def __init__(self, md_dist):
        self.md_dist = md_dist
        self.gcode = md_dist.printer.lookup_object("gcode")
        self.gcode.register_command(
            "MD_DIST_MODEL_SELECT",
            self.cmd_MD_DIST_MODEL_SELECT,
            desc=self.cmd_MD_DIST_MODEL_SELECT_help
        )
        self.gcode.register_command(
            "MD_DIST_MODEL_SAVE",
            self.cmd_MD_DIST_MODEL_SAVE,
            desc=self.cmd_MD_DIST_MODEL_SAVE_help
        )
        self.gcode.register_command(
            "MD_DIST_MODEL_REMOVE",
            self.cmd_MD_DIST_MODEL_REMOVE,
            desc=self.cmd_MD_DIST_MODEL_REMOVE_help
        )
        self.gcode.register_command(
            "MD_DIST_MODEL_LIST",
            self.cmd_MD_DIST_MODEL_LIST,
            desc=self.cmd_MD_DIST_MODEL_LIST_help
        )

    cmd_MD_DIST_MODEL_SELECT_help = "Load named md_dist model"
    def cmd_MD_DIST_MODEL_SELECT(self, gcmd):
        name = gcmd.get("NAME")
        model = self.md_dist.models.get(name, None)
        if model is None:
            raise gcmd.error("Unknown model '%s'" % (name,))
        self.md_dist.model = model
        gcmd.respond_info("Selected md_dist model '%s'" % (name,))

    cmd_MD_DIST_MODEL_SAVE_help = "Save current md_dist model"
    def cmd_MD_DIST_MODEL_SAVE(self, gcmd):
        model = self.md_dist.model
        if model is None:
            raise gcmd.error("No model currently selected")
        oldname = model.name
        name = gcmd.get("NAME", oldname)
        if name != oldname:
            model = copy.copy(model)
        model.name = name
        model.save(self.md_dist)
        if name != oldname:
            self.md_dist.models[name] = model

    cmd_MD_DIST_MODEL_REMOVE_help = "Remove saved md_dist model"
    def cmd_MD_DIST_MODEL_REMOVE(self, gcmd):
        name = gcmd.get("NAME")
        model = self.md_dist.models.get(name, None)
        if model is None:
            raise gcmd.error("Unknown model '%s'" % (name,))
        configfile = self.md_dist.printer.lookup_object("configfile")
        section = "md_dist model " + model.name
        configfile.remove_section(section)
        self.md_dist.models.pop(name)
        gcmd.respond_info(
            "Model '%s' was removed for the current session.\n"
            "Run SAVE_CONFIG to update the printer configuration"
            "and restart Klipper." % (name,)
        )
        if self.md_dist.model == model:
            self.md_dist.model = None

    cmd_MD_DIST_MODEL_LIST_help = "Remove saved md_dist model"
    def cmd_MD_DIST_MODEL_LIST(self, gcmd):
        if not self.md_dist.models:
            gcmd.respond_info("No md_dist models loaded")
            return
        gcmd.respond_info("List of loaded md_dist models:")
        current_model = self.md_dist.model
        for _name, model in sorted(self.md_dist.models.items()):
            if model == current_model:
                gcmd.respond_info("- %s [active]" % (model.name,))
            else:
                gcmd.respond_info("- %s" % (model.name,))


class Alpha_Beta_Filter:
    def __init__(self, alpha, beta):
        self.alpha = alpha
        self.beta = beta
        self.reset()

    def reset(self):
        self.xl = None
        self.vl = 0
        self.tl = None

    def update(self, time, measurement):
        if self.xl == None:
            self.xl = measurement
        if self.tl is not None:
            dt = time - self.tl
        else:
            dt = 0
        self.tl = time
        xk = self.xl + self.vl * dt
        vk = self.vl
        rk = measurement - xk
        xk = xk + self.alpha * rk
        if dt > 0:
            vk = vk + self.beta / dt * rk
        self.xl = xk
        self.vl = vk
        return xk

    def value(self):
        return self.xl

class Streaming_Helper:
    def __init__(self, md_dist, callback, completion_callback, latency):
        self.md_dist = md_dist
        self.cb = callback
        self.completion_cb = completion_callback
        self.completion = self.md_dist.reactor.completion()

        self.latency_key = None
        if latency is not None:
            self.latency_key = self.md_dist.request_stream_latency(latency)

        self.md_dist._stream_callbacks[self] = self._handle
        self.md_dist._start_streaming()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def _handle(self, sample):
        try:
            self.cb(sample)
        except Stop_Streaming:
            self.completion.complete(())

    def stop(self):
        if not self in self.md_dist._stream_callbacks:
            return
        del self.md_dist._stream_callbacks[self]
        self.md_dist._stop_streaming()
        if self.latency_key is not None:
            self.md_dist.drop_stream_latency_request(self.latency_key)
        if self.completion_cb is not None:
            self.completion_cb()

    def wait(self):
        self.completion.wait()
        self.stop()

class Stop_Streaming(Exception):
    pass


class API_Dump_Helper:
    def __init__(self, md_dist):
        self.md_dist = md_dist
        self.clients = {}
        self.stream = None
        self.buffer = []
        self.fields = ["dist", "temp", "pos", "freq", "vel", "time"]

    def _start_stop(self):
        if not self.stream and self.clients:
            self.stream = self.md_dist.streaming_session(self._cb)
        elif self.stream is not None and not self.clients:
            self.stream.stop()
            self.stream = None

    def _cb(self, sample):
        tmp = [sample.get(key, None) for key in self.fields]
        self.buffer.append(tmp)
        if len(self.buffer) > 50:
            self._update_clients()

    def _update_clients(self):
        for cconn, template in list(self.clients.items()):
            if cconn.is_closed():
                del self.clients[cconn]
                self._start_stop()
                continue
            tmp = dict(template)
            tmp["params"] = self.buffer
            cconn.send(tmp)
        self.buffer = []

    def add_client(self, web_request):
        cconn = web_request.get_client_connection()
        template = web_request.get_dict("response_template", {})
        self.clients[cconn] = template
        self._start_stop()
        web_request.send({"header": self.fields})

class MD_Dist_Probe_Wrapper:
    def __init__(self, md_dist):
        self.md_dist = md_dist

    def multi_probe_begin(self):
        return self.md_dist.multi_probe_begin()
    def multi_probe_end(self):
        return self.md_dist.multi_probe_end()
    def get_offsets(self):
        return self.md_dist.get_offsets()
    def get_lift_speed(self, gcmd=None):
        return self.md_dist.get_lift_speed(gcmd)
    def run_probe(self, gcmd):
        return self.md_dist.run_probe(gcmd)
    def get_status(self, eventtime):
        return self.md_dist.get_status(eventtime)

class MD_Dist_Temp_Wrapper:
    def __init__(self, md_dist):
        self.md_dist = md_dist

    def get_temp(self, eventtime):
        return self.md_dist.last_temp, 0

    def get_status(self, eventtime):
        return {
            "temperature": round(self.md_dist.last_temp, 2),
            "measured_min_temp": round(self.md_dist.measured_min, 2),
            "measured_max_temp": round(self.md_dist.measured_max, 2)
        }

TRSYNC_TIMEOUT = 0.025
TRSYNC_SINGLE_MCU_TIMEOUT = 0.250

class MD_Dist_Endstop_Wrapper:
    def __init__(self, md_dist):
        self.md_dist = md_dist
        self._mcu = md_dist._mcu

        ffi_main, ffi_lib = chelper.get_ffi()
        self._trdispatch = ffi_main.gc(
            ffi_lib.trdispatch_alloc(),
            ffi_lib.free
        )
        self._trsyncs = [MCU_trsync(self.md_dist._mcu, self._trdispatch)]

        printer = self.md_dist.printer
        printer.register_event_handler(
            "klippy:mcu_identify",
            self._handle_mcu_identify
        )
        printer.register_event_handler(
            "homing:home_rails_begin",
            self._handle_home_rails_begin
        )
        printer.register_event_handler(
            "homing:home_rails_end",
            self._handle_home_rails_end
        )

        self.z_homed = False
        self.is_homing = False

    def _handle_mcu_identify(self):
        self.toolhead = self.md_dist.printer.lookup_object("toolhead")
        kin = self.toolhead.get_kinematics()
        for stepper in kin.get_steppers():
            if stepper.is_active_axis('z'):
                self.add_stepper(stepper)

    def _handle_home_rails_begin(self, homing_state, rails):
        self.is_homing = False

    def _handle_home_rails_end(self, homing_state, rails):
        if self.md_dist.model is None:
            return

        if not self.is_homing:
            return

        if 2 not in homing_state.get_axes():
            return

        # After homing Z we perform a measurement and 
        # adjust the toolhead kinematic position.
        (dist, samples) = self.md_dist._sample(
            self.md_dist.z_settling_time, 10
        )
        if math.isinf(dist):
            logging.error(
                "Post-homing adjustment measured samples %s",
                samples
            )
            raise self.md_dist.printer.command_error(
                "Toolhead stopped below model range"
            )
        homing_state.set_homed_position([None, None, dist])

    def get_mcu(self):
        return self._mcu

    def add_stepper(self, stepper):
        trsyncs = {trsync.get_mcu(): trsync for trsync in self._trsyncs}
        stepper_mcu = stepper.get_mcu()
        trsync = trsyncs.get(stepper_mcu)
        if trsync is None:
            trsync = MCU_trsync(stepper_mcu, self._trdispatch)
            self._trsyncs.append(trsync)
        trsync.add_stepper(stepper)
        # Check for unsupported multi-mcu shared stepper rails, duplicated
        # from MCU_endstop
        sname = stepper.get_name()
        if sname.startswith("stepper_"):
            for ot in self._trsyncs:
                for s in ot.get_steppers():
                    if ot is not trsync and s.get_name().startswith(sname[:9]):
                        cerror = self._mcu.get_printer().config_error
                        raise cerror("Multi-mcu homing not supported on"
                                     " multi-mcu shared axis")

    def get_steppers(self):
        return [s for trsync in self._trsyncs for s in trsync.get_steppers()]

    def home_start(self, print_time, sample_time,
                   sample_count, rest_time, triggered=True):
        if self.md_dist.model is None:
            raise self.md_dist.printer.command_error("No md_dist model loaded")

        self.is_homing = True
        self.md_dist._apply_threshold()
        self.md_dist._sample_async()
        clock = self._mcu.print_time_to_clock(print_time)
        rest_ticks = self._mcu.print_time_to_clock(print_time+rest_time)-clock
        self._rest_ticks = rest_ticks
        reactor = self._mcu.get_printer().get_reactor()
        self._trigger_completion = reactor.completion()
        expire_timeout = TRSYNC_TIMEOUT
        if len(self._trsyncs) == 1:
            expire_timeout = TRSYNC_SINGLE_MCU_TIMEOUT
        for i, trsync in enumerate(self._trsyncs):
            try:
                trsync.start(
                    print_time, self._trigger_completion, expire_timeout
                )
            except TypeError:
                offset = float(i) / len(self._trsyncs)
                trsync.start(
                    print_time, offset, self._trigger_completion,
                    expire_timeout
                )
        etrsync = self._trsyncs[0]
        ffi_main, ffi_lib = chelper.get_ffi()
        ffi_lib.trdispatch_start(self._trdispatch, etrsync.REASON_HOST_REQUEST)
        self.md_dist.md_dist_home_cmd.send([
            etrsync.get_oid(),
            etrsync.REASON_ENDSTOP_HIT,
            0,
        ])
        return self._trigger_completion

    def home_wait(self, home_end_time):
        etrsync = self._trsyncs[0]
        etrsync.set_home_end_time(home_end_time)
        if self._mcu.is_fileoutput():
            self._trigger_completion.complete(True)
        self._trigger_completion.wait()
        self.md_dist.md_dist_stop_home_cmd.send()
        ffi_main, ffi_lib = chelper.get_ffi()
        ffi_lib.trdispatch_stop(self._trdispatch)
        res = [trsync.stop() for trsync in self._trsyncs]
        if any([r == etrsync.REASON_COMMS_TIMEOUT for r in res]):
            return -1.0
        if res[0] != etrsync.REASON_ENDSTOP_HIT:
            return 0.0
        if self._mcu.is_fileoutput():
            return home_end_time
        return home_end_time

    def query_endstop(self, print_time):
        if self.md_dist.model is None:
            return 1
        clock = self._mcu.print_time_to_clock(print_time)
        sample = self.md_dist._sample_async()
        if self.md_dist.trigger_freq <= sample["freq"]:
            return 1
        else:
            return 0

    def get_position_endstop(self):
        return self.md_dist.trigger_distance

class MD_Dist_Mesh_Helper:
    @classmethod
    def create(cls, md_dist, config):
        if config.has_section("bed_mesh"):
            return MD_Dist_Mesh_Helper(md_dist, config)
        else:
            return None

    def __init__(self, md_dist, config):
        self.md_dist = md_dist
        mesh_config = self.mesh_config = config.getsection("bed_mesh")
        self.bm = self.md_dist.printer.load_object(mesh_config, "bed_mesh")

        self.speed = mesh_config.getfloat(
            "speed", 50.0, above=0.0, note_valid=False
        )
        self.def_min_x, self.def_min_y = mesh_config.getfloatlist(
            "mesh_min", count=2, note_valid=False
        )
        self.def_max_x, self.def_max_y = mesh_config.getfloatlist(
            "mesh_max", count=2, note_valid=False
        )
        self.def_res_x, self.def_res_y = mesh_config.getintlist(
            "probe_count", count=2, note_valid=False
        )
        self.rri = mesh_config.getint(
            "relative_reference_index", None, note_valid=False
        )
        self.dir = config.getchoice(
            "mesh_main_direction",
            {'x':'x', 'X':'x', 'y':'y', 'Y':'y'},
            'y'
        )
        self.overscan = config.getfloat("mesh_overscan", -1, minval=0)
        self.cluster_size = config.getfloat("mesh_cluster_size", 1, minval=0)
        self.runs = config.getint("mesh_runs", 1, minval=1)
        self.rewrite_command = config.getboolean('rewrite_command', True)

        self.faulty_regions = []
        for i in list(range(1, 100, 1)):
            start = mesh_config.getfloatlist(
                "faulty_region_%d_min" % (i,), None, count=2
            )
            if start is None:
                break
            end = mesh_config.getfloatlist(
                "faulty_region_%d_max" % (i,), count=2
            )
            x_min = min(start[0], end[0])
            x_max = max(start[0], end[0])
            y_min = min(start[1], end[1])
            y_max = max(start[1], end[1])
            self.faulty_regions.append(Region(x_min, x_max, y_min, y_max))

        self.gcode = self.md_dist.printer.lookup_object(
            "gcode"
        )
        if self.rewrite_command:
            self.prev_gcmd = self.gcode.register_command(
                "BED_MESH_CALIBRATE", None
            )
            self.gcode.register_command(
                "BED_MESH_CALIBRATE",
                self.cmd_BED_MESH_CALIBRATE,
                desc=self.cmd_BED_MESH_CALIBRATE_help
            )

        if self.overscan < 0:
            printer = self.md_dist.printer
            printer.register_event_handler(
                "klippy:mcu_identify",
                self._handle_mcu_identify
            )

    cmd_BED_MESH_CALIBRATE_help = "Perform Mesh Bed Leveling"
    def cmd_BED_MESH_CALIBRATE(self, gcmd):
        method = gcmd.get("METHOD", "md_dist").lower()
        if method == "md_dist":
            self.calibrate(gcmd)
        # else:
        #     self.prev_gcmd(gcmd)

    def _handle_mcu_identify(self):
        # Auto determine a safe overscan amount
        toolhead = self.md_dist.printer.lookup_object("toolhead")
        curtime = self.md_dist.reactor.monotonic()
        status = toolhead.get_kinematics().get_status(curtime)
        xo = self.md_dist.x_offset
        yo = self.md_dist.y_offset
        settings = {
            'x': {
                "range": [self.def_min_x-xo,\
                          self.def_max_x-xo],
                "machine": [status["axis_minimum"][0],\
                            status["axis_maximum"][0]],
                "count": self.def_res_y,
            },
            'y': {
                "range": [self.def_min_y-yo,\
                          self.def_max_y-yo],
                "machine": [status["axis_minimum"][1],\
                            status["axis_maximum"][1]],
                "count": self.def_res_x,
            }
        }[self.dir]

        r = settings["range"]
        m = settings["machine"]
        space = (r[1] - r[0]) / (float(settings["count"]-1))
        self.overscan = min([
            max(0, r[0]-m[0]),
            max(0, m[1]-r[1]),
            space+2.0, # A half circle with 2mm lead in/out
        ])

    def _generate_path(self):
        xo = self.md_dist.x_offset
        yo = self.md_dist.y_offset
        settings = {
            'x': {
                "range_aligned": [self.min_x-xo, self.max_x-xo],
                "range_perpendicular": [self.min_y-yo, self.max_y-yo],
                "count": self.res_y,
                "swap_coord": False,
            },
            'y': {
                "range_aligned": [self.min_y-yo, self.max_y-yo],
                "range_perpendicular": [self.min_x-xo, self.max_x-xo],
                "count": self.res_x,
                "swap_coord": True,
            }
        }[self.dir]

        # We build the path in "normalized" coordinates and then simply
        # swap x and y at the end if we need to
        begin_a, end_a = settings["range_aligned"]
        begin_p, end_p = settings["range_perpendicular"]
        swap_coord = settings["swap_coord"]
        step = (end_p - begin_p) / (float(settings["count"]-1))
        points = []
        corner_radius = min(step/2, self.overscan)
        for i in range(0, settings["count"]):
            pos_p = begin_p + step * i
            even = i % 2 == 0 # If even we are going 'right', else 'left'
            pa = (begin_a, pos_p) if even else (end_a, pos_p)
            pb = (end_a, pos_p) if even else (begin_a, pos_p)

            l = (pa,pb)

            if len(points) > 0 and corner_radius > 0:
                # We need to insert an overscan corner. Basically we insert
                # a rounded rectangle to smooth out the transition and retain
                # as much speed as we can.
                #
                #  ---|---<
                # /
                # |
                # \
                #  ---|--->
                #
                # We just need to draw the two 90 degree arcs. They contain
                # the endpoints of the lines connecting everything.
                if even:
                    center = begin_a - self.overscan + corner_radius
                    points += arc_points(
                        center, pos_p - step + corner_radius,
                        corner_radius, -90, -90
                    )
                    points += arc_points(
                        center, pos_p - corner_radius,
                        corner_radius, -180, -90
                    )
                else:
                    center = end_a + self.overscan - corner_radius
                    points += arc_points(
                        center, pos_p - step + corner_radius,
                        corner_radius, -90, 90
                    )
                    points += arc_points(
                        center, pos_p - corner_radius,
                        corner_radius, 0, 90
                    )

            points.append(l[0])
            points.append(l[1])

        if swap_coord:
            for i in range(len(points)):
                (x,y) = points[i]
                points[i] = (y,x)

        return points

    def calibrate(self, gcmd):
        self.min_x, self.min_y = coord_fallback(
            gcmd, "MESH_MIN", float,
            self.def_min_x, self.def_min_y, lambda v, d: max(v, d)
        )
        self.max_x, self.max_y = coord_fallback(
            gcmd, "MESH_MAX", float,
            self.def_max_x, self.def_max_y, lambda v, d: min(v, d)
        )
        self.res_x, self.res_y = coord_fallback(
            gcmd, "PROBE_COUNT", int,
            self.def_res_x, self.def_res_y, lambda v, _d: max(v, 3)
        )
        self.profile_name = gcmd.get("PROFILE", "default")

        if self.min_x > self.max_x:
            self.min_x, self.max_x = (
                max(self.max_x, self.def_min_x),
                min(self.min_x, self.def_max_x)
            )
        if self.min_y > self.max_y:
            self.min_y, self.max_y = (
                max(self.max_y, self.def_min_y),
                min(self.min_y, self.def_max_y)
            )

        self.step_x = (self.max_x - self.min_x) / (self.res_x - 1)
        self.step_y = (self.max_y - self.min_y) / (self.res_y - 1)

        self.toolhead = self.md_dist.toolhead
        path = self._generate_path()

        probe_speed = gcmd.get_float(
            "PROBE_SPEED",
            self.md_dist.speed, above=0.0
        )
        self.md_dist._move_to_probing_height(probe_speed)

        speed = gcmd.get_float("SPEED", self.speed, above=0.0)
        runs = gcmd.get_int("RUNS", self.runs, minval=1)

        try:
            self.md_dist._start_streaming()

            # Move to first location
            (x,y) = path[0]
            self.toolhead.manual_move([x, y, None], speed)
            self.toolhead.wait_moves()

            self.md_dist._sample_printtime_sync(5)
            clusters = self._sample_mesh(gcmd, path, speed, runs)

        finally:
            self.md_dist._stop_streaming()

        clusters = self._interpolate_faulty(clusters)
        self._apply_mesh(clusters, gcmd)

        if(self.md_dist.end_park_x is not None \
           and self.md_dist.end_park_y is not None):
            if(self.md_dist.first_move_axis == 'x'):
                self.toolhead.manual_move(
                    [self.md_dist.end_park_x, None, None],
                    speed
                )
                self.toolhead.wait_moves()
            else:
                self.toolhead.manual_move(
                    [None, self.md_dist.end_park_y, None],
                    speed
                )
                self.toolhead.wait_moves()

            self.toolhead.manual_move(
                [self.md_dist.end_park_x, self.md_dist.end_park_y, None],
                speed
            )
            self.toolhead.wait_moves()

    def _fly_path(self, path, speed, runs):
        # Run through the path
        for i in range(runs):
            p = path if i % 2 == 0 else reversed(path)
            for (x,y) in p:
                self.toolhead.manual_move([x, y, None], speed)
        self.toolhead.wait_moves()

    def _is_valid_position(self, x, y):
        return self.min_x <= x <= self.max_x and self.min_y <= y <= self.min_y

    def _sample_mesh(self, gcmd, path, speed, runs):
        cs = gcmd.get_float("CLUSTER_SIZE", self.cluster_size, minval=0.0)

        min_x, min_y = self.min_x, self.min_y
        xo, yo = self.md_dist.x_offset, self.md_dist.y_offset

        clusters = {}
        total_samples = [0]
        invalid_samples = [0]

        def cb(sample):
            total_samples[0] += 1
            d = sample["dist"]
            (x, y, z) = sample["pos"]
            x += xo
            y += yo

            if math.isinf(d):
                if self._is_valid_position(x, y):
                    invalid_samples[0] += 1
                return

            # Calculate coordinate of the cluster we are in
            xi = int(round((x - min_x) / self.step_x))
            yi = int(round((y - min_y) / self.step_y))

            # If there's a cluster size limit, apply it here
            if cs > 0:
                xf = xi * self.step_x + min_x
                yf = yi * self.step_y + min_y
                dx = x - xf
                dy = y - yf
                dist = math.sqrt(dx*dx+dy*dy)
                if dist > cs:
                    return

            k = (xi, yi)

            if k not in clusters:
                clusters[k] = []
            clusters[k].append(d)

        with self.md_dist.streaming_session(cb) as ss:
            self._fly_path(path, speed, runs)

        gcmd.respond_info(
            "Sampled %d total points over %d runs" % (total_samples[0], runs)
        )
        if invalid_samples[0]:
            gcmd.respond_info(
                "!! Encountered %d invalid samples!" % (invalid_samples[0],)
            )
        gcmd.respond_info(
            "Samples binned in %d clusters" % (len(clusters),)
        )

        return clusters

    def _is_faulty_coordinate(self, x, y, add_offsets=False):
        if add_offsets:
            xo, yo = self.md_dist.x_offset, self.md_dist.y_offset
            x += xo
            y += yo
        for r in self.faulty_regions:
            if r.is_point_within(x, y):
                return True
        return False

    def _interpolate_faulty(self, clusters):
        faulty_indexes = []
        xi_max = 0
        yi_max = 0
        for (xi, yi), points in clusters.items():
            if xi > xi_max:
                xi_max = xi
            if yi > yi_max:
                yi_max = yi
            xc = xi * self.step_x + self.min_x
            yc = yi * self.step_y + self.min_y
            if self._is_faulty_coordinate(xc, yc):
                clusters[(xi, yi)] = None
                faulty_indexes.append((xi, yi))

        def get_nearest(start, dx, dy):
            (x, y) = start
            x += dx
            y += dy
            while (x >= 0 and x <= xi_max and y >= 0 and y <= yi_max):
                if clusters[(x, y)] is not None:
                    return (abs(x-start[0])+abs(y-start[0]), \
                            median(clusters[(x,y)]))
                x += dx
                y += dy
            return None

        def interp_weighted(lower, higher):
            if lower is None and higher is None:
                return None
            if lower is None and higher is not None:
                return higher[1]
            elif lower is not None and higher is None:
                return lower[1]
            else:
                return ((lower[1] * lower[0] + higher[1] * higher[0]) \
                        / (lower[0] + higher[0]))

        for coord in faulty_indexes:
            xl = get_nearest(coord, -1,  0)
            xh = get_nearest(coord,  1,  0)
            xavg = interp_weighted(xl, xh)
            yl = get_nearest(coord,  0, -1)
            yh = get_nearest(coord,  0,  1)
            yavg = interp_weighted(yl, yh)
            avg = None
            if xavg is not None and yavg is None:
                avg = xavg
            elif xavg is None and yavg is not None:
                avg = yavg
            else:
                avg = (xavg + yavg) / 2.0
            clusters[coord] = [avg]

        return clusters

    def _apply_mesh(self, clusters, gcmd):
        matrix = []
        td = self.md_dist.trigger_distance
        for yi in range(self.res_y):
            line = []
            for xi in range(self.res_x):
                cluster = clusters.get((xi,yi), None)
                if cluster is None or len(cluster) == 0:
                    xc = xi * self.step_x + self.min_x
                    yc = yi * self.step_y + self.min_y
                    logging.info(
                        "Cluster (%.3f,%.3f)[%d,%d] is empty!"
                        % (xc, yc, xi, yi)
                    )
                    err = (
                        "Empty clusters found\n"
                        "Try increasing mesh cluster_size or slowing down"
                    )
                    raise self.gcode.error(err)
                data = [td-d for d in cluster]
                line.append(median(data))
            matrix.append(line)

        rri = gcmd.get_int("RELATIVE_REFERENCE_INDEX", self.rri)
        if rri is not None:
            if rri < 0 or rri >= self.res_x * self.res_y:
                rri = None

        if rri is not None:
            rri_x = rri % self.res_x
            rri_y = int(math.floor(rri / self.res_x))
            z_offset = matrix[rri_y][rri_x]
            for i, line in enumerate(matrix):
                matrix[i] = [z-z_offset for z in line]

        params = dict(self.bm.bmc.mesh_config)
        params["min_x"] = self.min_x
        params["max_x"] = self.max_x
        params["min_y"] = self.min_y
        params["max_y"] = self.max_y
        params["x_count"] = self.res_x
        params["y_count"] = self.res_y
        try:
            mesh = bed_mesh.ZMesh(params)
        except:
            mesh = bed_mesh.ZMesh(params, self.profile_name)
        try:
            mesh.build_mesh(matrix)
        except bed_mesh.BedMeshError as e:
            raise self.gcode.error(str(e))
        self.bm.set_mesh(mesh)
        self.gcode.respond_info("Mesh calibration complete")
        self.bm.save_profile(self.profile_name)

class Region:
    def __init__(self, x_min, x_max, y_min, y_max):
        self.x_min = x_min
        self.x_max = x_max
        self.y_min = y_min
        self.y_max = y_max

    def is_point_within(self, x, y):
        return ((x > self.x_min and x < self.x_max) \
                and (y > self.y_min and y < self.y_max))

def arc_points(cx, cy, r, start_angle, span):
    # Angle delta is determined by a max deviation(md) from 0.1mm:
    #   r * versin(d_a) < md
    #   versin(d_a) < md/r
    #   d_a < arcversin(md/r)
    #   d_a < arccos(1-md/r)
    # We then determine how many of these we can fit in exactly
    # 90 degrees(rounding up) and then determining the exact
    # delta angle.
    start_angle = start_angle / 180.0 * math.pi
    span = span / 180.0 * math.pi
    d_a = math.acos(1 - 0.1 / r)
    cnt = int(math.ceil(abs(span) / d_a))
    d_a = span / float(cnt)

    points = []
    for i in range(cnt+1):
        ang = start_angle + d_a*float(i)
        x = cx + math.cos(ang)*r
        y = cy + math.sin(ang)*r
        points.append((x,y))

    return points

def coord_fallback(gcmd, name, parse, def_x, def_y, map=lambda v, d: v):
    param = gcmd.get(name, None)
    if param is not None:
        try:
            x, y = [parse(p.strip()) for p in param.split(",", 1)]
            return map(x, def_x), map(y, def_y)
        except:
            raise gcmd.error("Unable to parse parameter '%s'" % (name,))
    else:
        return def_x, def_y

def median(samples):
    return float(np.median(samples))

def load_config(config):
    md_dist = MD_Dist_Probe(config)
    config.get_printer().add_object(probe_object_name, MD_Dist_Probe_Wrapper(md_dist))
    temp = MD_Dist_Temp_Wrapper(md_dist)
    config.get_printer().add_object("temperature_sensor MD_DIST", temp)
    pheaters = md_dist.printer.load_object(config, "heaters")
    pheaters.available_sensors.append("temperature_sensor MD_DIST")
    return md_dist

def load_config_prefix(config):
    md_dist = config.get_printer().lookup_object("md_dist")
    name = config.get_name()
    if name.startswith("md_dist model "):
        name = name[14:]
        model = MD_Dist_Model.load(name, config, md_dist)
        md_dist._register_model(name, model)
        return model
    else:
        raise config.error("Unknown MD_DIST config directive '%s'"
              % (name[7:],))
