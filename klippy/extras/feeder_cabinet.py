#!/usr/bin/env python3
# 送料柜控制模块
#
# Copyright (C) 2024 <your name> <your email>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import math
import time
import os

class FeederCabinet:
    # 状态常量定义
    STATE_IDLE = 0
    STATE_RUNOUT = 1 
    STATE_REQUESTING = 2
    STATE_FEEDING = 3
    STATE_LOADED = 4
    STATE_ERROR = 5
    STATE_COMPLETE = 6

    # CAN通信命令定义
    CMD_REQUEST_FEED = 0x01
    CMD_STOP_FEED = 0x02
    CMD_QUERY_STATUS = 0x03
    CMD_PRINTING = 0x04
    CMD_PRINT_COMPLETE = 0x05
    CMD_PRINT_PAUSE = 0x06
    CMD_PRINT_CANCEL = 0x07
    CMD_PRINTER_IDLE = 0x08

    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name()
        self.config = config  # 保存config对象
        
        # 获取配置参数
        self.canbus_uuid = config.get('canbus_uuid')
        self.can_interface = config.get('can_interface', 'can0')
        self.auto_feed = config.getboolean('auto_feed', True)
        self.runout_sensor = config.get('runout_sensor', None)
        self.log_level = config.getint('log_level', 1)  # 0=错误, 1=信息, 2=调试
        
        # 初始化状态
        self.state = self.STATE_IDLE
        self.error_code = 0
        self.progress = 0
        self.is_printing = False
        self.print_paused = False
        
        # CAN通信相关
        self.can = None
        self.can_node_id = None
        self.receive_queue = []
        self.last_status_time = 0
        self.status_timer = None
        
        # 错误处理相关
        self.error_dict = {
            0x00: "无错误",
            0x01: "机械故障",
            0x02: "耗材缺失",
            0x03: "通信超时",
            0x04: "未知错误"
        }
        self.max_retries = config.getint('max_retries', 3)
        self.retry_count = 0
        self.last_error_time = 0
        
        # 日志记录
        self.log_file = config.get('log_file', None)
        if self.log_file:
            self.log_file = os.path.expanduser(self.log_file)
        
        # 注册事件处理器
        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.printer.register_event_handler("klippy:disconnect", self._handle_disconnect)
        
        # 注册打印事件处理器
        self.printer.register_event_handler("print_stats:printing", 
                                          self._handle_printing)
        self.printer.register_event_handler("print_stats:paused",
                                          self._handle_paused)
        self.printer.register_event_handler("print_stats:complete",
                                          self._handle_complete)
        
        # 注册G-code命令
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('START_FEEDER_CABINET', self.cmd_START_FEEDER_CABINET,
                             desc=self.cmd_START_FEEDER_CABINET_help)
        gcode.register_command('QUERY_FEEDER_CABINET', self.cmd_QUERY_FEEDER_CABINET,
                             desc=self.cmd_QUERY_FEEDER_CABINET_help)
        gcode.register_command('CANCEL_FEEDER_CABINET', self.cmd_CANCEL_FEEDER_CABINET,
                             desc=self.cmd_CANCEL_FEEDER_CABINET_help)

    def _handle_connect(self):
        """处理Klipper连接事件"""
        try:
            # 初始化CAN总线通信
            self._init_canbus()
            
            # 设置耗材传感器回调
            if self.runout_sensor:
                self.runout_sensor = self.printer.lookup_object(self.runout_sensor)
                if hasattr(self.runout_sensor, 'register_runout_callback'):
                    self.runout_sensor.register_runout_callback(self._handle_runout)
                else:
                    logging.warning("送料柜: 耗材传感器不支持runout回调")
        except Exception:
            logging.exception("送料柜初始化失败")
            raise

    def _handle_ready(self):
        """处理Klipper就绪事件"""
        # 设置初始状态
        self.state = self.STATE_IDLE
        self.error_code = 0
        self.progress = 0

    def _init_canbus(self):
        """初始化CAN总线通信"""
        # 获取打印机对象
        self.reactor = self.printer.get_reactor()
        
        # 初始化CAN总线
        cbid = self.printer.load_object(self.config, 'canbus_ids')
        self.can_node_id = cbid.get_nodeid(self.canbus_uuid)
        
        # 设置发送和接收ID
        self.tx_id = self.can_node_id * 2 + 256
        self.rx_id = self.tx_id + 1
        
        # 创建CAN通信对象
        self.can = self.printer.lookup_object('canbus').get_canbus(self.can_interface)
        
        # 注册接收回调
        self.can.register_callback(self.rx_id, self._handle_can_receive)
        
        # 启动状态查询定时器
        self.status_timer = self.reactor.register_timer(
            self._status_timer_event, self.reactor.NOW)

    def _handle_disconnect(self):
        """处理断开连接事件"""
        if self.status_timer is not None:
            self.reactor.unregister_timer(self.status_timer)
            self.status_timer = None

    def _status_timer_event(self, eventtime):
        """定时查询状态"""
        if self.state != self.STATE_IDLE:
            # 检查通信超时
            if eventtime - self.last_status_time > 5.0:  # 5秒超时
                self._log(0, "CAN通信超时")
                self._handle_error(0x03)  # 通信超时错误
            else:
                # 发送状态查询
                self._send_can_message(self.CMD_QUERY_STATUS)
                self._log(2, "发送状态查询")
        return eventtime + 1.0  # 每秒查询一次

    def _send_can_message(self, cmd, data=None):
        """发送CAN消息"""
        msg = bytearray([cmd])  # 命令类型
        msg.append(0)  # 挤出头编号(默认0)
        
        # 添加数据
        if data is not None:
            msg.extend(data)
        
        # 补齐消息长度到8字节
        while len(msg) < 8:
            msg.append(0)
            
        # 发送消息
        self.can.send_message(self.tx_id, msg)

    def _handle_printing(self, print_time):
        """处理打印开始事件"""
        self.is_printing = True
        self._send_can_message(self.CMD_PRINTING)

    def _handle_paused(self, print_time):
        """处理打印暂停事件"""
        self.print_paused = True
        self._send_can_message(self.CMD_PRINT_PAUSE)

    def _handle_complete(self, print_time):
        """处理打印完成事件"""
        self.is_printing = False
        self.print_paused = False
        self._send_can_message(self.CMD_PRINT_COMPLETE)

    def _handle_runout(self, print_time):
        """处理耗材用尽事件"""
        if not self.auto_feed or not self.is_printing:
            return
            
        logging.info("送料柜: 检测到耗材用尽")
        self.state = self.STATE_RUNOUT
        
        # 暂停打印
        pause_resume = self.printer.lookup_object('pause_resume')
        pause_resume.send_pause_command()
        
        # 等待打印机暂停
        reactor = self.printer.get_reactor()
        reactor.pause(reactor.monotonic() + 2.)
        
        # 发送补料请求
        self._send_can_message(self.CMD_REQUEST_FEED)
        self.state = self.STATE_REQUESTING
        
        # 通知用户
        gcode = self.printer.lookup_object('gcode')
        gcode.respond_info("检测到耗材用尽，已启动自动续料")

    def _handle_can_receive(self, msg_id, msg):
        """处理接收到的CAN消息"""
        if len(msg) < 3:
            self._log(0, "接收到无效的CAN消息")
            return
            
        status = msg[0]  # 状态码
        progress = msg[1]  # 进度
        error = msg[2]  # 错误码
        
        self._log(2, "接收CAN消息: 状态=%d, 进度=%d, 错误=%d" % 
                 (status, progress, error))
        
        # 更新状态
        self.progress = progress
        self.error_code = error
        
        # 根据状态码更新状态
        if status == 0x00:  # 空闲
            self.state = self.STATE_IDLE
            self.retry_count = 0  # 重置重试计数
        elif status == 0x01:  # 准备送料
            self.state = self.STATE_REQUESTING
        elif status == 0x02:  # 送料中
            self.state = self.STATE_FEEDING
        elif status == 0x03:  # 送料完成
            self.state = self.STATE_LOADED
            self.retry_count = 0  # 重置重试计数
            self._handle_load_complete()
        elif status == 0x04:  # 送料失败
            self._handle_error(error)
            
        # 记录最后接收状态的时间
        self.last_status_time = self.reactor.monotonic()

    def _handle_load_complete(self):
        """处理送料完成"""
        if not self.print_paused:
            return
            
        # 恢复打印
        pause_resume = self.printer.lookup_object('pause_resume')
        pause_resume.send_resume_command()
        
        # 通知用户
        gcode = self.printer.lookup_object('gcode')
        gcode.respond_info("送料完成，已恢复打印")
        
        # 更新状态
        self.state = self.STATE_COMPLETE
        self._send_can_message(self.CMD_PRINTING)

    def _handle_error(self, error_code):
        """处理错误"""
        error_msg = self.error_dict.get(error_code, "未知错误(代码:%d)" % error_code)
        self._log(0, "送料柜错误: %s" % error_msg)
        
        # 记录错误时间
        self.last_error_time = self.reactor.monotonic()
        
        # 检查是否需要重试
        if self.retry_count < self.max_retries:
            self.retry_count += 1
            self._log(1, "尝试重试 (%d/%d)" % (self.retry_count, self.max_retries))
            
            # 等待2秒后重试
            self.reactor.pause(self.last_error_time + 2.)
            self._send_can_message(self.CMD_REQUEST_FEED)
            self.state = self.STATE_REQUESTING
        else:
            self._log(0, "达到最大重试次数，需要手动处理")
            self.state = self.STATE_ERROR

    def _log(self, level, msg):
        """记录日志"""
        if level > self.log_level:
            return
            
        # 获取当前时间
        current_time = time.strftime("%Y-%m-%d %H:%M:%S")
        
        # 构建日志消息
        log_msg = "[%s] %s" % (current_time, msg)
        
        # 根据级别记录日志
        if level == 0:  # 错误
            logging.error(log_msg)
        elif level == 1:  # 信息
            logging.info(log_msg)
        else:  # 调试
            logging.debug(log_msg)
            
        # 写入日志文件
        if self.log_file:
            try:
                with open(self.log_file, 'a') as f:
                    f.write(log_msg + "\n")
            except Exception:
                logging.exception("写入日志文件失败")

    def get_status(self, eventtime=None):
        """返回当前状态信息"""
        return {
            'state': self.state,
            'error_code': self.error_code,
            'error_msg': self.error_dict.get(self.error_code, "未知错误"),
            'progress': self.progress,
            'retry_count': self.retry_count,
            'is_printing': self.is_printing,
            'print_paused': self.print_paused,
            'auto_feed': self.auto_feed
        }

    cmd_START_FEEDER_CABINET_help = "手动启动送料柜续料"
    def cmd_START_FEEDER_CABINET(self, gcmd):
        """手动启动送料柜续料"""
        if self.state != self.STATE_IDLE:
            raise gcmd.error("送料柜当前正忙")
        # 发送补料请求
        self._send_can_message(self.CMD_REQUEST_FEED)
        self.state = self.STATE_REQUESTING
        gcmd.respond_info("已发送补料请求")

    cmd_QUERY_FEEDER_CABINET_help = "查询送料柜状态"
    def cmd_QUERY_FEEDER_CABINET(self, gcmd):
        """查询送料柜状态"""
        status = self.get_status()
        state_desc = {
            self.STATE_IDLE: "空闲",
            self.STATE_RUNOUT: "耗材用尽",
            self.STATE_REQUESTING: "请求补料",
            self.STATE_FEEDING: "送料中",
            self.STATE_LOADED: "已装载",
            self.STATE_ERROR: "错误",
            self.STATE_COMPLETE: "完成"
        }.get(status['state'], "未知")
        
        gcmd.respond_info(
            "当前状态：%s\n"
            "进度：%d%%\n"
            "错误代码：%d" % (
                state_desc,
                status['progress'],
                status['error_code']
            ))

    cmd_CANCEL_FEEDER_CABINET_help = "取消送料柜操作"
    def cmd_CANCEL_FEEDER_CABINET(self, gcmd):
        """取消送料柜操作"""
        if self.state == self.STATE_IDLE:
            raise gcmd.error("送料柜当前空闲")
        # 发送停止命令
        self._send_can_message(self.CMD_STOP_FEED)
        self.state = self.STATE_IDLE
        gcmd.respond_info("已取消操作")

def load_config(config):
    return FeederCabinet(config) 